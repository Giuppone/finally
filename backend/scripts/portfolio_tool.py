#!/usr/bin/env python3
"""Put the FinAlly portfolio into a known state, or save and restore one.

    portfolio_tool.py equal   [options]     equal-dollar-weight across the watchlist
    portfolio_tool.py random  [options]     a reproducible, deliberately lopsided book
    portfolio_tool.py broker  [--source N]  broker export -> editable TICKER WEIGHT% list
    portfolio_tool.py dump    [--name N]    current holdings -> editable TICKER QTY list
    portfolio_tool.py build   [--name N]    reset, then buy the holdings in that list
    portfolio_tool.py save    [--file F]    write the current session to JSON (exact)
    portfolio_tool.py load    [--file F]    restore a saved session (exact)

Everything goes through the public API, so the trade lock, validation, tracked-set sync
and post-trade snapshots all apply exactly as they do for a human clicking Buy. Nothing
here touches Docker, the volume, or the SQLite file.

Stdlib only - no new dependency, and it runs under any Python 3.12 on the machine:

    cd backend && uv run python scripts/portfolio_tool.py equal        # normal path
    python backend/scripts/portfolio_tool.py equal                     # any system python
    docker exec finally python /app/scripts/portfolio_tool.py equal \
        --base http://127.0.0.1:8000                                   # no python on host

The `scripts/*.sh` wrappers pick whichever of those works. See
planning/REBALANCE_TEST_HARNESS.md for the design and the reasoning.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE = f"http://localhost:{os.environ.get('FINALLY_PORT', '8000')}"
DEFAULT_SESSION_DIR = Path(__file__).resolve().parents[2] / "sessions"
# Holdings lists live apart from the JSON sessions on purpose: a session is an exact,
# machine-written round trip (cash and average costs included), a list is a handful of
# quantities meant to be edited by hand.
DEFAULT_LIST_DIR = Path(__file__).resolve().parents[2] / "suggested"

# Below this a "rebalance" is noise: a $2 order clutters the blotter and moves no weight.
MIN_NOTIONAL = 10.0
QUANTITY_DP = 4                 # fractional shares are supported; four places is plenty


class ApiError(RuntimeError):
    pass


# ---- allocation: pure functions, unit-tested in tests/test_portfolio_tool.py ----

@dataclass(frozen=True)
class Order:
    ticker: str
    side: str
    quantity: float
    notional: float


def equal_weights(tickers: list[str]) -> dict[str, float]:
    """Equal *dollar* weight - which is deliberately NOT equal risk weight.

    That gap is the point: ALAB carries sigma~1.06 against SLV's ~0.75, and LRCX/AMAT are
    0.90-correlated near-duplicates, so 1/n of the money is nowhere near 1/n of the
    volatility. It gives the rebalancer something real to correct.
    """
    if not tickers:
        return {}
    share = 1.0 / len(tickers)
    return {ticker: share for ticker in tickers}


def random_weights(tickers: list[str], rng: random.Random,
                   alpha: float = 0.6) -> dict[str, float]:
    """A Dirichlet(alpha) draw, built from gamma variates so it stays stdlib-only.

    `alpha` below 1 concentrates the mass in one or two names, which is what makes the
    minimum-variance suggestion large and visible. `alpha` above 1 tends toward uniform.
    """
    if not tickers:
        return {}
    draws = [max(rng.gammavariate(alpha, 1.0), 1e-12) for _ in tickers]
    total = math.fsum(draws)
    return {ticker: draw / total for ticker, draw in zip(tickers, draws)}


def to_orders(weights: dict[str, float], prices: dict[str, float], cash: float,
              invest: float) -> tuple[list[Order], list[str]]:
    """Turn target weights into buy orders against `cash * invest`.

    Quantities are TRUNCATED, never rounded, at four decimals. Rounding up on every leg
    can push the batch past the budget, and it is the last order that then fails - after
    the rest have already filled, leaving a half-built portfolio.
    """
    budget = cash * invest
    orders: list[Order] = []
    warnings: list[str] = []

    for ticker in sorted(weights):
        price = prices.get(ticker)
        if price is None or price <= 0:
            warnings.append(f"{ticker}: no price yet - skipped")
            continue
        notional = budget * weights[ticker]
        if notional < MIN_NOTIONAL:
            warnings.append(f"{ticker}: ${notional:,.2f} is below the ${MIN_NOTIONAL:.0f} "
                            f"minimum - skipped")
            continue
        scale = 10 ** QUANTITY_DP
        quantity = math.floor(notional / price * scale) / scale
        if quantity <= 0:
            warnings.append(f"{ticker}: ${notional:,.2f} buys less than "
                            f"{1 / scale:g} shares at ${price:,.2f} - skipped")
            continue
        orders.append(Order(ticker, "buy", quantity, quantity * price))

    return orders, warnings


# ---- HTTP --------------------------------------------------------------------

class Api:
    """Minimal JSON client. urllib, so the script needs nothing installed."""

    def __init__(self, base: str, timeout: float = 20.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    def get(self, path: str) -> dict:
        return self._send("GET", path, None)

    def post(self, path: str, payload: dict | None = None) -> dict:
        return self._send("POST", path, payload)

    def _send(self, method: str, path: str, payload: dict | None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            raise ApiError(f"{method} {path} -> {exc.code}: {_detail(exc)}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(
                f"cannot reach {self.base} ({exc.reason}). Is the container running? "
                f"./scripts/start_mac.sh  or  .\\scripts\\start_windows.ps1"
            ) from exc


def _detail(exc: urllib.error.HTTPError) -> str:
    """The backend returns structured `detail` bodies; show the reason, not 'Bad Request'."""
    try:
        body = json.loads(exc.read())
    except Exception:                                    # noqa: BLE001
        return exc.reason or "no body"
    detail = body.get("detail", body)
    if isinstance(detail, dict):
        return str(detail.get("reason") or detail)
    if isinstance(detail, list) and detail:
        return str(detail[0].get("msg", detail[0]))
    return str(detail)


def wait_healthy(api: Api, seconds: float = 60.0) -> None:
    """Block until /api/health reports ok - which means the schema is applied AND the
    market task is ticking, so every watchlist ticker has a price to fill at."""
    deadline = time.monotonic() + seconds
    last = "no response"
    announced = False
    while time.monotonic() < deadline:
        try:
            health = api.get("/api/health")
            if health.get("status") == "ok":
                if announced:
                    print(" ready")
                return
            last = json.dumps(health.get("market", health))
        except ApiError as exc:
            last = str(exc)
        if not announced:
            # A container that is still booting refuses connections for a few seconds, so
            # retrying is right - but retrying in silence for a minute reads as a hang,
            # and a typo in --base looks identical to a slow start.
            print(f"waiting for {api.base} ", end="", flush=True)
            announced = True
        print(".", end="", flush=True)
        time.sleep(2.0)
    if announced:
        print()
    raise ApiError(f"the app did not become healthy within {seconds:.0f}s - last: {last}")


# ---- shared steps ------------------------------------------------------------

def confirm(prompt: str, assume_yes: bool) -> None:
    """Gate the destructive step. Refuses rather than hangs when nobody can answer.

    `isatty()` is not enough on its own: under Git Bash on Windows a redirected stdin can
    still report as a terminal, and `input()` then dies on EOF with a traceback instead of
    the one line that tells the user to pass --yes.
    """
    if assume_yes:
        return
    needs_yes = ApiError(f"{prompt} Pass --yes to confirm (nothing is reading stdin).")
    if not sys.stdin.isatty():
        raise needs_yes
    try:
        answer = input(f"{prompt} [y/N] ")
    except EOFError:
        raise needs_yes from None
    if answer.strip().lower() not in ("y", "yes"):
        raise SystemExit("cancelled")


def watchlist_prices(api: Api) -> dict[str, float]:
    """Ticker -> price for everything watched. Unpriced names are simply absent, which is
    what lets `to_orders` warn about them instead of dividing by zero."""
    body = api.get("/api/watchlist")
    return {
        entry["ticker"]: float(entry["price"])
        for entry in body["tickers"]
        if entry.get("priced") and entry.get("price")
    }


def resolve_tickers(api: Api, requested: str | None, count: int | None,
                    rng: random.Random, allow_writes: bool = True) -> list[str]:
    """The watchlist, or `--tickers` (adding any that are not watched yet)."""
    watched = [entry["ticker"] for entry in api.get("/api/watchlist")["tickers"]]

    if requested:
        wanted = [t.strip().upper() for t in requested.split(",") if t.strip()]
        for ticker in wanted:
            if ticker in watched:
                continue
            if not allow_writes:
                # Adding it is a write. Left off the watchlist it has no price either, so
                # the plan will show it skipped rather than silently sized against nothing.
                print(f"  ! {ticker} is not watched; a real run would add it first")
                continue
            # Also what pulls it into the tracked set and gives it a price (PLAN.md §6).
            api.post("/api/watchlist", {"ticker": ticker})
        return wanted

    if count is not None and count < len(watched):
        return sorted(rng.sample(watched, count))
    return sorted(watched)


def place(api: Api, orders: list[Order]) -> None:
    """Sequential, stopping on the first rejection.

    Never parallel: each buy is validated against the cash its predecessors left, exactly
    as PLAN.md §9 requires of the LLM trade path.
    """
    for order in orders:
        api.post("/api/portfolio/trade", {
            "ticker": order.ticker, "side": order.side, "quantity": order.quantity,
        })
        print(f"  {order.side:4} {order.quantity:>12,.4f} {order.ticker:<6} "
              f"~ ${order.notional:>12,.2f}")


def report(api: Api, targets: dict[str, float] | None = None) -> dict:
    """Print the resulting book. Realised weights land within ~+/-0.5% of target: prices
    tick every 500ms and the orders are sequential, so the book moves while it is built."""
    state = api.get("/api/portfolio")
    print(f"\n  {'TICKER':<8}{'QTY':>14}{'PRICE':>12}{'VALUE':>14}{'WEIGHT':>9}"
          f"{'TARGET':>9}")
    for holding in state["positions"]:
        target = (targets or {}).get(holding["ticker"])
        print(f"  {holding['ticker']:<8}{holding['quantity']:>14,.4f}"
              f"{holding['price']:>12,.2f}{holding['market_value']:>14,.2f}"
              f"{holding['weight'] * 100:>8.2f}%"
              f"{(f'{target * 100:>8.2f}%' if target is not None else '        -')}")
    print(f"\n  cash ${state['cash_balance']:,.2f}   "
          f"positions ${state['positions_value']:,.2f}   "
          f"total ${state['total_value']:,.2f}")
    return state


# ---- commands ----------------------------------------------------------------

def cmd_seed(args: argparse.Namespace) -> int:
    api = Api(args.base)
    rng = random.Random(args.seed)
    wait_healthy(api)

    # --dry-run means NO writes AT ALL, and that has to include the reset and the watchlist
    # additions below - both of which used to run before the plan was ever printed. A flag
    # that promises to change nothing and then empties the account is worse than no flag.
    if args.dry_run:
        print("dry run: nothing will be written")
    elif not args.no_reset:
        confirm("Reset the portfolio to $10,000 and the seed watchlist? "
                "Positions, trades and P&L history will be deleted.", args.yes)
        api.post("/api/portfolio/reset")
        print("reset: $10,000 cash, seed watchlist")

    # A random book over all ten names is barely lopsided; six is enough to be visibly
    # concentrated and still worth diversifying.
    count = args.count if args.count is not None else (6 if args.mode == "random" else None)
    tickers = resolve_tickers(api, args.tickers, count, rng, allow_writes=not args.dry_run)
    if not tickers:
        raise ApiError("no tickers to allocate across")

    weights = (equal_weights(tickers) if args.mode == "equal"
               else random_weights(tickers, rng, args.concentration))
    prices = watchlist_prices(api)
    cash = float(api.get("/api/portfolio")["cash_balance"])
    orders, warnings = to_orders(weights, prices, cash, args.invest)

    label = "equal weight" if args.mode == "equal" else f"random (seed {args.seed})"
    print(f"\n{label} across {len(tickers)} tickers, deploying "
          f"{args.invest:.0%} of ${cash:,.2f}")
    for warning in warnings:
        print(f"  ! {warning}")

    if args.dry_run:
        for order in orders:
            print(f"  would buy {order.quantity:>12,.4f} {order.ticker:<6} "
                  f"~ ${order.notional:>12,.2f}")
        return 0

    if not orders:
        raise ApiError("nothing to buy - every leg was filtered out (see warnings above)")

    print()
    place(api, orders)
    state = report(api, weights)

    if args.json:
        print(json.dumps({
            "mode": args.mode, "seed": args.seed, "targets": weights,
            "orders": [order.__dict__ for order in orders],
            "warnings": warnings, "portfolio": state,
        }, indent=2))
    return 0


@dataclass(frozen=True)
class BrokerRow:
    ticker: str
    name: str
    quantity: float
    price: float
    market_value: float
    #: False for a locally-listed instrument with no US underlying (no "CEDEAR" prefix).
    is_cedear: bool


# One holding of an Argentine broker export, flattened across five lines. The market-value
# line carries the NEXT record's ticker, which is why this matches five lines and consumes
# four: `finditer` then re-enters on that trailing ticker.
_BROKER_RECORD = re.compile(
    r"(?P<ticker>[A-Z][A-Z0-9]{0,6})[ \t]*\n"
    r"(?P<name>[^\n]+)\n"
    r"(?P<qty>[\d.]+)\$(?P<price>[\d.,]+)[ \t]+[-\d.,]+%"
    r"\$(?P<avg>[\d.,]+)(?P<pnl>-?\$[\d.,]+)[ \t]*\n"
    r"[ \t]*(?P<pnlpct>-?[\d.,]+)%[ \t]*\n"
    r"\$(?P<value>[\d.,]+)"
)


def parse_amount(text: str) -> float:
    """Argentine number format: `.` groups thousands, `,` is the decimal separator.

    So `305.650` is three hundred five thousand six hundred fifty, not 305.65 - reading it
    the American way understates a holding by a factor of a thousand.
    """
    return float(text.replace(".", "").replace(",", "."))


def parse_broker(text: str) -> tuple[list[BrokerRow], list[str]]:
    """Parse a broker's holdings export into rows.

    The layout is a table flattened into lines, and every row is checked against its own
    arithmetic: `quantity x price` must equal the reported market value. That is a free
    correctness test on a format nobody documented, and it is what distinguishes "parsed"
    from "parsed correctly" - a decimal-separator mistake would sail through any looser
    check and produce a portfolio a thousand times too large.
    """
    rows: list[BrokerRow] = []
    warnings: list[str] = []

    for match in _BROKER_RECORD.finditer(text):
        ticker = match["ticker"]
        quantity = parse_amount(match["qty"])
        price = parse_amount(match["price"])
        value = parse_amount(match["value"])

        if abs(quantity * price - value) > max(1.0, value * 1e-6):
            warnings.append(
                f"{ticker}: {quantity:g} x {price:,.2f} = {quantity * price:,.2f} but the "
                f"file says {value:,.2f} - skipped, the row did not parse cleanly"
            )
            continue

        rows.append(BrokerRow(
            ticker=ticker, name=match["name"].strip(), quantity=quantity,
            price=price, market_value=value,
            is_cedear=match["name"].strip().upper().startswith("CEDEAR"),
        ))

    if not rows:
        warnings.append("no holdings recognised - is this the right file?")
    return rows, warnings


def parse_list(text: str) -> tuple[list[tuple[str, float]], str, list[str]]:
    """Parse a hand-editable holdings list into ([(ticker, amount)], mode, warnings).

    Two row shapes, and `mode` says which was used:

        MU 4        -> 4 shares          (mode "shares")
        MU 12.5%    -> 12.5% of the book (mode "weight")

    Weights exist because that is the only thing worth carrying over from a real brokerage
    account: share counts there are denominated in different instruments at different
    prices in a different currency. Mixing the two shapes in one file is an error rather
    than a guess - there is no sensible reading of "4 shares of MU and 30% of AMD".

    Deliberately liberal about separators - `MU 10`, `MU,10`, `MU: 10` and `MU=10` all mean
    the same thing, because this file exists to be typed by a human and a format that
    rejects the obvious spellings is a format people stop using. `#` starts a comment, blank
    lines are ignored, and a line that cannot be read is REPORTED and skipped rather than
    aborting the run: one fat-fingered row should not throw away the other nine.
    """
    holdings: list[tuple[str, float]] = []
    warnings: list[str] = []
    seen: set[str] = set()
    modes: set[str] = set()

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue

        parts = [part for part in re.split(r"[\s,:=]+", line) if part]
        if len(parts) != 2:
            warnings.append(f"line {number}: expected 'TICKER QTY', got {line!r} - skipped")
            continue

        ticker = parts[0].upper()
        raw_amount = parts[1]
        mode = "weight" if raw_amount.endswith("%") else "shares"
        try:
            amount = float(raw_amount.rstrip("%"))
        except ValueError:
            warnings.append(f"line {number}: {raw_amount!r} is not a number - skipped")
            continue

        if amount <= 0:
            warnings.append(f"line {number}: {ticker} amount {amount:g} is not positive "
                            f"- skipped")
            continue
        if ticker in seen:
            warnings.append(f"line {number}: {ticker} listed twice - skipped")
            continue

        seen.add(ticker)
        modes.add(mode)
        holdings.append((ticker, amount / 100.0 if mode == "weight" else amount))

    if len(modes) > 1:
        raise ValueError(
            "this list mixes share counts and percentages, which have no combined reading. "
            "Use one or the other."
        )

    return holdings, (modes.pop() if modes else "shares"), warnings


def format_list(holdings: list[tuple[str, float]], header: list[str],
                mode: str = "shares") -> str:
    """Render a holdings list. Aligned columns, and a header that says how to load it back -
    a saved file nobody remembers the command for is a saved file nobody uses."""
    lines = list(header) + [
        "#",
        "# Edit it, add or delete rows, then load it with:",
        "#     .\\scripts\\load_list.ps1 --name NAME --yes        (PowerShell)",
        "#     ./scripts/load_list.sh --name NAME --yes          (bash)",
        "#",
        "# Loading RESETS to $10,000 and buys at the current price.",
        "",
    ]
    width = max((len(ticker) for ticker, _ in holdings), default=6)
    for ticker, amount in holdings:
        rendered = f"{amount * 100:.3f}%" if mode == "weight" else f"{amount:g}"
        lines.append(f"{ticker:<{width}}  {rendered}")
    return "\n".join(lines) + "\n"


def cmd_dump(args: argparse.Namespace) -> int:
    api = Api(args.base)
    wait_healthy(api)

    state = api.get("/api/portfolio")
    holdings = [(row["ticker"], float(row["quantity"])) for row in state["positions"]]
    if not holdings:
        raise ApiError("no positions to save - buy something first")

    path = _list_path(args.file, args.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "# FinAlly holdings list - one TICKER QUANTITY per line.",
        f"# Saved {time.strftime('%Y-%m-%d %H:%M:%S')} from a "
        f"${state['total_value']:,.2f} book.",
    ]
    path.write_text(format_list(holdings, header), encoding="utf-8")

    print(f"saved {path}")
    for ticker, quantity in holdings:
        print(f"  {ticker:<8}{quantity:>14,.4f}")
    print(f"\n  {len(holdings)} holdings, total ${state['total_value']:,.2f}")
    print(f"\nEdit it, then load it back with:  load_list --name {args.name} --yes")
    return 0


def cmd_broker(args: argparse.Namespace) -> int:
    """Convert a broker holdings export into a FinAlly weights list.

    **Weights, not share counts, and that is not a shortcut.** A real Argentine brokerage
    account holds CEDEARs: certificates over a *fraction* of a US share, at a ratio that
    differs per stock, priced in pesos. 100 MU CEDEARs is not 100 MU shares, and a nine-figure
    peso book is not a $10,000 one. The share counts in that file are therefore meaningless
    here in every respect except one - the proportions they represent. So the proportions
    are what gets carried over, and `build` sizes them against whatever cash it has.

    Purely text in, text out: no API call, nothing written but the list. Read it, edit it,
    then load it.
    """
    source = Path(args.file) if args.file else DEFAULT_LIST_DIR / f"{args.source}.txt"
    if not source.is_file():
        raise ApiError(f"no broker export at {source}")

    rows, warnings = parse_broker(source.read_text(encoding="utf-8"))
    for warning in warnings:
        print(f"  ! {warning}")
    if not rows:
        raise ApiError(f"{source}: nothing parsed")

    total = sum(row.market_value for row in rows)
    keep = [row for row in rows if row.is_cedear or args.keep_local]
    dropped = [row for row in rows if row not in keep]

    # Renormalised over what is kept, so dropping a local listing does not silently leave
    # the book under-invested by its weight.
    kept_total = sum(row.market_value for row in keep)
    holdings = [(row.ticker, row.market_value / kept_total) for row in keep]
    holdings.sort(key=lambda item: -item[1])

    header = [
        f"# Converted from {source.name} on {time.strftime('%Y-%m-%d %H:%M:%S')}.",
        f"# {len(rows)} holdings, {total:,.2f} ARS.",
        "#",
        "# WEIGHTS, not share counts. Those are CEDEARs - fractional claims on a US share,",
        "# at a different ratio per stock, priced in pesos - so their quantities mean nothing",
        "# in a $10,000 USD book. The proportions do, and those are what is reproduced.",
    ]
    for row in dropped:
        header.append(f"# Dropped {row.ticker} ({row.name}): locally listed, no US ticker.")

    path = _list_path(None, args.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_list(holdings, header, mode="weight"), encoding="utf-8")

    print(f"converted {source.name} -> {path}")
    print(f"\n  {'TICKER':<8}{'WEIGHT':>9}{'ARS VALUE':>20}")
    for row in keep:
        print(f"  {row.ticker:<8}{row.market_value / kept_total * 100:>8.2f}%"
              f"{row.market_value:>20,.2f}")
    for row in dropped:
        print(f"  {row.ticker:<8}{'dropped':>9}{row.market_value:>20,.2f}  {row.name}")
    print(f"\n  {len(keep)} holdings, {kept_total:,.2f} ARS of {total:,.2f}")
    print(f"\nReview it, then load it with:  load_list --name {args.name} --yes")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    """Load a holdings list as the portfolio: reset, then buy those quantities at market.

    Buys rather than writing the quantities straight into the database, so the book is built
    the same way a person would build it - through validation, the trade blotter and the
    watchlist auto-add. That also means it can run out of cash, which is why the cost is
    checked against the balance BEFORE the first order goes in: a rejection halfway through
    leaves a half-built portfolio, and the whole point of a list is to get a known one.
    """
    api = Api(args.base)
    wait_healthy(api)

    path = _list_path(args.file, args.name)
    if not path.is_file():
        raise ApiError(f"no list at {path} - run dump_list first, or write one by hand")

    try:
        holdings, mode, warnings = parse_list(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ApiError(f"{path}: {exc}") from exc
    for warning in warnings:
        print(f"  ! {warning}")
    if not holdings:
        raise ApiError(f"{path} has no usable 'TICKER QTY' lines")

    if args.dry_run:
        print("dry run: nothing will be written")
    elif not args.no_reset:
        confirm(f"Load {path.name} over the current portfolio? It resets to $10,000 and "
                f"buys {len(holdings)} holdings.", args.yes)
        api.post("/api/portfolio/reset")
        print("reset: $10,000 cash, seed watchlist")

    watched = {entry["ticker"] for entry in api.get("/api/watchlist")["tickers"]}
    for ticker, _ in holdings:
        if ticker in watched:
            continue
        if args.dry_run:
            continue                    # reported in one line below, not once per ticker
        # Also what gives it a price to fill at (PLAN.md §6).
        api.post("/api/watchlist", {"ticker": ticker})

    prices = watchlist_prices(api)
    state = api.get("/api/portfolio")
    cash = float(state["cash_balance"])

    # A dry run skips the reset, so the balance sitting there now is not the balance a real
    # run would spend. Checking against it reports a shortfall that would never happen and
    # sends the user off scaling quantities down for no reason.
    resets = not args.no_reset
    if args.dry_run and resets:
        cash = float(state.get("starting_cash", cash))

    # Drop the unpriceable FIRST, then renormalise over what is left. Sizing against the
    # original weights and dropping afterwards silently under-invests by the dropped weight
    # - a list where a third of the names cannot be priced would quietly deploy two thirds
    # of the cash and look like it worked.
    priceable = [(t, a) for t, a in holdings if prices.get(t)]
    missing = [t for t, _ in holdings if not prices.get(t)]
    if missing:
        note = ("not on the watchlist yet, so they are unpriced in a dry run - a real run "
                "adds and buys them" if args.dry_run else "no price available - skipped")
        print(f"  ! {len(missing)} of {len(holdings)}: {note}")
        print(f"    {', '.join(missing)}")

    # Weights are sized against the cash this run will actually have; share counts are taken
    # literally. `--invest` leaves a sliver unspent so the last buy does not race an upward
    # tick, which only means anything for the weighted form.
    budget = cash * args.invest
    total_weight = sum(amount for _, amount in priceable) if mode == "weight" else 0.0

    orders: list[Order] = []
    for ticker, amount in priceable:
        price = prices[ticker]
        if mode == "weight":
            # Renormalised, so a list summing to 98% or 103% still means "these
            # proportions" rather than silently under- or over-investing.
            share = amount / total_weight if total_weight > 0 else 0.0
            scale = 10 ** QUANTITY_DP
            quantity = math.floor(budget * share / price * scale) / scale
            if quantity <= 0:
                print(f"  ! {ticker}: {share:.2%} of ${budget:,.2f} buys less than "
                      f"{1 / scale:g} shares - skipped")
                continue
        else:
            quantity = amount
        orders.append(Order(ticker, "buy", quantity, quantity * price))

    cost = sum(order.notional for order in orders)
    basis = " after the reset" if resets else ""
    print(f"\n{path.name}: {len(orders)} holdings costing ${cost:,.2f} "
          f"of ${cash:,.2f} available{basis}")

    if cost > cash:
        hint = ("" if resets else
                " (or drop --no-reset, which restores the starting balance first)")
        raise ApiError(
            f"that list costs ${cost:,.2f} but only ${cash:,.2f} is available - "
            f"${cost - cash:,.2f} short. Scale every quantity to about "
            f"{cash / cost:.0%} of its current value and try again{hint}."
        )

    if args.dry_run:
        for order in orders:
            print(f"  would buy {order.quantity:>12,.4f} {order.ticker:<6} "
                  f"~ ${order.notional:>12,.2f}")
        return 0

    print()
    place(api, orders)
    state = report(api)

    if args.json:
        print(json.dumps({
            "file": str(path), "orders": [order.__dict__ for order in orders],
            "warnings": warnings, "portfolio": state,
        }, indent=2))
    return 0


def cmd_save(args: argparse.Namespace) -> int:
    api = Api(args.base)
    wait_healthy(api)

    document = api.get("/api/session")
    path = _session_path(args.file, args.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    meta = document.get("meta", {})
    print(f"saved {path}")
    print(f"  {len(document['positions'])} positions, "
          f"{len(document['watchlist'])} watched, "
          f"cash ${document['cash_balance']:,.2f}, "
          f"total ${meta.get('total_value', 0):,.2f} ({meta.get('mode', '?')})")
    if not meta.get("all_priced", True):
        print("  ! some positions had no live price when this was saved; "
              "their market values are estimates, but quantities and costs are exact")
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    api = Api(args.base)
    wait_healthy(api)

    path = _session_path(args.file, args.name)
    if not path.is_file():
        raise ApiError(f"no session file at {path}")
    document = json.loads(path.read_text(encoding="utf-8"))

    confirm(f"Restore {path.name} over the current portfolio? Positions, trades and P&L "
            f"history will be replaced (chat history is kept).", args.yes)

    result = api.post("/api/session", document)
    loaded = result["loaded"]
    print(f"loaded {path}: {loaded['positions']} positions, "
          f"{loaded['watchlist']} watched")
    for ticker in result.get("unpriced", []):
        print(f"  ! {ticker} has no live price yet - valued at cost until the next tick")

    report(api)
    if args.json:
        print(json.dumps(result, indent=2))
    return 0


def _session_path(file: str | None, name: str) -> Path:
    return Path(file) if file else DEFAULT_SESSION_DIR / f"{name}.json"


def _list_path(file: str | None, name: str) -> Path:
    return Path(file) if file else DEFAULT_LIST_DIR / f"{name}.txt"


# ---- CLI ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    # Shared flags live on a parent parser rather than the top level, so they work AFTER
    # the subcommand - `... equal --yes`, which is how the shell wrappers pass them
    # through. Defined only at the top level, argparse rejects that ordering.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base", default=DEFAULT_BASE,
                        help=f"app base URL (default {DEFAULT_BASE})")
    common.add_argument("--json", action="store_true",
                        help="also print a machine-readable summary, for the E2E")
    common.add_argument("--yes", "-y", action="store_true",
                        help="skip the confirmation prompt (required non-interactively)")

    parser = argparse.ArgumentParser(
        prog="portfolio_tool.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (("equal", "equal dollar weight across the tickers"),
                            ("random", "a reproducible, lopsided random portfolio")):
        seed_parser = sub.add_parser(name, help=help_text, parents=[common])
        seed_parser.add_argument("--tickers", help="comma-separated; default the watchlist")
        seed_parser.add_argument("--count", type=int,
                                 help="random mode: how many watchlist names to pick")
        seed_parser.add_argument("--invest", type=float, default=0.95,
                                 help="fraction of cash to deploy (default 0.95 - a 1.00 "
                                      "target races an upward tick on the last buy)")
        # Fixed, not entropy: a harness that seeds a different book every run cannot
        # assert on anything but tautologies.
        seed_parser.add_argument("--seed", type=int, default=42, help="RNG seed")
        seed_parser.add_argument("--concentration", type=float, default=0.6,
                                 help="random mode Dirichlet alpha; <1 is lopsided")
        seed_parser.add_argument("--no-reset", action="store_true",
                                 help="build on top of the current portfolio")
        seed_parser.add_argument("--dry-run", action="store_true",
                                 help="print the plan, send no writes")
        seed_parser.set_defaults(func=cmd_seed, mode=name)

    save_parser = sub.add_parser("save", help="write the current session to JSON",
                                 parents=[common])
    save_parser.add_argument("--file", help="explicit path")
    save_parser.add_argument("--name", default="default",
                             help=f"session name under {DEFAULT_SESSION_DIR} "
                                  f"(default 'default')")
    save_parser.set_defaults(func=cmd_save)

    dump_parser = sub.add_parser(
        "dump", help="write the current holdings to an editable TICKER QTY list",
        parents=[common])
    dump_parser.add_argument("--file", help="explicit path")
    dump_parser.add_argument("--name", default="portfolio",
                             help=f"list name under {DEFAULT_LIST_DIR} (default 'portfolio')")
    dump_parser.set_defaults(func=cmd_dump)

    broker_parser = sub.add_parser(
        "broker", help="convert a broker holdings export into a weights list",
        parents=[common])
    broker_parser.add_argument("--source", default="sugested",
                               help=f"export name under {DEFAULT_LIST_DIR} "
                                    f"(default 'sugested')")
    broker_parser.add_argument("--file", help="explicit path to the export")
    broker_parser.add_argument("--name", default="broker",
                               help="name for the list to write (default 'broker')")
    broker_parser.add_argument("--keep-local", action="store_true",
                               help="keep locally-listed rows that have no US ticker")
    broker_parser.set_defaults(func=cmd_broker)

    build_parser = sub.add_parser(
        "build", help="reset, then buy the holdings in a TICKER QTY list", parents=[common])
    build_parser.add_argument("--file", help="explicit path")
    build_parser.add_argument("--name", default="portfolio", help="list name")
    build_parser.add_argument("--no-reset", action="store_true",
                              help="buy on top of the current portfolio")
    build_parser.add_argument("--dry-run", action="store_true",
                              help="print the plan, send no writes")
    build_parser.add_argument("--invest", type=float, default=0.95,
                              help="fraction of cash to deploy for a WEIGHTS list "
                                   "(default 0.95; ignored for share counts)")
    build_parser.set_defaults(func=cmd_build)

    load_parser = sub.add_parser("load", help="restore a saved session", parents=[common])
    load_parser.add_argument("--file", help="explicit path")
    load_parser.add_argument("--name", default="default", help="session name")
    load_parser.set_defaults(func=cmd_load)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
