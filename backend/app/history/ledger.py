"""The dated brokerage ledger: parsing the raw export, and the canonical document.

STDLIB ONLY. `backend/scripts/portfolio_tool.py` imports this module directly under whatever
bare `python3` the host happens to have, so nothing here may import fastapi, pydantic, or any
module from `app.*` that does. See `app/history/__init__.py` for why.

Two number formats live one directory apart and look identical:

    ledger `Precio`      8610.0      -> plain dot-decimal, 8,610.0
    broker `sugested`    305.650     -> Argentine grouping, 305,650

`portfolio_tool.parse_amount` reads the second. Using it on the first understates a row by a
factor of a thousand, and the error is completely silent - so this module does NOT reuse it.
`test_history_ledger.py` asserts the two disagree, to stop a well-meaning unification.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DOCUMENT_VERSION = 1

# Deliberately NOT market.symbols.normalize_ticker, whose TICKER_RE is `[A-Z]{1,5}` - letters
# only. Half this ledger is Argentine instruments with digits (AL30, GD35, AE38, S29Y6,
# TGNO4), and the bond rows are what the whole FX derivation rests on. Rejecting them here
# would throw away the exchange rate before it could be measured.
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{0,11}$")

BUY, SELL = "buy", "sell"

# Income rows. The instruction was explicit: "Do not pay attention to dividends." They are
# counted and reported, never applied - a silent drop and a deliberate one look identical in
# the output otherwise.
IGNORED_KINDS = {
    "Dividendos Cash": "dividend",
    "Renta": "coupon",
    "Amortizacion": "amortisation",
}
_SIDES = {"Compra": BUY, "Venta": SELL}

_COLUMNS = ("Fecha", "Tipo", "Ticker", "Cantidad", "Moneda", "Precio", "Neto")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class LedgerError(ValueError):
    """The ledger could not be read at all - a missing column, an unknown transaction type."""


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One executed transaction, in the units the broker wrote it in.

    `quantity` is CEDEAR units, not US shares - that conversion needs a ratio, derived later
    in `reconstruct.py` because it needs price history this module does not have.
    """

    date: str          # ISO-8601 date
    kind: str          # "buy" | "sell"
    ticker: str
    quantity: float    # CEDEAR units
    currency: str      # "ARS" | "USD"
    price: float       # per CEDEAR, in `currency`
    net: float         # total consideration, in `currency`, fees included


@dataclass(frozen=True, slots=True)
class Holding:
    """A line from the broker's current-holdings export, in CEDEAR units and pesos."""

    quantity: float
    price_ars: float


@dataclass
class LedgerDocument:
    """The committed artifact at `backend/calibration/ledger.json`.

    It carries INPUTS, not conclusions. The exchange rate, the CEDEAR ratios and the equity
    curve are all recomputed at runtime, because every one of them is a function of
    `calibration/bars.json` - which grows. Freezing them here would pin the curve's right edge
    to whatever day this file was generated, which is the opposite of the point.

    `opening` and `snapshot` are the exceptions and must be baked: both derive from the
    broker's current-holdings export, and that file is not in the container image.
    """

    rows: list[LedgerRow] = field(default_factory=list)
    opening: dict[str, float] = field(default_factory=dict)
    snapshot_date: str = ""
    snapshot: dict[str, Holding] = field(default_factory=dict)
    ignored: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    version: int = DOCUMENT_VERSION
    generated_at: str = ""
    source: str = ""

    @property
    def start_date(self) -> str:
        return self.rows[0].date if self.rows else ""

    @property
    def end_date(self) -> str:
        return self.rows[-1].date if self.rows else ""


def normalize_symbol(raw: str) -> str:
    ticker = (raw or "").strip().upper()
    if not SYMBOL_RE.match(ticker):
        raise LedgerError(f"invalid ticker: {raw!r}")
    return ticker


def _number(text: str, what: str) -> float:
    try:
        return float((text or "").strip())
    except ValueError as exc:
        raise LedgerError(f"{what}: {text!r} is not a number") from exc


