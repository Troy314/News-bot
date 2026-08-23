"""
FastAPI app: REST API for managing sources, browsing/favoriting articles,
folders, and newsletter-email subscribers, plus the static frontend.
Runs alongside the Discord bot in the same process (see main.py).
"""

import ipaddress
import json
import logging
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
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
    category: str = "website"  # "website" | "newsletter" | "social" | "job"
    url: str
    article_selector: str | None = None
    title_selector: str | None = None
    link_selector: str | None = None
    base_url: str | None = None
    date_selector: str | None = None
    date_year_selector: str | None = None
    deadline_selector: str | None = None
    forced_job_tag: str | None = None  # job-category only: skip keyword classification, always use this tag
    image_selector: str | None = None
    user_agent: str | None = None
    enabled: bool = True
    group_tag: str | None = None  # free-text, user-defined -- orthogonal to category


class SourceUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None  # "rss" | "css"
    category: str | None = None  # "website" | "newsletter" | "social" | "job"
    url: str | None = None
    article_selector: str | None = None
    title_selector: str | None = None
    link_selector: str | None = None
    base_url: str | None = None
    date_selector: str | None = None
    date_year_selector: str | None = None
    deadline_selector: str | None = None
    forced_job_tag: str | None = None
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


class DeadlineIn(BaseModel):
    deadline: str | None = None  # "YYYY-MM-DD", or null/blank to clear


class SubscribeIn(BaseModel):
    email: EmailStr


class JobTagIn(BaseModel):
    name: str
    keywords: list[str] = []
    sort_order: int | None = None  # defaults to the next-highest (added last) if omitted


class JobTagUpdate(BaseModel):
    name: str | None = None
    keywords: list[str] | None = None
    sort_order: int | None = None


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


