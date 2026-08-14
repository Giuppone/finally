"""Schema definition and seed data.

Deliberately NOT `backend/db/`: the backend is copied to `/app` in the image, so a
`backend/db/` would land on `/app/db` — exactly where the SQLite volume mounts — and be
shadowed at runtime, leaving init with no schema to apply (PLAN.md §4).
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path

from app.market import SEED_WATCHLIST

from .. import clock

log = logging.getLogger(__name__)

SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

DEFAULT_USER = "default"
STARTING_CASH = 10000.0


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema and seed defaults. Idempotent.

    Runs in the lifespan startup hook, never on first request: the market service needs
    the watchlist to know what to track, and it starts during startup too (Review.md D3).
    Doing it here also sidesteps the two-concurrent-first-requests race lazy init needs a
    lock for.
    """
    conn.executescript(SCHEMA_SQL)
    seed_defaults(conn)
    conn.commit()


def seed_defaults(conn: sqlite3.Connection, user_id: str = DEFAULT_USER, *,
                  force: bool = False) -> bool:
    """Insert the default profile and watchlist. Returns True if the watchlist was seeded.

    Seeding is keyed to "this database is brand new", detected by whether the profile row
    had to be created — NOT to "the watchlist is currently empty". A user who deliberately
    removes all ten tickers would otherwise get them all back on the next restart, forever.
    The profile row is created once and never deleted (`reset` updates it in place), which
    is what makes it a reliable freshness marker.

    `force` is for `db.reset`, where restoring the seed watchlist is the whole point.
    """
    now = clock.now_iso()

    cursor = conn.execute(
        "INSERT OR IGNORE INTO users_profile (id, cash_balance, created_at) VALUES (?, ?, ?)",
        (user_id, STARTING_CASH, now),
    )
    if not (cursor.rowcount > 0 or force):
        return False

    conn.executemany(
        "INSERT OR IGNORE INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
        [(str(uuid.uuid4()), user_id, ticker, now) for ticker in SEED_WATCHLIST],
    )
    log.info("seeded watchlist: %s", ", ".join(SEED_WATCHLIST))
    return True
