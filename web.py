"""
FastAPI app: REST API for managing sources, browsing/favoriting articles,
folders, and newsletter-email subscribers, plus the static frontend.
Runs alongside the Discord bot in the same process (see main.py).
"""

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr

import db
import digest
import mailer
import scraper

logger = logging.getLogger("news_bot.web")

app = FastAPI(title="News Bot")

STATIC_DIR = Path(__file__).resolve().parent / "static"


# --------------------------------------------------------------------------
# Pydantic request bodies
# --------------------------------------------------------------------------

class SourceIn(BaseModel):
    name: str
    kind: str  # "rss" | "css"
    category: str = "website"  # "website" | "newsletter" | "social"
    url: str
    article_selector: str | None = None
    title_selector: str | None = None
    link_selector: str | None = None
    base_url: str | None = None
    date_selector: str | None = None
    image_selector: str | None = None
    user_agent: str | None = None
    enabled: bool = True
    group_tag: str | None = None  # free-text, user-defined -- orthogonal to category


class SourceUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None  # "rss" | "css"
    category: str | None = None  # "website" | "newsletter" | "social"
    url: str | None = None
    article_selector: str | None = None
    title_selector: str | None = None
    link_selector: str | None = None
    base_url: str | None = None
    date_selector: str | None = None
    image_selector: str | None = None
    user_agent: str | None = None
    enabled: bool | None = None
    group_tag: str | None = None


class DetectFeedIn(BaseModel):
    url: str


class FolderIn(BaseModel):
    name: str


class FavoriteIn(BaseModel):
    folder_id: int | None = None


class SubscribeIn(BaseModel):
    email: EmailStr


class SettingsUpdate(BaseModel):
    digest_hour: int | None = None
    digest_minute: int | None = None
    digest_timezone: str | None = None
    news_channel_id: int | None = None


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

def _source_row_to_dict(row) -> dict:
    return dict(row)


@app.get("/api/sources")
def list_sources():
    conn = db.get_conn()
    try:
        # SQLite-specific (not standard SQL) but well-documented behavior:
        # grouping with MAX() in the SELECT list guarantees the other,
        # non-aggregated columns come from the same row that produced the
        # MAX value -- exactly "this source's most recently published
        # article", in one query rather than a per-source round-trip.
        rows = conn.execute(
            "SELECT sources.*, latest.title AS latest_title, latest.link AS latest_link, "
            "latest.pub_date AS latest_pub_date FROM sources "
            "LEFT JOIN ("
            "  SELECT source_id, title, link, MAX(pub_date) AS pub_date FROM articles GROUP BY source_id"
            ") AS latest ON latest.source_id = sources.id "
            "ORDER BY sources.category, sources.name"
        ).fetchall()
        return [_source_row_to_dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/sources/detect-feed")
def detect_feed(body: DetectFeedIn):
    feed_url = scraper.detect_feed(body.url)
    return {"feed_url": feed_url}


@app.post("/api/sources")
def create_source(body: SourceIn):
    if body.kind not in ("rss", "css"):
        raise HTTPException(400, "kind must be 'rss' or 'css'")
    if body.category not in ("website", "newsletter", "social"):
        raise HTTPException(400, "category must be 'website', 'newsletter', or 'social'")
    if body.kind == "css":
        missing = [
            f for f in ("article_selector", "title_selector", "link_selector", "base_url")
            if not getattr(body, f)
        ]
        if missing:
            raise HTTPException(400, f"css sources require: {', '.join(missing)}")

    conn = db.get_conn()
    try:
        try:
            cur = conn.execute(
                "INSERT INTO sources (name, kind, category, url, article_selector, title_selector, "
                "link_selector, base_url, date_selector, image_selector, user_agent, enabled, group_tag) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    body.name, body.kind, body.category, body.url,
                    body.article_selector, body.title_selector, body.link_selector, body.base_url,
                    body.date_selector, body.image_selector, body.user_agent, int(body.enabled),
                    body.group_tag,
                ),
            )
            conn.commit()
        except db.sqlite3.IntegrityError:
            raise HTTPException(409, f"A source named '{body.name}' already exists")
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _source_row_to_dict(row)
    finally:
        conn.close()


@app.put("/api/sources/{source_id}")
def update_source(source_id: int, body: SourceUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "No fields to update")
    if "kind" in fields and fields["kind"] not in ("rss", "css"):
        raise HTTPException(400, "kind must be 'rss' or 'css'")
    if "category" in fields and fields["category"] not in ("website", "newsletter", "social"):
        raise HTTPException(400, "category must be 'website', 'newsletter', or 'social'")

    conn = db.get_conn()
    try:
        existing = conn.execute("SELECT id FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Source not found")
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values())
        if "enabled" in fields:
            values[list(fields.keys()).index("enabled")] = int(fields["enabled"])
        conn.execute(f"UPDATE sources SET {set_clause} WHERE id = ?", values + [source_id])
        conn.commit()
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return _source_row_to_dict(row)
    finally:
        conn.close()


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int):
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        conn.commit()
        return {"deleted": True}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Articles + favorites
