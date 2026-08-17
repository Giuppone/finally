#!/usr/bin/env python3
"""Put the FinAlly portfolio into a known state, or save and restore one.

    portfolio_tool.py equal   [options]     equal-dollar-weight across the watchlist
    portfolio_tool.py random  [options]     a reproducible, deliberately lopsided book
    portfolio_tool.py save    [--file F]    write the current session to JSON
    portfolio_tool.py load    [--file F]    restore a saved session

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
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE = f"http://localhost:{os.environ.get('FINALLY_PORT', '8000')}"
DEFAULT_SESSION_DIR = Path(__file__).resolve().parents[2] / "sessions"

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
                    rng: random.Random) -> list[str]:
    """The watchlist, or `--tickers` (adding any that are not watched yet)."""
    watched = [entry["ticker"] for entry in api.get("/api/watchlist")["tickers"]]

    if requested:
        wanted = [t.strip().upper() for t in requested.split(",") if t.strip()]
        for ticker in wanted:
            if ticker not in watched:
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

    if not args.no_reset:
        confirm("Reset the portfolio to $10,000 and the seed watchlist? "
                "Positions, trades and P&L history will be deleted.", args.yes)
        api.post("/api/portfolio/reset")
        print("reset: $10,000 cash, seed watchlist")

    # A random book over all ten names is barely lopsided; six is enough to be visibly
    # concentrated and still worth diversifying.
    count = args.count if args.count is not None else (6 if args.mode == "random" else None)
    tickers = resolve_tickers(api, args.tickers, count, rng)
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
