"""
SQLite schema and connection helper for the News Bot app.

One file, `news.db`, holds everything: configured sources (websites and
newsletters), every article ever scraped (for dedup + favorites/folders to
reference), folders, favorited articles, newsletter-digest email subscribers,
and app settings (digest time, timezone, Discord channel).
"""

import os
import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("news_bot.db")

DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).resolve().parent / "news.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('rss', 'css')),
    category TEXT NOT NULL DEFAULT 'website' CHECK(category IN ('website', 'newsletter', 'social', 'job')),
    url TEXT NOT NULL,
    article_selector TEXT,
    title_selector TEXT,
    link_selector TEXT,
    base_url TEXT,
    date_selector TEXT,
    date_year_selector TEXT,
    deadline_selector TEXT,
    forced_job_tag TEXT,
    image_selector TEXT,
    user_agent TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_checked_at TEXT,
    last_status TEXT,
    last_error TEXT,
    group_tag TEXT
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
    content_tag TEXT,
    deadline TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    excerpt TEXT
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

CREATE TABLE IF NOT EXISTS job_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    keywords TEXT NOT NULL DEFAULT '[]',
    sort_order INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# User-editable keyword -> tag classification for job/thesis offers (title
# match, same "first tag whose keyword list matches wins" idea as the
# physics content_tag system -- but user-configurable via the Settings tab
# rather than a fixed Python list, since job-offer "type" isn't a fixed
# taxonomy the way physics topics are). Seeded once, same DEFAULT_SETTINGS
# pattern; editable/deletable afterward, never re-seeded once the table has
# any row at all. List order here is seed *and* default sort_order --
# classify_job() checks tags in sort_order, so a tag earlier in this list
# wins ties against one later in it.
#
# Post-Doc is deliberately first: real posting titles use "Post-doctorant"
# (LPSC) and "Postdoctoral Researcher" (ALICE) -- both would otherwise get
# mis-tagged PhD, since "doctorant" is a real substring/word inside
# "Post-doctorant" (a hyphen is a word boundary, so \bdoctorant\b still
# matches there -- this is a genuine language collision word-boundary
# matching alone can't resolve, see classify_job()'s docstring) and
# "doctoral" is a substring of "Postdoctoral". Checking Post-Doc's own
# keywords *before* PhD's fixes both at the ordering level instead.
DEFAULT_JOB_TAGS = [
    ("Post-Doc", [
        "postdoc", "post-doc", "postdoctoral", "post-doctoral",
        "postdoctorant", "post-doctorant", "postdoctorat", "post-doctorat",
    ]),
    ("Fellowship", ["fellowship", "fellow"]),
    ("PhD", ["phd", "doctorant", "thèse", "these", "thesis"]),
    ("Internship", ["internship", "intern", "stage", "stagiaire"]),
    ("Professorship", ["professor", "professorship", "professeur", "chair", "faculty"]),
    ("Outreach", ["outreach", "médiation", "mediation", "communication", "vulgarisation"]),
    ("Physicist", ["physicist", "physicien"]),
]

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
    ("sources", "group_tag", "TEXT"),
    ("sources", "date_year_selector", "TEXT"),
    ("sources", "deadline_selector", "TEXT"),
    ("articles", "deadline", "TEXT"),
    ("articles", "job_tag", "TEXT"),
    ("job_tags", "sort_order", "INTEGER"),
    ("sources", "forced_job_tag", "TEXT"),
    ("articles", "archived", "INTEGER NOT NULL DEFAULT 0"),
    ("articles", "excerpt", "TEXT"),
]


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, column, col_type in COLUMN_MIGRATIONS:
        existing_cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing_cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def _recreate_sources_table(conn: sqlite3.Connection) -> None:
    """Shared plumbing for a sources.category CHECK-constraint migration --
    SQLite can't ALTER a CHECK in place, so adding a new allowed category
    value needs a real recreate-and-copy, not just an ALTER TABLE ADD
    COLUMN. Used by both _ensure_social_category and _ensure_job_category.

    THE BUG THIS ONCE HAD, for the next table-rename migration: SQLite's
    default `legacy_alter_table=OFF` makes `ALTER TABLE ... RENAME` also
    rewrite the *stored schema text* of every other table's foreign keys
    that reference it -- `articles.source_id REFERENCES sources(id)`
    silently became `REFERENCES "sources_old"(id)` the moment `sources` was
    renamed here, surviving the rename back to `sources` because nothing
    ever touched `articles`'s own schema again. `PRAGMA foreign_keys = OFF`
    (which this function already had) only suppresses *runtime enforcement*
    during the swap -- a completely different setting from
    `legacy_alter_table`, which controls whether the rename rewrites other
    tables' schema at all. The break was invisible until the first genuinely
    new row in a dependent table (every prior insert happened to be a
    duplicate, short-circuited by `INSERT OR IGNORE` before the broken FK
    was ever evaluated) -- caught only when a real new article finally hit
    `sqlite3.OperationalError: no such table: main.sources_old` in
    production. Fixed with `PRAGMA legacy_alter_table = ON` for the
    duration of the rename; verified by checking `articles`' own stored
    schema still says `REFERENCES sources(id)`, not just re-running the
    logical orphan-row check (`source_id NOT IN (SELECT id FROM sources)`),
    which stayed green the entire time this was broken since it checks
    actual values, not the broken schema text. Every future
    category-constraint bump should keep going through this same helper
    rather than hand-rolling the rename again."""
    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("ALTER TABLE sources RENAME TO sources_old")
    conn.executescript(SCHEMA)  # recreates `sources` under the current (new) constraint
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(sources_old)")]
    column_list = ", ".join(columns)
    conn.execute(f"INSERT INTO sources ({column_list}) SELECT {column_list} FROM sources_old")
    conn.execute("DROP TABLE sources_old")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA legacy_alter_table = OFF")


