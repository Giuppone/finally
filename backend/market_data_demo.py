"""FinAlly market data demo — a live terminal dashboard for the price engine.

Run with:  uv run market_data_demo.py            (from backend/)
           uv run market_data_demo.py --help

Drives the real MarketDataService with the simulated source and a static anchor
provider, so what you see is the same loop, cache and Quote objects the SSE endpoint
serves — no database, no network, no API key.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import random
import sys
import time
from collections import deque
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from app.market import (
    GBMEngine,
    MarketDataService,
    Mode,
    PriceCache,
    Quote,
    SEED_WATCHLIST,
    SimulatedSource,
    StaticAnchorProvider,
    build_market_service,
)

SPARK_CHARS = "▁▂▃▄▅▆▇█"
ASCII_SPARK_CHARS = "_.-~=+*#"

UP = "bold #2ecc8f"
DOWN = "bold #e05252"
FLAT = "dim"
ACCENT = "#ecad0a"
BLUE = "#209dd7"


class Glyphs:
    """Chosen once at startup. A legacy Windows console encodes cp1252 only, where the
    block-drawing sparkline and 'Δ' raise UnicodeEncodeError mid-render."""

    spark = SPARK_CHARS
    up, down, flat = "▲", "▼", "·"
    delta = "Δ"

    @classmethod
    def use_ascii(cls) -> None:
        cls.spark = ASCII_SPARK_CHARS
        cls.up, cls.down, cls.flat = "^", "v", "-"
        cls.delta = "d"


def setup_encoding(force_ascii: bool) -> bool:
    """Try to get UTF-8 out of stdout; fall back to ASCII glyphs if it will not encode.

    Returns True if unicode glyphs are safe to print.
    """
    if not force_ascii:
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8")
        try:
            (SPARK_CHARS + "Δ▲▼·").encode(sys.stdout.encoding or "ascii")
            return True
        except (UnicodeEncodeError, LookupError):
            pass
    Glyphs.use_ascii()
    return False


def sparkline(points: list[tuple[float, float]]) -> Text:
    """Render (ts, price) points as a sparkline, colored by net direction."""
    chars = Glyphs.spark
    if len(points) < 2:
        return Text(Glyphs.flat * 4, style=FLAT)
    values = [p for _, p in points]
    lo, hi = min(values), max(values)
    spread = hi - lo
    if spread <= 0:
        return Text(chars[3] * len(values), style=FLAT)
    n = len(chars) - 1
    glyphs = "".join(chars[int((v - lo) / spread * n)] for v in values)
    style = UP if values[-1] >= values[0] else DOWN
    return Text(glyphs, style=style)


def money(value: float) -> str:
    return f"{value:,.2f}"


def signed(value: float, places: int = 2) -> str:
    return f"{value:+,.{places}f}"


def quote_row(quote: Quote, history: list[tuple[float, float]], show_open: bool) -> list:
    """One table row. Tick delta drives the arrow; open_price drives the day columns."""
    style = {"up": UP, "down": DOWN, "flat": FLAT}[quote.direction]
    arrow = {"up": Glyphs.up, "down": Glyphs.down, "flat": Glyphs.flat}[quote.direction]

    day_style = UP if quote.day_change > 0 else DOWN if quote.day_change < 0 else FLAT

    row = [
        Text(quote.ticker, style="bold white"),
        Text(money(quote.price), style=style),
        Text(arrow, style=style),
        Text(signed(quote.change, 3), style=style),
        Text(signed(quote.day_change), style=day_style),
        Text(f"{quote.day_change_pct:+.2f}%", style=day_style),
    ]
    if show_open:
        row.append(Text(money(quote.open_price), style="dim"))
    row.append(sparkline(history))
    return row


def build_table(
    service: MarketDataService,
    cache: PriceCache,
    tickers: list[str],
    spark_width: int,
    show_open: bool,
) -> Table:
    table = Table(
        expand=True,
        border_style="grey35",
        header_style=f"bold {ACCENT}",
        pad_edge=False,
        padding=(0, 1),
    )
    table.add_column("TICKER", width=7, no_wrap=True)
    table.add_column("LAST", justify="right", width=10, no_wrap=True)
    table.add_column("", width=2, no_wrap=True)
    table.add_column(f"TICK {Glyphs.delta}", justify="right", width=9, no_wrap=True)
    table.add_column(f"DAY {Glyphs.delta}", justify="right", width=9, no_wrap=True)
    table.add_column("DAY %", justify="right", width=9, no_wrap=True)
    if show_open:
        table.add_column("OPEN", justify="right", width=10, no_wrap=True)
    table.add_column("SESSION", width=spark_width, no_wrap=True)

    for ticker in tickers:
        quote = service.quote(ticker)
        if quote is None:
            table.add_row(Text(ticker, style="bold white"), Text("--", style=FLAT))
            continue
        table.add_row(*quote_row(quote, cache.history(ticker, spark_width), show_open))

    return table


def build_header(service: MarketDataService, elapsed: float) -> Text:
    health = "healthy" if service.healthy else "DEGRADED"
    line = Text()
    line.append("FinAlly", style=f"bold {ACCENT}")
    line.append(" · ", style="dim")
    line.append(service.mode.value, style=BLUE)
    line.append(" · ", style="dim")
    line.append(health, style=UP if service.healthy else DOWN)
    line.append(" · ", style="dim")
    line.append(f"{elapsed:.0f}s", style="white")
    return line


def build_footer(quotes: list[Quote], errors: deque[str]) -> Text:
    if errors:
        return Text("\n".join(errors), style=DOWN)
    if not quotes:
        return Text("waiting for the first tick…", style=FLAT)

    up = sum(1 for q in quotes if q.day_change > 0)
    down = sum(1 for q in quotes if q.day_change < 0)
    avg = sum(q.day_change_pct for q in quotes) / len(quotes)
    avg_style = UP if avg > 0 else DOWN if avg < 0 else FLAT

    line = Text()
    line.append(f"{up} up", style=UP)
    line.append(" / ", style="dim")
    line.append(f"{down} down", style=DOWN)
    line.append("   basket ", style="dim")
    line.append(f"{avg:+.2f}%", style=avg_style)
    line.append("   Ctrl-C to stop", style="dim")
    return line


def load_project_env() -> str | None:
    """Cargar el .env de la raiz — SOLO para este demo.

    PLAN.md §5 es explicito: el backend lee os.environ y nunca parsea un .env; en Docker
    las variables llegan por --env-file. Esa regla sigue intacta porque nada de app/ llama
    a esta funcion. Un script de desarrollo lanzado a mano no tiene quien le inyecte el
    entorno, y pedirle al usuario que exporte la clave a mano para ver el modo real seria
    una traba sin proposito.

    Devuelve el path leido, o None si no habia .env.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return None
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # El entorno real gana: exportar la variable antes de correr debe poder pisar
        # el archivo, no al reves.
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    return str(env_path)