# --------------------------------------------------------------------------

@app.get("/api/articles")
def list_articles(source: str | None = None, favorites_only: bool = False, folder_id: int | None = None):
    conn = db.get_conn()
    try:
        query = (
            "SELECT articles.*, sources.name AS source_name, sources.category AS source_category, "
            "sources.group_tag AS source_group, favorites.folder_id AS folder_id FROM articles "
            "JOIN sources ON sources.id = articles.source_id "
            "LEFT JOIN favorites ON favorites.article_id = articles.id"
        )
        clauses, params = [], []
        if source:
            clauses.append("sources.name = ?")
            params.append(source)
        if favorites_only:
            clauses.append("favorites.article_id IS NOT NULL")
        if folder_id is not None:
            clauses.append("favorites.folder_id = ?")
            params.append(folder_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY articles.pub_date DESC"
        rows = conn.execute(query, params).fetchall()
        results = [dict(r) for r in rows]
        # Belt-and-suspenders: scraper.backfill_content_tags() covers this at
        # startup, but a row could reach here NULL some other way (a restored
        # backup, direct DB edits) -- this endpoint reads raw rows, not
        # through Article, so it doesn't get Article.__init__'s fallback.
        for a in results:
            if not a["content_tag"]:
                a["content_tag"] = scraper.classify_article(a["title"])
        return results
    finally:
        conn.close()


@app.put("/api/articles/{article_id}/favorite")
def favorite_article(article_id: int, body: FavoriteIn):
    conn = db.get_conn()
    try:
        exists = conn.execute("SELECT id FROM articles WHERE id = ?", (article_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Article not found")
        conn.execute(
            "INSERT INTO favorites (article_id, folder_id) VALUES (?, ?) "
            "ON CONFLICT(article_id) DO UPDATE SET folder_id = excluded.folder_id",
            (article_id, body.folder_id),
        )
        conn.commit()
        return {"favorited": True, "folder_id": body.folder_id}
    finally:
        conn.close()


@app.delete("/api/articles/{article_id}/favorite")
def unfavorite_article(article_id: int):
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM favorites WHERE article_id = ?", (article_id,))
        conn.commit()
        return {"favorited": False}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Folders
# --------------------------------------------------------------------------

@app.get("/api/folders")
def list_folders():
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT folders.*, COUNT(favorites.article_id) AS article_count FROM folders "
            "LEFT JOIN favorites ON favorites.folder_id = folders.id "
            "GROUP BY folders.id ORDER BY folders.name"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/folders")
def create_folder(body: FolderIn):
    conn = db.get_conn()
    try:
        try:
            cur = conn.execute("INSERT INTO folders (name) VALUES (?)", (body.name,))
            conn.commit()
        except db.sqlite3.IntegrityError:
            raise HTTPException(409, f"A folder named '{body.name}' already exists")
        row = conn.execute("SELECT * FROM folders WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


@app.delete("/api/folders/{folder_id}")
def delete_folder(folder_id: int):
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        conn.commit()
        return {"deleted": True}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Newsletter (email digest subscription)
# --------------------------------------------------------------------------

@app.get("/api/newsletter/subscribers")
def list_subscribers():
    return {"subscribers": mailer.list_subscribers(), "smtp_configured": mailer.is_configured()}


@app.post("/api/newsletter/subscribe")
def subscribe(body: SubscribeIn):
    mailer.add_subscriber(body.email)
    return {"subscribed": body.email}


@app.delete("/api/newsletter/unsubscribe")
def unsubscribe(body: SubscribeIn):
    mailer.remove_subscriber(body.email)
    return {"unsubscribed": body.email}


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings():
    return {
        "digest_hour": int(db.get_setting("digest_hour", "8")),
        "digest_minute": int(db.get_setting("digest_minute", "0")),
        "digest_timezone": db.get_setting("digest_timezone", "Europe/Paris"),
        "news_channel_id": int(db.get_setting("news_channel_id", "0")),
    }


@app.put("/api/settings")
def update_settings(body: SettingsUpdate):
    fields = body.model_dump(exclude_none=True)
    for key, value in fields.items():
        db.set_setting(key, str(value))
    if fields:
        logger.info(
            f"Settings updated: {fields}. The digest schedule itself needs a restart to "
            "take effect (see scheduler.py)."
        )
    return get_settings()


# --------------------------------------------------------------------------
# Manual trigger + digest download
# --------------------------------------------------------------------------

@app.post("/api/run-now")
async def run_now():
    import scheduler  # lazy: avoids importing discord_bot until the app is fully up

    sent = await scheduler.run_daily_job()
    return {"sent": sent}


@app.get("/api/digest.html")
def download_digest():
    from fastapi.responses import HTMLResponse

    articles = scraper.get_articles()
    return HTMLResponse(digest.render_digest_html(articles))


# --------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
