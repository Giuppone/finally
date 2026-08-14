"""Time formatting for everything OUTSIDE the market layer.

Review.md B14 / design D14 fix one convention per layer:

* market wire (`/api/stream/prices`, `/api/prices/*`) — integer epoch milliseconds
* everywhere else (DB columns, `/api/portfolio`, `/api/chat`) — ISO 8601 UTC with `Z`

ISO strings are used in the DB because string ordering is then chronological ordering,
which every `ORDER BY recorded_at` in this codebase depends on.
"""

from __future__ import annotations

from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(tz=timezone.utc)


def now_iso() -> str:
    return to_iso(now())


def to_iso(moment: datetime) -> str:
    """ISO 8601 UTC, seconds precision, `Z` suffix — not `+00:00`."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