async def build_service(args: argparse.Namespace, cache: PriceCache) -> MarketDataService:
    """Simulador puro por defecto; con --live, el mismo factory que usa la app."""
    if args.live:
        load_project_env()
        # build_market_service sondea la entitlement de la clave y elige LIVE / ANCHORED /
        # SIMULATED. Documenta que nunca levanta excepcion, ni con una clave invalida.
        return await build_market_service(cache)

    engine = GBMEngine(seed=args.seed, tick_seconds=args.interval)
    return MarketDataService(
        source=SimulatedSource(engine, poll_interval=args.interval),
        # RNG sembrado: sin esto, un ticker fuera de SEED_PRICES arranca de un ancla
        # distinta en cada corrida y --seed deja de reproducir lo que promete.
        anchors=StaticAnchorProvider(random.Random(args.seed)),
        cache=cache,
        mode=Mode.SIMULATED,
    )


class ErrorCollector(logging.Handler):
    """Keeps log records out of the Live display and in a panel instead."""

    def __init__(self, sink: deque[str]) -> None:
        super().__init__(level=logging.WARNING)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self._sink.append(f"{record.levelname}: {record.getMessage()}")


async def run(args: argparse.Namespace) -> int:
    setup_encoding(args.ascii)
    console = Console()
    errors: deque[str] = deque(maxlen=3)

    logging.basicConfig(level=logging.CRITICAL, handlers=[])
    logging.getLogger("app").addHandler(ErrorCollector(errors))

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    cache = PriceCache()
    service = await build_service(args, cache)

    await service.start(set(tickers))
    started = time.monotonic()

    # Give SESSION exactly the space the fixed columns leave, so `expand=True` has no
    # surplus to spread and the sparkline gets it all. Overhead = content widths
    # (7+10+2+9+9+9 [+10]) + 2/col padding + per-column borders + the panel's 4.
    # OPEN is the first column to drop on a narrow terminal — it is the one value on the
    # row that never changes during a run.
    show_open = console.width >= 100
    overhead = 85 if show_open else 72
    spark_width = max(10, console.width - overhead)

    try:
        with Live(console=console, refresh_per_second=8, screen=False) as live:
            while True:
                elapsed = time.monotonic() - started
                if args.duration and elapsed >= args.duration:
                    break

                quotes = [q for q in (service.quote(t) for t in tickers) if q is not None]

                live.update(
                    Panel(
                        Group(
                            build_table(service, cache, tickers, spark_width, show_open),
                            Text(),
                            build_footer(quotes, errors),
                        ),
                        title=build_header(service, elapsed),
                        border_style="grey30",
                        padding=(0, 1),
                    )
                )
                await asyncio.sleep(args.interval / 2)
    except KeyboardInterrupt:
        pass
    finally:
        await service.stop()

    console.print(f"[dim]stopped after {time.monotonic() - started:.1f}s[/dim]")
    return 0


def positive_float(raw: str) -> float:
    """Rechazar 0 y negativos en el parseo del argumento.

    Sin esto, --interval 0 revienta con ZeroDivisionError y --interval -0.5 con
    'math domain error', ambos como traceback crudo antes de que arranque el dashboard.
    Un error de CLI se informa como error de CLI.
    """
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} no es un numero") from None
    if value <= 0 or value != value or value == float("inf"):
        raise argparse.ArgumentTypeError(f"debe ser mayor que cero, recibi {raw!r}")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="market_data_demo",
        description="Live terminal dashboard for the FinAlly simulated market data engine.",
    )
    parser.add_argument(
        "--duration", type=float, default=60.0,
        help="seconds to run; 0 runs until Ctrl-C (default: 60)",
    )
    parser.add_argument(
        "--tickers", default=",".join(SEED_WATCHLIST),
        help="comma-separated symbols (default: the PLAN.md seed watchlist)",
    )
    parser.add_argument(
        "--interval", type=positive_float, default=0.5,
        help="seconds between ticks, must be > 0 (default: 0.5)",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="RNG seed for a reproducible run (default: random)",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="use the real market-data factory: reads MASSIVE_API_KEY from the project "
             ".env, probes the key, and picks LIVE / ANCHORED / SIMULATED. Ignores "
             "--seed and --interval, which the factory owns.",
    )
    parser.add_argument(
        "--ascii", action="store_true",
        help="plain ASCII glyphs, for consoles that cannot encode block characters",
    )
    return parser.parse_args()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(asyncio.run(run(parse_args())))
