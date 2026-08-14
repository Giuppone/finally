-- FinAlly schema — PLAN.md §7.
--
-- Every table carries user_id defaulting to 'default'. Single-user today; the column is
-- what lets multi-user arrive without a migration.
--
-- Ticker columns are COLLATE NOCASE as a backstop to normalize_ticker() (Review.md B1).
-- Without it SQLite's case-sensitive default makes `aapl` and `AAPL` two watchlist rows,
-- two positions in the same stock, and a heatmap showing one holding twice. The
-- application still normalises at every boundary — the cache and the GBM engine are plain
-- dicts and no collation reaches them.

CREATE TABLE IF NOT EXISTS users_profile (
    id           TEXT PRIMARY KEY DEFAULT 'default',
    cash_balance REAL NOT NULL DEFAULT 10000.0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    id       TEXT PRIMARY KEY,
    user_id  TEXT NOT NULL DEFAULT 'default',
    ticker   TEXT NOT NULL COLLATE NOCASE,
    added_at TEXT NOT NULL,
    UNIQUE (user_id, ticker)
);

CREATE TABLE IF NOT EXISTS positions (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL DEFAULT 'default',
    ticker     TEXT NOT NULL COLLATE NOCASE,
    quantity   REAL NOT NULL,
    avg_cost   REAL NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, ticker)
);

CREATE TABLE IF NOT EXISTS trades (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL DEFAULT 'default',
    ticker      TEXT NOT NULL COLLATE NOCASE,
    side        TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity    REAL NOT NULL,
    price       REAL NOT NULL,
    executed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL DEFAULT 'default',
    total_value REAL NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL DEFAULT 'default',
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    actions    TEXT,                     -- JSON; null for user messages
    created_at TEXT NOT NULL
);

-- recorded_at is ISO-8601 UTC, so string ordering IS chronological ordering (Review B14).
CREATE INDEX IF NOT EXISTS idx_snapshots_user_time
    ON portfolio_snapshots (user_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_trades_user_time
    ON trades (user_id, executed_at);
CREATE INDEX IF NOT EXISTS idx_chat_user_time
    ON chat_messages (user_id, created_at);
