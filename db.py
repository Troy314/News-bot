"""
SQLite schema and connection helper for the News Bot app.

One file, `news.db`, holds everything: configured sources (websites and
newsletters), every article ever scraped (for dedup + favorites/folders to
reference), folders, favorited articles, newsletter-digest email subscribers,
and app settings (digest time, timezone, Discord channel).
"""

import os
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).resolve().parent / "news.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('rss', 'css')),
    category TEXT NOT NULL DEFAULT 'website' CHECK(category IN ('website', 'newsletter', 'social')),
    url TEXT NOT NULL,
    article_selector TEXT,
    title_selector TEXT,
    link_selector TEXT,
    base_url TEXT,
    date_selector TEXT,
    image_selector TEXT,
    user_agent TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_checked_at TEXT,
    last_status TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    link TEXT NOT NULL UNIQUE,
    pub_date TEXT,
    image_url TEXT,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    notified INTEGER NOT NULL DEFAULT 0,
    content_tag TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source_id);
CREATE INDEX IF NOT EXISTS idx_articles_pub_date ON articles(pub_date);
CREATE INDEX IF NOT EXISTS idx_articles_notified ON articles(notified);

CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS favorites (
    article_id INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS newsletter_subscribers (
    email TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "digest_hour": "8",
    "digest_minute": "0",
    "digest_timezone": "Europe/Paris",
    "news_channel_id": "0",
}


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# (table, column, SQL type/default) -- columns added after a table's first
# release. CREATE TABLE IF NOT EXISTS is a no-op on an already-existing
# table, so new columns need an explicit ALTER TABLE migration every startup
# (harmless once the column already exists -- checked against PRAGMA
# table_info first).
COLUMN_MIGRATIONS = [
    ("sources", "last_checked_at", "TEXT"),
    ("sources", "last_status", "TEXT"),
    ("sources", "last_error", "TEXT"),
    ("articles", "content_tag", "TEXT"),
]


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, column, col_type in COLUMN_MIGRATIONS:
        existing_cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing_cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def _ensure_social_category(conn: sqlite3.Connection) -> None:
    """SQLite can't ALTER a CHECK constraint in place -- when 'social' was
    added to sources.category's allowed values, an existing sources table
    (created under the old two-value constraint) needs a real
    recreate-and-copy migration, not just an ALTER TABLE ADD COLUMN. Run
    after _ensure_columns() so every column that might exist is already
    there to copy. Detected by checking the table's own stored CREATE TABLE
    SQL for the new value; a no-op once applied."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sources'").fetchone()
    if row is None or "'social'" in row["sql"]:
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("ALTER TABLE sources RENAME TO sources_old")
    conn.executescript(SCHEMA)  # recreates `sources` under the current (new) constraint
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(sources_old)")]
    column_list = ", ".join(columns)
    conn.execute(f"INSERT INTO sources ({column_list}) SELECT {column_list} FROM sources_old")
    conn.execute("DROP TABLE sources_old")
    conn.execute("PRAGMA foreign_keys = ON")


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        _ensure_social_category(conn)
        conn.commit()
        existing = {row["key"] for row in conn.execute("SELECT key FROM settings")}
        for key, value in DEFAULT_SETTINGS.items():
            if key not in existing:
                env_value = os.environ.get(key.upper(), value)
                conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, env_value))
        conn.commit()
    finally:
        conn.close()


def get_setting(key: str, default: str | None = None) -> str | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key: str, value: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()
