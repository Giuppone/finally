"""SQLite access. One connection per operation; every call offloaded off the event loop.

PLAN.md §3 puts the SSE generators, the market-data loop and every request handler on one
event loop, so a blocking `sqlite3` call stalls the price stream for every connected
client (Review.md C1). `run()` is the only way this module touches the database.

Connection-per-operation rather than one shared connection: sharing would let two
concurrent `asyncio.to_thread` calls interleave their transactions on the same handle.
Opening an already-existing SQLite file costs microseconds, and WAL lets readers run
alongside the single writer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeVar

from . import clock
from .schema import DEFAULT_USER, STARTING_CASH, init_db

log = logging.getLogger(__name__)

T = TypeVar("T")

# Repo-root db/ locally; the container sets FINALLY_DB_PATH=/app/db/finally.db, which is
# where the volume mounts (PLAN.md §11).
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "db" / "finally.db"


def db_path() -> Path:
    override = os.environ.get("FINALLY_DB_PATH", "").strip()
    return Path(override) if override else DEFAULT_DB_PATH


@contextmanager
def connect():
    """Open a tuned connection. Callers run inside a worker thread, never on the loop."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")       # readers do not block the writer
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


async def run(fn: Callable[[sqlite3.Connection], T]) -> T:
    """Run `fn` against a fresh connection in a worker thread."""

    def work() -> T:
        with connect() as conn:
            return fn(conn)

    return await asyncio.to_thread(work)


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Explicit BEGIN/COMMIT. `isolation_level=None` disables sqlite3's implicit handling,
    so read-validate-write sequences are atomic exactly where they say they are."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


async def initialize() -> None:
    """Create the schema and seed defaults. Called once, in lifespan startup."""
    await run(init_db)
    log.info("database ready: %s", db_path())


# ---- queries ----------------------------------------------------------------

def tracked_tickers(conn: sqlite3.Connection, user_id: str = DEFAULT_USER) -> set[str]:
    """PLAN.md §6: watchlist ∪ open positions. Never the watchlist alone.

    A ticker removed from the watchlist while a position is open must keep updating, or
    portfolio value, the heatmap and the P&L chart all silently go stale (§13 item 4).
    """
    rows = conn.execute(
        """
        SELECT ticker FROM watchlist WHERE user_id = ?
        UNION
        SELECT ticker FROM positions WHERE user_id = ? AND quantity > 1e-9
        """,
        (user_id, user_id),
    ).fetchall()
    return {row["ticker"] for row in rows}


def cash_balance(conn: sqlite3.Connection, user_id: str = DEFAULT_USER) -> float:
    row = conn.execute(
        "SELECT cash_balance FROM users_profile WHERE id = ?", (user_id,)
    ).fetchone()
    return float(row["cash_balance"]) if row else 0.0


def positions(conn: sqlite3.Connection, user_id: str = DEFAULT_USER) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT ticker, quantity, avg_cost, updated_at FROM positions "
        "WHERE user_id = ? AND quantity > 1e-9 ORDER BY ticker",
        (user_id,),
    ).fetchall()


def position(conn: sqlite3.Connection, ticker: str,
             user_id: str = DEFAULT_USER) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT ticker, quantity, avg_cost FROM positions WHERE user_id = ? AND ticker = ?",
        (user_id, ticker),
    ).fetchone()


def watchlist(conn: sqlite3.Connection, user_id: str = DEFAULT_USER) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT ticker, added_at FROM watchlist WHERE user_id = ? ORDER BY ticker",
        (user_id,),
    ).fetchall()