def parse_ledger(text: str) -> tuple[list[LedgerRow], list[dict], list[str]]:
    """Parse the tab-separated broker transaction export.

    Returns `(rows, ignored, warnings)` - the same "report and skip, never fatal" contract as
    `parse_broker` and `parse_list` in portfolio_tool.py. One unreadable line must not throw
    away the other hundred.

    An unknown transaction *type* is the exception and is fatal: silently dropping a whole
    category of transaction misstates the book with no visible symptom anywhere.
    """
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        raise LedgerError("the ledger is empty")

    header = [cell.strip() for cell in lines[0].split("\t")]
    missing = [column for column in _COLUMNS if column not in header]
    if missing:
        raise LedgerError(
            f"ledger is missing column(s) {', '.join(missing)} - expected {', '.join(_COLUMNS)}"
        )
    index = {name: header.index(name) for name in _COLUMNS}

    rows: list[LedgerRow] = []
    ignored: list[dict] = []
    warnings: list[str] = []

    for number, line in enumerate(lines[1:], start=2):
        cells = line.split("\t")
        if len(cells) < len(header):
            warnings.append(
                f"line {number}: {len(cells)} columns, expected {len(header)} - skipped"
            )
            continue

        def cell(name: str, _cells: list[str] = cells) -> str:
            return _cells[index[name]].strip()

        kind = cell("Tipo")
        if kind in IGNORED_KINDS:
            ignored.append({
                "date": cell("Fecha"), "kind": kind, "ticker": cell("Ticker"),
                "net": cell("Neto"), "currency": cell("Moneda"),
                "category": IGNORED_KINDS[kind],
            })
            continue
        if kind not in _SIDES:
            raise LedgerError(
                f"line {number}: unknown transaction type {kind!r}. Add it to _SIDES if it "
                f"moves a position, or to IGNORED_KINDS if it does not - guessing here "
                f"silently misstates the book."
            )

        try:
            date = cell("Fecha")
            if not _DATE_RE.match(date):
                raise LedgerError(f"line {number}: {date!r} is not an ISO date")
            currency = cell("Moneda").upper()
            if currency not in ("ARS", "USD"):
                raise LedgerError(f"line {number}: unknown currency {currency!r}")
            row = LedgerRow(
                date=date,
                kind=_SIDES[kind],
                ticker=normalize_symbol(cell("Ticker")),
                quantity=_number(cell("Cantidad"), f"line {number} Cantidad"),
                currency=currency,
                price=_number(cell("Precio"), f"line {number} Precio"),
                net=_number(cell("Neto"), f"line {number} Neto"),
            )
        except LedgerError as exc:
            warnings.append(f"{exc} - skipped")
            continue

        if row.quantity <= 0:
            warnings.append(f"line {number}: {row.ticker} quantity {row.quantity:g} - skipped")
            continue
        rows.append(row)

    rows.sort(key=lambda r: (r.date, r.ticker))
    return rows, ignored, warnings


def net_flow(rows: list[LedgerRow]) -> dict[str, float]:
    """Signed CEDEAR units moved per ticker across the whole window."""
    flow: dict[str, float] = {}
    for row in rows:
        flow[row.ticker] = flow.get(row.ticker, 0.0) + (
            row.quantity if row.kind == BUY else -row.quantity
        )
    return flow


def back_solve_openings(
    current: dict[str, float], rows: list[LedgerRow]
) -> tuple[dict[str, float], list[str]]:
    """What the account held the day before the ledger starts.

    `opening = current_holdings - net_ledger_flow`. The export carries no opening balance, so
    without this every name sold-but-never-bought inside the window goes negative and the
    curve is nonsense.

    That every opening on the real pair of files solves non-negative is the strongest evidence
    available that the ledger and the holdings export describe the same account. A negative
    result is therefore a genuine inconsistency between two files, not a rounding artefact -
    so it is reported. Clamping silently produces a plausible-looking curve that is wrong.
    """
    flow = net_flow(rows)
    openings: dict[str, float] = {}
    warnings: list[str] = []
    for ticker in sorted(set(current) | set(flow)):
        opening = current.get(ticker, 0.0) - flow.get(ticker, 0.0)
        if opening < -1e-6:
            warnings.append(
                f"{ticker}: opening position solves to {opening:,.2f} - the ledger sells more "
                f"than the holdings file accounts for. Carried as 0; check both files."
            )
            opening = 0.0
        openings[ticker] = max(0.0, opening)
    return openings, warnings