def _is_safe_external_url(url: str) -> bool:
    """SSRF guard for GET /api/sources/preview-html, the one endpoint in
    this app that fetches an arbitrary URL a caller supplies directly
    (not one already saved as a configured source, see that route's
    docstring) -- rejects anything that isn't plain http(s), and anything
    whose hostname resolves to a private/loopback/link-local/reserved/
    multicast address, the standard defense against this server being
    used as a proxy into its own internal network. Best-effort, not
    airtight: a DNS answer could still change between this check and the
    actual fetch a moment later (a fully airtight fix would need the HTTP
    client itself to validate the IP it connects to, which `requests`
    doesn't expose a hook for cheaply) -- but this stops the overwhelming
    majority of real SSRF attempts (a literal internal IP or a hostname
    like `localhost`/`*.internal`) for negligible cost, which is the
    right tradeoff for a personal tool that isn't behind any auth layer."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


@app.get("/api/sources/preview-html", response_class=HTMLResponse)
def preview_html(request: Request, url: str, user_agent: str | None = None):
    """Fetches a target page server-side (same reasoning as every other
    fetch in this app: avoids the browser's own CORS/X-Frame-Options
    restrictions entirely, since the response is then served from *our*
    origin, not the target site's) and returns it rewritten for safe
    point-and-click selector picking in a sandboxed iframe:
    - Every <script> tag is stripped, so the target site's own JS can't
      interfere with (or fight) the picker's hover/click handling.
    - A <base href> is injected so the page's own relative image/CSS URLs
      still resolve and it renders recognizably.
    - The picker script itself (static/picker-inject.js) is appended as an
      absolute-URL <script src>, pointed back at *this* server -- not a
      relative path, since the injected <base href> now points at the
      *target* site's origin, and a relative src would resolve against
      that instead of us.
    This app already fetches whatever URL a source is configured with, on
    every scheduled scrape (scraper.fetch_html) -- a single-user local
    tool that fetches user-typed URLs by design, not a public multi-tenant
    service. But *this* endpoint is reachable with an arbitrary URL the
    caller never has to actually save as a source first (it's called
    straight from the Add-source form, before anything is persisted), so
    it still gets the same SSRF guard as any endpoint that fetches a
    caller-supplied URL server-side would, as defense in depth against
    this server ever being reachable beyond localhost -- see
    _is_safe_external_url()."""
    if not _is_safe_external_url(url):
        raise HTTPException(400, "That URL isn't allowed (must be a public http/https address)")
    html = scraper.fetch_html(url, user_agent or scraper.USER_AGENT)
    if not html:
        raise HTTPException(502, f"Could not fetch {url}")

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script"):
        tag.decompose()

    head = soup.head
    if head is None:
        head = soup.new_tag("head")
        (soup.html or soup).insert(0, head)
    head.insert(0, soup.new_tag("base", href=url))

    picker_script = soup.new_tag("script", src=f"{request.base_url}static/picker-inject.js")
    (soup.body or soup).append(picker_script)

    return str(soup)


@app.post("/api/sources")
def create_source(body: SourceIn):
    if body.kind not in ("rss", "css"):
        raise HTTPException(400, "kind must be 'rss' or 'css'")
    if body.category not in ("website", "newsletter", "social", "job"):
        raise HTTPException(400, "category must be 'website', 'newsletter', 'social', or 'job'")
    if body.kind == "css":
        # title_selector/link_selector may legitimately be blank -- that means
        # "the article container itself is the title/link element" (some
        # listing pages have no wrapping element at all around a bare <a>,
        # e.g. Subatech's theses page: a plain <a> directly under <ul>, no
        # <li>, so there's nothing for a descendant selector to match). See
        # scrape_css's title_el/link_el fallback in scraper.py.
        missing = [f for f in ("article_selector", "base_url") if not getattr(body, f)]
        if missing:
            raise HTTPException(400, f"css sources require: {', '.join(missing)}")

    conn = db.get_conn()
    try:
        try:
            cur = conn.execute(
                "INSERT INTO sources (name, kind, category, url, article_selector, title_selector, "
                "link_selector, base_url, date_selector, date_year_selector, deadline_selector, "
                "forced_job_tag, image_selector, user_agent, enabled, group_tag) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    body.name, body.kind, body.category, body.url,
                    body.article_selector, body.title_selector, body.link_selector, body.base_url,
                    body.date_selector, body.date_year_selector, body.deadline_selector,
                    body.forced_job_tag, body.image_selector, body.user_agent,
                    int(body.enabled), body.group_tag,
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
    if "category" in fields and fields["category"] not in ("website", "newsletter", "social", "job"):
        raise HTTPException(400, "category must be 'website', 'newsletter', 'social', or 'job'")

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
def list_articles(
    source: str | None = None,
    category: str | None = None,
    exclude_category: str | None = None,
    favorites_only: bool = False,
    folder_id: int | None = None,
    include_archived: bool = False,
):
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
        if category:
            clauses.append("sources.category = ?")
            params.append(category)
        if exclude_category:
            clauses.append("sources.category != ?")
            params.append(exclude_category)
        if favorites_only:
            clauses.append("favorites.article_id IS NOT NULL")
        if folder_id is not None:
            clauses.append("favorites.folder_id = ?")
            params.append(folder_id)
        # Archived articles are hidden from ordinary browsing (Feed, Jobs'
        # main list) -- same "don't delete, just hide" shape already used
        # elsewhere in this app. Deliberately *not* applied to a
        # favorites/folder-scoped query: archiving declutters the main
        # list, it isn't meant to also un-favorite something you
        # explicitly starred -- the two are independent axes, an archived
        # article you'd favorited should still be reachable from the
        # Favorites tab. include_archived exists for completeness (a
        # future "show archived" view, manual inspection) but nothing in
        # the UI passes it yet.
        if not include_archived and not favorites_only and folder_id is None:
            clauses.append("articles.archived = 0")
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


@app.put("/api/articles/{article_id}/archive")
def archive_article(article_id: int):
    """Hides one job/thesis offer from the Jobs tab's normal browsing views
    (see list_articles' `archived` filtering) without deleting it -- for
    "I've seen this, applied, or it's no longer relevant" without losing
    the row. The frontend always confirms with the user before calling
    this (renderCard's archive button), so no undo flow is offered here
    the way this app's instant-delete-with-undo pattern would need one --
    a confirmed action doesn't need a second safety net, and unarchiving
    (below) already covers "I changed my mind" without a timer."""
    conn = db.get_conn()
    try:
        exists = conn.execute("SELECT id FROM articles WHERE id = ?", (article_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Article not found")
        conn.execute("UPDATE articles SET archived = 1 WHERE id = ?", (article_id,))
        conn.commit()
        return {"archived": True}
    finally:
        conn.close()


@app.delete("/api/articles/{article_id}/archive")
def unarchive_article(article_id: int):
    conn = db.get_conn()
    try:
        conn.execute("UPDATE articles SET archived = 0 WHERE id = ?", (article_id,))
        conn.commit()
        return {"archived": False}
    finally:
        conn.close()


@app.put("/api/articles/{article_id}/deadline")
def set_article_deadline(article_id: int, body: DeadlineIn):
    """Manual override for a job/thesis offer's application deadline --
    most sources don't expose one to scrape (see "Job deadlines" in
    CLAUDE.md), so this lets the user set/correct/clear one by hand from
    the Jobs tab. Not restricted to category="job" articles server-side
    (the UI only ever shows the control there, same "server doesn't need
    to re-enforce a UI-level restriction" reasoning as elsewhere in this
    app) -- storing a deadline on a news article is harmless, just unused."""
    conn = db.get_conn()
    try:
        exists = conn.execute("SELECT id FROM articles WHERE id = ?", (article_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Article not found")
        value = None
        if body.deadline:
            try:
                value = datetime.strptime(body.deadline, "%Y-%m-%d").replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                raise HTTPException(400, "deadline must be in YYYY-MM-DD format")
        conn.execute("UPDATE articles SET deadline = ? WHERE id = ?", (value, article_id))
        conn.commit()
        return {"deadline": value}
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
# Job tags (user-editable keyword -> tag classification for job/thesis
# offers -- see scraper.classify_job/get_job_tags/retag_jobs). Dex-
# independent the same way /api/newsletter is: this is app-wide
# configuration, not a per-source or per-article resource.
# --------------------------------------------------------------------------

def _job_tag_row_to_dict(row) -> dict:
    return {
        "id": row["id"], "name": row["name"], "keywords": json.loads(row["keywords"]),
        "sort_order": row["sort_order"],
    }


@app.get("/api/job-tags")
def list_job_tags():
    conn = db.get_conn()
    try:
        rows = conn.execute("SELECT * FROM job_tags ORDER BY sort_order, id").fetchall()
        return [_job_tag_row_to_dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/job-tags")
def create_job_tag(body: JobTagIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    keywords = [k.strip() for k in body.keywords if k.strip()]
    conn = db.get_conn()
    try:
        # Default a new tag to sorting *after* every existing one (checked
        # last -- least likely to accidentally preempt a more specific,
        # already-configured tag) unless the caller explicitly places it.
        sort_order = body.sort_order
        if sort_order is None:
            row = conn.execute("SELECT MAX(sort_order) AS m FROM job_tags").fetchone()
            sort_order = (row["m"] or 0) + 1
        try:
            cur = conn.execute(
                "INSERT INTO job_tags (name, keywords, sort_order) VALUES (?, ?, ?)",
                (name, json.dumps(keywords), sort_order),
            )
            conn.commit()
        except db.sqlite3.IntegrityError:
            raise HTTPException(409, f"A tag named '{name}' already exists")
        row = conn.execute("SELECT * FROM job_tags WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _job_tag_row_to_dict(row)
    finally:
        conn.close()


@app.put("/api/job-tags/{tag_id}")
def update_job_tag(tag_id: int, body: JobTagUpdate):
    conn = db.get_conn()
    try:
        existing = conn.execute("SELECT id FROM job_tags WHERE id = ?", (tag_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Tag not found")
        fields = {}
        if body.name is not None:
            name = body.name.strip()
            if not name:
                raise HTTPException(400, "name cannot be blank")
            fields["name"] = name
        if body.keywords is not None:
            fields["keywords"] = json.dumps([k.strip() for k in body.keywords if k.strip()])
        if body.sort_order is not None:
            fields["sort_order"] = body.sort_order
        if not fields:
            raise HTTPException(400, "No fields to update")
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        try:
            conn.execute(f"UPDATE job_tags SET {set_clause} WHERE id = ?", list(fields.values()) + [tag_id])
            conn.commit()
        except db.sqlite3.IntegrityError:
            raise HTTPException(409, f"A tag named '{fields.get('name')}' already exists")
        row = conn.execute("SELECT * FROM job_tags WHERE id = ?", (tag_id,)).fetchone()
        return _job_tag_row_to_dict(row)
    finally:
        conn.close()


@app.post("/api/job-tags/{tag_id}/move")
def move_job_tag(tag_id: int, direction: str):
    """Swaps this tag's sort_order with its immediate neighbor in the
    current order -- the Settings tab's move-up/move-down buttons, so
    reordering priority (e.g. "Post-Doc checked before PhD") doesn't need
    the frontend to know or compute any tag's raw sort_order value."""
    if direction not in ("up", "down"):
        raise HTTPException(400, "direction must be 'up' or 'down'")
    conn = db.get_conn()
    try:
        rows = conn.execute("SELECT id, sort_order FROM job_tags ORDER BY sort_order, id").fetchall()
        ids = [r["id"] for r in rows]
        if tag_id not in ids:
            raise HTTPException(404, "Tag not found")
        idx = ids.index(tag_id)
        swap_idx = idx - 1 if direction == "up" else idx + 1
        if 0 <= swap_idx < len(rows):
            a, b = rows[idx], rows[swap_idx]
            conn.execute("UPDATE job_tags SET sort_order = ? WHERE id = ?", (b["sort_order"], a["id"]))
            conn.execute("UPDATE job_tags SET sort_order = ? WHERE id = ?", (a["sort_order"], b["id"]))
            conn.commit()
        result = conn.execute("SELECT * FROM job_tags ORDER BY sort_order, id").fetchall()
        return [_job_tag_row_to_dict(r) for r in result]
    finally:
        conn.close()


@app.delete("/api/job-tags/{tag_id}")
def delete_job_tag(tag_id: int):
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM job_tags WHERE id = ?", (tag_id,))
        conn.commit()
        return {"deleted": True}
    finally:
        conn.close()


@app.post("/api/job-tags/retag")
def retag_job_tags():
    """Recompute job_tag on every existing job-category article using the
    *current* job_tags table -- call this after editing keywords, so the
    change applies retroactively, not just to future scrapes."""
    updated = scraper.retag_jobs()
    return {"updated": updated}


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


@app.get("/api/feed.xml")
def download_rss_feed(request: Request):
    """The "cumulative feed of all the feeds" -- every source this app
    scrapes, combined into one ordinary RSS 2.0 document any RSS reader
    can subscribe to, instead of following each source individually.
    Archived articles are excluded (get_articles()'s own default), same
    as everywhere else archiving is meant to actually hide something."""
    from fastapi.responses import Response

    articles = scraper.get_articles()
    xml = digest.render_rss_feed(
        articles,
        title="News Bot — All Sources",
        link=str(request.base_url),
        description="Aggregated feed of every source configured in News Bot.",
    )
    return Response(content=xml, media_type="application/rss+xml")


# --------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