def add_watchlist(conn: sqlite3.Connection, ticker: str,
                  user_id: str = DEFAULT_USER) -> bool:
    """True if a row was inserted, False if the ticker was already watched."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), user_id, ticker, clock.now_iso()),
    )
    return cursor.rowcount > 0


def remove_watchlist(conn: sqlite3.Connection, ticker: str,
                     user_id: str = DEFAULT_USER) -> bool:
    cursor = conn.execute(
        "DELETE FROM watchlist WHERE user_id = ? AND ticker = ?", (user_id, ticker)
    )
    return cursor.rowcount > 0


def record_snapshot(conn: sqlite3.Connection, total_value: float,
                    user_id: str = DEFAULT_USER) -> str:
    recorded_at = clock.now_iso()
    conn.execute(
        "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) "
        "VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), user_id, total_value, recorded_at),
    )
    return recorded_at


def last_snapshot(conn: sqlite3.Connection,
                  user_id: str = DEFAULT_USER) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT total_value, recorded_at FROM portfolio_snapshots WHERE user_id = ? "
        "ORDER BY recorded_at DESC, rowid DESC LIMIT 1",
        (user_id,),
    ).fetchone()


def snapshots(conn: sqlite3.Connection, since: str | None = None, limit: int = 500,
              user_id: str = DEFAULT_USER) -> list[sqlite3.Row]:
    """Newest `limit` rows at or after `since`, returned oldest-first for charting.

    Bounded on purpose: 30s snapshots are 2,880 rows/day and the P&L chart would otherwise
    grow forever and eventually plot a million points (Review.md C3).
    """
    if since:
        rows = conn.execute(
            "SELECT total_value, recorded_at FROM portfolio_snapshots "
            "WHERE user_id = ? AND recorded_at >= ? "
            "ORDER BY recorded_at DESC, rowid DESC LIMIT ?",
            (user_id, since, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT total_value, recorded_at FROM portfolio_snapshots WHERE user_id = ? "
            "ORDER BY recorded_at DESC, rowid DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return list(reversed(rows))


def add_chat_message(conn: sqlite3.Connection, role: str, content: str,
                     actions: str | None = None,
                     user_id: str = DEFAULT_USER) -> dict[str, Any]:
    """Append one turn. `actions` is a JSON string for assistant rows, null for user rows."""
    row = {
        "id": str(uuid.uuid4()),
        "role": role,
        "content": content,
        "actions": actions,
        "created_at": clock.now_iso(),
    }
    conn.execute(
        "INSERT INTO chat_messages (id, user_id, role, content, actions, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (row["id"], user_id, role, content, actions, row["created_at"]),
    )
    return row


def chat_messages(conn: sqlite3.Connection, limit: int = 50, days: int | None = 30,
                  user_id: str = DEFAULT_USER) -> list[sqlite3.Row]:
    """The `limit` most recent messages within `days`, returned oldest-first.

    Newest-first in SQL and reversed in Python, because "the 50 most recent" and "the first
    50" are different sets and only the former is wanted (PLAN.md §9 step 2). The window
    keeps a heavy chat session from inflating prompt size and cost without bound; it rarely
    binds in normal use. `days=None` lifts the age filter for the UI's history restore.
    """
    since = (
        clock.to_iso(clock.now() - timedelta(days=days)) if days is not None else None
    )
    if since:
        rows = conn.execute(
            "SELECT id, role, content, actions, created_at FROM chat_messages "
            "WHERE user_id = ? AND created_at >= ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (user_id, since, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, role, content, actions, created_at FROM chat_messages "
            "WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return list(reversed(rows))


def reset(conn: sqlite3.Connection, user_id: str = DEFAULT_USER) -> None:
    """Back to a fresh account: $10k cash, the seed watchlist, no positions or history.

    E2E needs it (the fresh-start scenario fails on the second run otherwise) and it is
    the escape hatch when a demo's LLM drains the account (Review.md B9).
    """
    from .schema import seed_defaults

    with transaction(conn):
        for table in ("positions", "trades", "portfolio_snapshots", "chat_messages",
                      "watchlist"):
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        conn.execute(
            "UPDATE users_profile SET cash_balance = ? WHERE id = ?",
            (STARTING_CASH, user_id),
        )
        seed_defaults(conn, user_id, force=True)   # restoring the seed watchlist is the point


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Health fragment: enough to tell 'schema applied' from 'empty file'."""
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    required = {"users_profile", "watchlist", "positions", "trades",
                "portfolio_snapshots", "chat_messages"}
    return {
        "ready": required <= tables,
        "path": str(db_path()),
        "missing_tables": sorted(required - tables),
    }