def build_document(
    text: str,
    holdings: dict[str, Holding],
    *,
    snapshot_date: str,
    source: str = "",
) -> LedgerDocument:
    """Raw export plus current holdings -> the canonical document.

    `holdings` comes from the broker's positions export, parsed by the caller - the script has
    `portfolio_tool.parse_broker` for that and this package must not import it (`scripts/` is
    excluded from the wheel by `packages = ["app"]`, so `app/` importing it only works by cwd
    accident).
    """
    rows, ignored, warnings = parse_ledger(text)
    current = {ticker: holding.quantity for ticker, holding in holdings.items()}
    openings, opening_warnings = back_solve_openings(current, rows)
    return LedgerDocument(
        rows=rows,
        opening={t: q for t, q in openings.items() if q > 0},
        snapshot_date=snapshot_date,
        snapshot=dict(holdings),
        ignored=ignored,
        warnings=warnings + opening_warnings,
        generated_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        source=source,
    )


# ---- the committed document ------------------------------------------------------

def default_path() -> Path:
    """`backend/calibration/ledger.json`, beside bars.json.

    `parents[2]` is correct in BOTH layouts, which is the only reason this works unchanged in
    a checkout and in the image: from `backend/app/history/ledger.py` it resolves to
    `backend/calibration/`, and from `/app/app/history/ledger.py` to `/app/calibration/` -
    because the Dockerfile flattens `backend/` onto `/app`. Same trick as `db.db_path()`.
    """
    return Path(__file__).resolve().parents[2] / "calibration" / "ledger.json"


def to_json(document: LedgerDocument) -> str:
    payload = {
        "version": document.version,
        "generated_at": document.generated_at
        or datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "source": document.source,
        "start_date": document.start_date,
        "end_date": document.end_date,
        "snapshot_date": document.snapshot_date,
        "opening": {t: round(q, 6) for t, q in sorted(document.opening.items())},
        "snapshot": {t: asdict(h) for t, h in sorted(document.snapshot.items())},
        "rows": [asdict(row) for row in document.rows],
        "ignored": document.ignored,
        "warnings": document.warnings,
    }
    return json.dumps(payload, indent=1) + "\n"


def from_payload(payload: dict) -> LedgerDocument:
    version = int(payload.get("version", 0))
    if version != DOCUMENT_VERSION:
        raise LedgerError(
            f"ledger.json is version {version}; this build reads {DOCUMENT_VERSION}. "
            f"Re-run import_broker_with_dates."
        )
    return LedgerDocument(
        rows=[LedgerRow(**row) for row in payload.get("rows", [])],
        opening={str(k): float(v) for k, v in (payload.get("opening") or {}).items()},
        snapshot_date=str(payload.get("snapshot_date", "")),
        snapshot={
            str(k): Holding(quantity=float(v["quantity"]), price_ars=float(v["price_ars"]))
            for k, v in (payload.get("snapshot") or {}).items()
        },
        ignored=list(payload.get("ignored") or []),
        warnings=list(payload.get("warnings") or []),
        version=version,
        generated_at=str(payload.get("generated_at", "")),
        source=str(payload.get("source", "")),
    )


def load_document(path: Path | None = None) -> LedgerDocument | None:
    """The document, or None when it has not been generated.

    None is a normal state, not an error: a fresh checkout has never run
    `import_broker_with_dates`, and the app must still boot and serve everything else.
    """
    target = path or default_path()
    if not target.is_file():
        return None
    return from_payload(json.loads(target.read_text(encoding="utf-8")))