def _ensure_social_category(conn: sqlite3.Connection) -> None:
    """See _recreate_sources_table's docstring for the mechanics and the
    incident this pattern caused once. Detected by checking the table's own
    stored CREATE TABLE SQL for the new value; a no-op once applied. Run
    after _ensure_columns() so every column that might exist is already
    there to copy."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sources'").fetchone()
    if row is None or "'social'" in row["sql"]:
        return
    _recreate_sources_table(conn)


def _ensure_job_category(conn: sqlite3.Connection) -> None:
    """Same migration as _ensure_social_category, run again now that 'job'
    has been added to sources.category's allowed values (for the Jobs tab's
    job/thesis-offer listing sources). Run after _ensure_social_category so
    a DB that's never seen either migration only pays for one actual
    rename-recreate-copy, not two -- by the time this checks, the table
    already has 'social' (or was created fresh with both already present),
    so this only does real work for a DB that had 'social' but not 'job'
    yet."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sources'").fetchone()
    if row is None or "'job'" in row["sql"]:
        return
    _recreate_sources_table(conn)


def _check_foreign_key_integrity(conn: sqlite3.Connection) -> None:
    """Startup safety net, cheap to run every time: `PRAGMA foreign_key_check`
    catches a foreign key referencing a table that no longer exists (not
    just a dangling id value) -- exactly the failure mode a table-rename
    migration (see _ensure_social_category's docstring for the real
    incident) can silently introduce. The logical check this project used
    before adding this ("source_id NOT IN (SELECT id FROM sources)") stays
    green even when the *schema text* of a foreign key is broken, since it
    only compares values -- this pragma inspects the actual constraint
    definitions, which is what actually caught the incident on replay.
    Logs loudly (doesn't raise -- a corrupt FK doesn't stop the app from
    serving already-working data) so it surfaces immediately at next
    startup instead of waiting for a production INSERT to hit it."""
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        logger.error(
            f"Foreign key integrity check found {len(violations)} violation(s) -- "
            "likely a broken FK from a table-rename migration (see db.py's "
            "_ensure_social_category docstring for the pattern this bit once already). "
            f"First few: {violations[:5]}"
        )


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        _ensure_social_category(conn)
        _ensure_job_category(conn)
        conn.commit()
        existing = {row["key"] for row in conn.execute("SELECT key FROM settings")}
        for key, value in DEFAULT_SETTINGS.items():
            if key not in existing:
                env_value = os.environ.get(key.upper(), value)
                conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, env_value))
        # Seed only when the table is genuinely empty -- never re-seeded once
        # the user has added/edited/deleted a single tag, same "seed once,
        # then hands off to the user" contract as every other seeded table
        # in this app (SUGGESTED_CATEGORIES-style, not a re-applied default).
        if conn.execute("SELECT COUNT(*) AS n FROM job_tags").fetchone()["n"] == 0:
            conn.executemany(
                "INSERT INTO job_tags (name, keywords, sort_order) VALUES (?, ?, ?)",
                [(name, json.dumps(keywords), i) for i, (name, keywords) in enumerate(DEFAULT_JOB_TAGS)],
            )
        # Backfill for installs that seeded job_tags before sort_order
        # existed -- defaults to id, preserving whatever order those rows
        # already had (classify_job() used id order before this column).
        conn.execute("UPDATE job_tags SET sort_order = id WHERE sort_order IS NULL")
        conn.commit()
        _check_foreign_key_integrity(conn)
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
