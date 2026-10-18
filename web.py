"""
FastAPI app: REST API for managing sources, browsing/favoriting articles,
folders, and newsletter-email subscribers, plus the static frontend.
Runs alongside the Discord bot in the same process (see main.py).
"""

import ipaddress
import json
import logging
import math
import re
import socket
import statistics
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
from changelog import version_router
import mailer
import scraper

logger = logging.getLogger("news_bot.web")

app = FastAPI(title="News Bot")
# GET /api/version : version + journal des versions (CHANGELOG.md à la racine de l'application). Pas d'authentification propre à l'app (Cloudflare Access devant).
app.include_router(version_router(Path(__file__).resolve().parent / "CHANGELOG.md"))

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Captured at import time -- web.py is imported exactly once per process
# (from main.py), so this is a reliable process-uptime baseline for the
# Debug tab below.
PROCESS_STARTED_AT = datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Pydantic request bodies
# --------------------------------------------------------------------------

class SourceIn(BaseModel):
    name: str
    kind: str  # "rss" | "css" | "email" | "inspirehep" | "doctorat_gouv"
    category: str = "website"  # "website" | "newsletter" | "social" | "job"
    url: str
    article_selector: str | None = None
    title_selector: str | None = None
    link_selector: str | None = None
    base_url: str | None = None
    date_selector: str | None = None
    date_year_selector: str | None = None
    deadline_selector: str | None = None
    location_selector: str | None = None  # job-category: CSS selector for a free-text location/city (see Article.city)
    forced_job_tag: str | None = None  # job-category only: skip keyword classification, always use this tag
    image_selector: str | None = None
    user_agent: str | None = None
    enabled: bool = True
    group_tag: str | None = None  # free-text, user-defined -- orthogonal to category
    title_filter: str | None = None  # regex (case-insensitive); only keep articles whose title matches
    paginate: bool = False  # css only: fetch every page via JetEngine's load-more AJAX, not just page 1
    logo_url: str | None = None  # fallback image for an article with no image_url of its own (see renderCard)


class SourceUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None  # "rss" | "css" | "email" | "inspirehep" | "doctorat_gouv"
    category: str | None = None  # "website" | "newsletter" | "social" | "job"
    url: str | None = None
    article_selector: str | None = None
    title_selector: str | None = None
    link_selector: str | None = None
    base_url: str | None = None
    date_selector: str | None = None
    date_year_selector: str | None = None
    deadline_selector: str | None = None
    location_selector: str | None = None
    forced_job_tag: str | None = None
    image_selector: str | None = None
    user_agent: str | None = None
    enabled: bool | None = None
    group_tag: str | None = None
    title_filter: str | None = None
    paginate: bool | None = None
    logo_url: str | None = None


class DetectFeedIn(BaseModel):
    url: str


class FolderIn(BaseModel):
    name: str


class FavoriteIn(BaseModel):
    folder_id: int | None = None


class DeadlineIn(BaseModel):
    deadline: str | None = None  # "YYYY-MM-DD", or null/blank to clear


class RemunerationIn(BaseModel):
    remuneration: str | None = None  # free text, or null/blank to clear


class CityIn(BaseModel):
    city: str | None = None  # free text, or null/blank to clear


class LinkIn(BaseModel):
    link: str  # not nullable -- articles.link is NOT NULL UNIQUE, can only be changed, never cleared


class JobTagsIn(BaseModel):
    tags: list[str] = []  # the full set to store; [] clears them all to "Untagged"


class TopicsIn(BaseModel):
    topics: list[str] = []  # the full set of subject topics; [] clears them


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


def _fetch_for_iframe_embed(url: str, user_agent: str | None) -> BeautifulSoup:
    """Shared by preview_html (the Add-source selector picker) and
    view_article_html (Feed's "open embedded" article viewer): fetches a
    target page server-side (avoids the browser's own CORS/X-Frame-Options
    restrictions entirely, since the response is then served from *our*
    origin, not the target site's) and strips it down to something safe to
    put in an iframe:
    - Every <script> tag is stripped, so the target site's own JS can't run
      in our origin's iframe at all (defense in depth -- this isn't a
      sandboxed iframe the way GithubDocViewer's is in the sibling
      achievement-vault project, so no script from the embedded page
      should run here regardless of what it would do).
    - A <base href> is injected so the page's own relative image/CSS URLs
      still resolve and it renders recognizably.
    Raises HTTPException on a disallowed or unfetchable URL -- both
    callers just let that propagate, same "SSRF guard applies to any
    caller-supplied URL fetched server-side" reasoning _is_safe_external_url
    itself documents."""
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

    # Alpine.js/Vue-style conditional visibility (x-show/x-if, v-show/v-if)
    # defaults to *visible* in plain HTML+CSS -- it's the framework's own
    # JS that evaluates the expression and hides the element when it's
    # false. Since every <script> above is gone, that JS never runs, so a
    # mobile-nav drawer or an image-lightbox overlay meant to stay hidden
    # until clicked open renders permanently on top of the real content
    # instead -- confirmed directly on a real embedded page (IRAM's site,
    # Alpine-based): its `x-show="isMobileNavOpen"` nav drawer and
    # `x-show="modal.isOpen"` lightbox both sat visible over the article.
    # Force-hiding anything gated by these attributes is the safe default
    # here -- worst case a page loses a piece of UI it never needed for
    # reading anyway, versus the alternative of a modal/drawer silently
    # blocking the actual content. General fix, not IRAM-specific: any
    # Alpine/Vue site embedded this way would hit the same problem.
    hide_style = soup.new_tag("style")
    hide_style.string = "[x-show], [x-if], [v-show], [v-if] { display: none !important; }"
    head.append(hide_style)

    return soup


@app.get("/api/sources/preview-html", response_class=HTMLResponse)
def preview_html(request: Request, url: str, user_agent: str | None = None):
    """See _fetch_for_iframe_embed for the shared fetch/strip/base-href
    mechanics. This endpoint additionally appends the point-and-click
    selector-picker script (static/picker-inject.js) as an absolute-URL
    <script src>, pointed back at *this* server -- not a relative path,
    since the injected <base href> now points at the *target* site's
    origin, and a relative src would resolve against that instead of us.
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
    soup = _fetch_for_iframe_embed(url, user_agent)
    picker_script = soup.new_tag("script", src=f"{request.base_url}static/picker-inject.js")
    (soup.body or soup).append(picker_script)
    return str(soup)


@app.get("/api/articles/view-html", response_class=HTMLResponse)
def view_article_html(url: str, user_agent: str | None = None):
    """Backs Feed's "open embedded" article viewer (a modal with an
    iframe, instead of a new tab) -- same _fetch_for_iframe_embed mechanics
    as preview_html, just without the picker script (nothing here is being
    selector-picked, this is read-only viewing). A separate endpoint
    rather than a mode flag on preview_html: the two have different names
    for a reason (one's for configuring a source, one's for reading an
    already-scraped article), and picker-inject.js genuinely has no
    purpose here."""
    soup = _fetch_for_iframe_embed(url, user_agent)
    return str(soup)


class SaveTextIn(BaseModel):
    html: str


@app.post("/api/articles/{article_id}/save-text")
def save_article_text(article_id: int, body: SaveTextIn):
    """Stores a plain-text snapshot of a job/thesis offer's own page --
    for the same reason a listing routinely gets taken down or edited once
    a deadline passes, unlike a news article, which stays up indefinitely.

    **Takes pasted HTML from the caller instead of fetching the URL
    server-side, which an earlier version of this route did.** That first
    version reused the same fetch_html() + SSRF-guard pattern every other
    caller-supplied-URL fetch in this app already uses -- but job/thesis
    listing pages are disproportionately likely to sit behind Cloudflare/
    DataDome or a login wall (this app has hit exactly that wall for
    several *sources* already -- Symmetry Magazine, psthc.fr, PSNProfiles
    in the sibling achievement-vault project -- see "don't build a bypass"
    elsewhere in this file), so a plain server-side fetch of an arbitrary
    job page silently failed or returned a challenge page far more often
    than this app's other server-side fetches (RSS/CSS scraping, the
    embedded article viewer) ever do. Same fix shape as PSNProfiles' import
    in the sibling project: stop depending on a fetch that a real site can
    block, and have the user paste a page they already have open (View
    Page Source, or Ctrl+A/copy the rendered page) instead.
    Not restricted to category="job" server-side (same "UI-level
    restriction, not re-enforced here" reasoning as set_article_deadline
    above) -- the Jobs tab is just the only place a control for this
    exists. get_text(separator, strip=True) rather than storing the raw
    HTML: this is meant to be read as plain text later (the whole point is
    outliving the page's own styling/markup), and storing markup would
    also mean re-sanitizing it every time it's displayed, the exact same
    script-stripping _fetch_for_iframe_embed already has to do for the
    live iframe path -- plain text sidesteps that entirely since there's
    nothing left to run. Re-saving overwrites any previous snapshot (no
    history), and archiving the article clears it right back out (see
    archive_article) -- this is meant to bridge "the offer might disappear
    before I decide", not to be a permanent archive of the page independent
    of the offer's own lifecycle in this app."""
    conn = db.get_conn()
    try:
        exists = conn.execute("SELECT id FROM articles WHERE id = ?", (article_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Article not found")
        if not body.html.strip():
            raise HTTPException(400, "Pasted HTML is empty")
        soup = BeautifulSoup(body.html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        if not text:
            raise HTTPException(400, "No readable text found in the pasted HTML")
        conn.execute("UPDATE articles SET saved_text = ? WHERE id = ?", (text, article_id))
        conn.commit()
        return {"saved_text": text}
    finally:
        conn.close()


@app.delete("/api/articles/{article_id}/save-text")
def delete_article_text(article_id: int):
    conn = db.get_conn()
    try:
        exists = conn.execute("SELECT id FROM articles WHERE id = ?", (article_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Article not found")
        conn.execute("UPDATE articles SET saved_text = NULL WHERE id = ?", (article_id,))
        conn.commit()
        return {"saved_text": None}
    finally:
        conn.close()


@app.post("/api/sources")
def create_source(body: SourceIn):
    if body.kind not in ("rss", "css", "email", "inspirehep", "doctorat_gouv"):
        raise HTTPException(400, "kind must be 'rss', 'css', 'email', 'inspirehep', or 'doctorat_gouv'")
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
                "link_selector, base_url, date_selector, date_year_selector, deadline_selector, location_selector, "
                "forced_job_tag, image_selector, user_agent, enabled, group_tag, title_filter, paginate, logo_url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    body.name, body.kind, body.category, body.url,
                    body.article_selector, body.title_selector, body.link_selector, body.base_url,
                    body.date_selector, body.date_year_selector, body.deadline_selector, body.location_selector,
                    body.forced_job_tag, body.image_selector, body.user_agent,
                    int(body.enabled), body.group_tag, body.title_filter, int(body.paginate), body.logo_url,
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
    if "kind" in fields and fields["kind"] not in ("rss", "css", "email", "inspirehep", "doctorat_gouv"):
        raise HTTPException(400, "kind must be 'rss', 'css', 'email', 'inspirehep', or 'doctorat_gouv'")
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
        if "paginate" in fields:
            values[list(fields.keys()).index("paginate")] = int(fields["paginate"])
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
    include_hidden: bool = False,
):
    conn = db.get_conn()
    try:
        query = (
            "SELECT articles.*, sources.name AS source_name, sources.category AS source_category, "
            "sources.group_tag AS source_group, sources.logo_url AS source_logo, "
            "favorites.folder_id AS folder_id FROM articles "
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
        # Favorites tab.
        #
        # **The archived and hidden filters defer to each other
        # symmetrically** -- a real bug, hit twice in a row from both
        # directions: first "jobs i hidden disapeared" (a hidden-then-
        # archived row needed *both* flags to reappear, but "Show hidden"
        # only ever sent one), fixed by making `include_hidden` bypass the
        # archived exclusion for hidden rows. Then "unhidden jobs
        # disapeared": the fix above was one-directional -- unhiding a
        # row that was *also* archived cleared `hidden` but left
        # `archived` untouched, and since nothing bypassed the archived
        # filter for it anymore (hidden=0 now) *and* nothing bypassed the
        # hidden filter either (there was no "Show archived" view at
        # all), the row fell into a real gap: invisible from every
        # existing view with no UI path back. Fixed by making the
        # relationship fully symmetric -- `include_hidden` bypasses the
        # archived check for hidden rows, and `include_archived` now
        # likewise bypasses the hidden check for archived rows, so
        # whichever "show X" toggle is on always surfaces every row with
        # that flag set, regardless of what the *other* flag happens to
        # be. With both toggles on, neither clause is added at all (show
        # everything, archived-and-hidden rows included).
        archived_clause = "(articles.archived = 0 OR articles.hidden = 1)" if include_hidden else "articles.archived = 0"
        if not include_archived and not favorites_only and folder_id is None:
            clauses.append(archived_clause)
        # "Hide" (see set_article_hidden) is a separate axis from archive --
        # it declutters the main list the same way, but deliberately
        # doesn't delete anything (archiving clears saved_text, hiding
        # never touches pay/text). Same favorites/folder carve-out as
        # archived, for the same reason: hiding a job shouldn't also make
        # a starred one unreachable from Favorites.
        hidden_clause = "(articles.hidden = 0 OR articles.archived = 1)" if include_archived else "articles.hidden = 0"
        if not include_hidden and not favorites_only and folder_id is None:
            clauses.append(hidden_clause)
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
            a["job_tag_list"] = _effective_job_tags(a.get("job_tags"), a.get("job_tag"))
            a["topic_list"] = json.loads(a["topics"]) if a.get("topics") else []
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
    (below) already covers "I changed my mind" without a timer.
    Also clears any saved_text snapshot (see save_article_text) -- that
    snapshot exists specifically to bridge the gap before you've decided
    what to do with an offer; once it's archived (decided, one way or
    another) there's no more reason to keep the page text around, so this
    is a real deletion of that field, not something unarchiving restores."""
    conn = db.get_conn()
    try:
        exists = conn.execute("SELECT id FROM articles WHERE id = ?", (article_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Article not found")
        conn.execute("UPDATE articles SET archived = 1, saved_text = NULL WHERE id = ?", (article_id,))
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


@app.put("/api/articles/{article_id}/hide")
def hide_article(article_id: int):
    """A second, deliberately gentler way to declutter the Jobs list,
    distinct from Archive above -- requested directly ("add job add tag
    hide that hide jobs but dont deleat pay and text") specifically
    because Archive's saved_text-clearing side effect is sometimes too
    much: hiding a job you're just not interested in right now shouldn't
    throw away pay data or a saved snapshot the way deciding you're *done*
    with it (Archive) does. This route touches `hidden` only -- `remuneration`
    and `saved_text` are never cleared, and never will be by this action.
    Unlike Archive (which has no "view archived" UI), `GET
    /api/articles?include_hidden=1` is a real, reachable view (the Jobs
    tab's "Show hidden" toggle) specifically so hiding stays genuinely
    reversible through the UI, not just through the API."""
    conn = db.get_conn()
    try:
        exists = conn.execute("SELECT id FROM articles WHERE id = ?", (article_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Article not found")
        conn.execute("UPDATE articles SET hidden = 1 WHERE id = ?", (article_id,))
        conn.commit()
        return {"hidden": True}
    finally:
        conn.close()


@app.delete("/api/articles/{article_id}/hide")
def unhide_article(article_id: int):
    conn = db.get_conn()
    try:
        conn.execute("UPDATE articles SET hidden = 0 WHERE id = ?", (article_id,))
        conn.commit()
        return {"hidden": False}
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


@app.put("/api/articles/{article_id}/remuneration")
def set_article_remuneration(article_id: int, body: RemunerationIn):
    """Manual set/correct/clear for a job/thesis offer's pay, same shape as
    set_article_deadline above -- scraper.extract_remuneration() only ever
    has a title/excerpt to guess from (see its own docstring), so most
    listings won't get one automatically; this is the reliable path.
    Free text, not validated/parsed here -- GET /api/jobs/stats is what
    does a best-effort numeric read of whatever ends up stored, manual or
    extracted alike, when it needs to compute an actual average."""
    conn = db.get_conn()
    try:
        exists = conn.execute("SELECT id FROM articles WHERE id = ?", (article_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Article not found")
        value = body.remuneration.strip() if body.remuneration else None
        conn.execute("UPDATE articles SET remuneration = ? WHERE id = ?", (value, article_id))
        conn.commit()
        return {"remuneration": value}
    finally:
        conn.close()


@app.put("/api/articles/{article_id}/city")
def set_article_city(article_id: int, body: CityIn):
    """Manual set/correct/clear for a job/thesis offer's location, same
    shape as set_article_remuneration above -- a source's
    location_selector (see scraper.scrape_css) only ever covers CSS-scraped
    sources with a real per-listing location element, so most offers won't
    get one automatically; this is the reliable path. Free text, not
    validated against any fixed city list, same latitude remuneration/
    deadline already have."""
    conn = db.get_conn()
    try:
        exists = conn.execute("SELECT id FROM articles WHERE id = ?", (article_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Article not found")
        value = body.city.strip() if body.city else None
        conn.execute("UPDATE articles SET city = ? WHERE id = ?", (value, article_id))
        conn.commit()
        return {"city": value}
    finally:
        conn.close()


@app.put("/api/articles/{article_id}/link")
def set_article_link(article_id: int, body: LinkIn):
    """Manual correction for an article's own URL -- requested directly
    ("allow to change url"), most usefully for the "Manually added"
    Jobs source (see its own CLAUDE.md section): those rows were created
    with a Google-search fallback link since no real URL existed at
    import time, and this is how one gets swapped for the real posting
    URL once found. Not restricted to category="job" server-side, same
    "UI-only restriction" latitude every other simple field-edit route in
    this file already has -- correcting a Feed article's link is equally
    harmless. Unlike remuneration/deadline/city, `link` is `NOT NULL
    UNIQUE` -- there's no "clear" here (a blank/empty value is rejected,
    not silently accepted as null), and a value colliding with another
    article's existing link is a real 409, not a silent overwrite."""
    value = body.link.strip()
    if not value:
        raise HTTPException(400, "URL can't be empty")
    conn = db.get_conn()
    try:
        exists = conn.execute("SELECT id FROM articles WHERE id = ?", (article_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Article not found")
        try:
            conn.execute("UPDATE articles SET link = ? WHERE id = ?", (value, article_id))
            conn.commit()
        except db.sqlite3.IntegrityError:
            raise HTTPException(409, "Another article already has this exact URL")
        return {"link": value}
    finally:
        conn.close()


def _clean_list(items: list[str]) -> list[str]:
    """Trims, drops blanks, and removes duplicates while keeping order --
    shared by every per-article tag/topic list so they all normalize the same
    way."""
    clean: list[str] = []
    for t in items:
        t = t.strip()
        if t and t not in clean:
            clean.append(t)
    return clean


def _set_article_list(article_id: int, column: str, items: list[str]) -> list[str]:
    clean = _clean_list(items)
    conn = db.get_conn()
    try:
        exists = conn.execute("SELECT id FROM articles WHERE id = ?", (article_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Article not found")
        conn.execute(f"UPDATE articles SET {column} = ? WHERE id = ?", (json.dumps(clean), article_id))
        conn.commit()
        return clean
    finally:
        conn.close()


@app.put("/api/articles/{article_id}/job-tags")
def set_article_job_tags(article_id: int, body: JobTagsIn):
    """Replace an offer's job tags with the given set. Stored as JSON in
    articles.job_tags -- once written, this list is authoritative over the
    auto-classified job_tag (see _effective_job_tags), so an empty list means
    "deliberately untagged", not "fall back to auto"."""
    return {"tags": _set_article_list(article_id, "job_tags", body.tags)}


@app.put("/api/articles/{article_id}/topics")
def set_article_topics(article_id: int, body: TopicsIn):
    """Replace an offer's subject topics. Free-form, never auto-classified,
    and separate from job tags: a tag says what kind of offer this is
    (PhD, Outreach), a topic says what it is about."""
    return {"topics": _set_article_list(article_id, "topics", body.topics)}


def _effective_job_tags(job_tags_raw: str | None, job_tag: str | None) -> list[str]:
    """The tags an offer actually carries: its explicit list when one has
    been set by hand (even an empty one), otherwise just the auto-classified
    job_tag. Empty means untagged."""
    if job_tags_raw is not None:
        return json.loads(job_tags_raw)
    return [job_tag] if job_tag else []


# Loose "first number found, k-notation expanded" parse -- deliberately
# cruder than scraper.REMUNERATION_RE (which is about *finding* a salary
# mention at all): once we already know a string is someone's stored
# remuneration value, extracting *a* representative number from it for
# averaging doesn't need the currency/suffix matching, just the number
# itself. A range ("35 000 - 40 000 €") uses its first number -- a rough
# but consistent choice (always the lower bound), not an attempt at a true
# midpoint.
_STATS_NUMBER_RE = re.compile(r"\d{1,3}(?:[\s.,]\d{3})+|\d+")

# Every stat this route computes (avg/median/min/max/histogram) needs a
# single comparable unit across offers -- without this, a monthly stipend
# ("900 € / month") and an annual salary ("35 000 € / year") get averaged
# as if they were the same kind of number, wildly understating "pay" for
# any tag that mixes the two (an Internship's monthly stipend next to a
# Post-Doc's annual salary, say). Annual is the assumed default (matches
# how a bare number/range has always been treated), monthly is detected
# and scaled *12 -- covers both the explicit "/ month" the remuneration
# editor's period selector now appends (see openRemunerationEditor) and
# whatever scraper.REMUNERATION_RE's own suffix matching already pulled in
# organically from a source's own text ("mensuel", "/mois", etc).
_MONTHLY_RE = re.compile(r"mensuel(?:le)?|/\s*mois|per\s*month|/\s*month\b", re.I)


def _parse_remuneration_number(text: str) -> float | None:
    match = _STATS_NUMBER_RE.search(text)
    if not match:
        return None
    digits = re.sub(r"[\s.,]", "", match.group(0))
    try:
        value = float(digits)
    except ValueError:
        return None
    # Checked across the *whole* string, not just immediately after the
    # matched number -- a real bug, caught by inspecting real stored
    # values while building the Data Analysis page: a French range like
    # "26 - 31 k€ brut annuel" writes the "k" only once, after the
    # *second* number, so checking only right after the first (lower-
    # bound) number this function actually extracts always missed it,
    # silently parsing "26" as 26 €/year instead of 26 000. Every one of
    # the 21 manually-added "médiation scientifique" listings (see their
    # own CLAUDE.md section) used exactly this range-with-trailing-k
    # idiom, so this wasn't a rare edge case -- it was most of that
    # batch's pay data read as if salaries were literally ~€25-45.
    # Only a trailing \b, not \bk\b -- a "18k€" (no space before k) has no
    # word boundary between the digit and "k" at all (both are \w), so
    # requiring one on *both* sides missed this real case too (caught the
    # same way, inspecting real output): "18k€ - 20k€ / year" parsed as
    # 18 instead of 18 000.
    if re.search(r"k\b", text, re.I):
        value *= 1000
    if _MONTHLY_RE.search(text):
        value *= 12
    return value


def _histogram(values: list[float], bin_size: float = 500) -> list[dict]:
    """Buckets `values` into *fixed-width* `bin_size` ranges spanning
    [min, max] -- a real bin width (in currency units), not a fixed bin
    *count* an earlier version used (6 equal-width bins regardless of
    range) -- requested directly ("make bin size 500"). Bin edges are
    aligned to multiples of `bin_size` (`floor(min/bin_size)`,
    `ceil(max/bin_size)`), not just `min`/`min + bin_size`/..., so a
    500-wide bin reads as a round, recognizable range ("26 000–26 500")
    rather than an arbitrary offset from wherever the data happens to
    start. Deliberately scaled to each tag's *own* min/max rather than
    one shared axis across every tag: pay scales differ wildly by tag (an
    Internship stipend vs a Post-Doc salary), and a shared axis would
    flatten most tags into one or two overcrowded bins while a
    high-paying outlier tag spreads across the rest. A single distinct
    value (or all values equal) collapses to one bin spanning that value,
    rather than dividing by a zero-width range. Capped at 200 bins as a
    safety net against a genuinely malformed outlier value (e.g. a
    mis-parsed number many orders of magnitude off) producing a
    pathologically wide range -- falls back to a coarser bin size that
    fits within the cap rather than generating thousands of empty bins."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if lo == hi:
        return [{"min": lo, "max": hi, "count": len(values)}]
    MAX_BINS = 200
    if (hi - lo) / bin_size > MAX_BINS:
        bin_size = (hi - lo) / MAX_BINS
    start = math.floor(lo / bin_size) * bin_size
    end = math.ceil(hi / bin_size) * bin_size
    n_bins = max(1, round((end - start) / bin_size))
    counts = [0] * n_bins
    for v in values:
        idx = int((v - start) / bin_size)
        if idx >= n_bins:  # the max value itself lands exactly on the top edge
            idx = n_bins - 1
        counts[idx] += 1
    return [
        {"min": round(start + i * bin_size), "max": round(start + (i + 1) * bin_size), "count": counts[i]}
        for i in range(n_bins)
    ]


@app.get("/api/jobs/stats")
def job_stats(include_archived: bool = True, bin_size: float = 500):
    """Aggregate remuneration stats grouped by job_tag -- count of
    category="job" articles per tag, how many of those actually have a
    remuneration value at all (extracted or manual), summary statistics
    (mean/median/min/max/variance/stdev) and a binned count-vs-pay
    histogram (see _histogram), all derived from whatever
    _parse_remuneration_number() can make of each stored remuneration
    string. Every value is normalized to an annual figure first (monthly
    entries *12, see _parse_remuneration_number/_MONTHLY_RE) so a tag
    mixing monthly stipends and annual salaries doesn't get averaged as if
    they were the same unit. Every number here is best-effort on top of an
    already-best-effort field (see extract_remuneration()'s docstring) --
    useful for "which kind of role tends to pay more, and how is that
    spread out" at a glance, not precise figures.

    **`bin_size` (default 500, see _histogram) is caller-adjustable** --
    requested directly ("allow to manually change bin for all graph"),
    the Data Analysis page exposes it as a real control rather than
    leaving 500 hardcoded.

    **`include_archived` defaults to `True`** -- archiving hides a
    listing from active browsing (you've applied, it's closed, whatever
    the reason), but it doesn't make a pay figure you already recorded
    any less real; excluding archived offers here was once silently
    dropping real, already-entered data points (reported directly: "i
    have 8 data but only 6 taken into account"). That's still this
    route's *default* -- the "📊 Pay stats" quick popup calls it with no
    param, so it keeps including archived offers. The Data Analysis page
    is a different case: it was reported directly as double-counting
    stale, decided-and-done-with offers alongside the active pipeline it
    was otherwise showing ("dont take into account archived jobs"), so
    its own fetch passes `include_archived=0` explicitly -- same
    underlying data, two different defaults for two different questions
    ("what's the real pay data I've recorded, ever" vs "what does my
    active pipeline look like"), not a contradiction of the earlier fix."""
    conn = db.get_conn()
    try:
        query = (
            "SELECT articles.job_tag, articles.job_tags, articles.remuneration FROM articles "
            "JOIN sources ON sources.id = articles.source_id "
            "WHERE sources.category = 'job'"
        )
        if not include_archived:
            query += " AND articles.archived = 0"
        rows = conn.execute(query).fetchall()
    finally:
        conn.close()

    by_tag: dict[str, dict] = {}
    for row in rows:
        # An offer counts once under each of its tags, so per-tag totals can
        # sum to more than the number of offers -- a multi-tagged job is
        # genuinely part of both groups.
        for tag in _effective_job_tags(row["job_tags"], row["job_tag"]) or ["Untagged"]:
            bucket = by_tag.setdefault(tag, {"tag": tag, "count": 0, "with_remuneration": 0, "values": []})
            bucket["count"] += 1
            if row["remuneration"]:
                bucket["with_remuneration"] += 1
                num = _parse_remuneration_number(row["remuneration"])
                if num is not None:
                    bucket["values"].append(num)

    results = []
    for bucket in by_tag.values():
        values = bucket.pop("values")
        bucket.update(_pay_summary_stats(values))
        bucket["histogram"] = _histogram(values, bin_size=bin_size)
        results.append(bucket)
    results.sort(key=lambda b: (b["avg"] is None, -(b["avg"] or 0)))
    return results


def _pay_summary_stats(values: list[float]) -> dict:
    """Shared by job_stats (per-tag) and job_pay_by_city (per-city) --
    avg/median/min/max/variance/stdev of a list of already-parsed pay
    values, same ddof=1 sample variance/stdev (needs >=2 points, `None`
    below that -- "no signal yet", not "zero variance") either route
    already computed inline before this was factored out."""
    return {
        "avg": round(sum(values) / len(values), 2) if values else None,
        "median": round(statistics.median(values), 2) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "variance": round(statistics.variance(values), 2) if len(values) >= 2 else None,
        "stdev": round(statistics.stdev(values), 2) if len(values) >= 2 else None,
    }


@app.get("/api/jobs/pay-by-city")
def job_pay_by_city(include_archived: bool = True):
    """Same shape as job_stats, grouped by `city` instead of `job_tag` --
    requested directly ("add Pay by city") now that city is tracked at
    all (see "Job locations" elsewhere in this file). No histogram here
    (unlike job_stats) -- city is a much sparser grouping than job_tag in
    practice (one or two offers per city is common), so a per-city
    *distribution* chart would mostly be empty; a single mean-pay bar per
    city is the useful comparison. Offers with no city set are excluded
    entirely (there's no "Unknown city" bucket the way job_tag has
    "Untagged") -- grouping every uncategorized location together would
    just be noise, not a meaningful comparison point. Same
    `include_archived` default/reasoning as job_stats -- the Data
    Analysis page passes `include_archived=0`, nothing else calls this
    route yet."""
    conn = db.get_conn()
    try:
        query = (
            "SELECT articles.city, articles.remuneration FROM articles "
            "JOIN sources ON sources.id = articles.source_id "
            "WHERE sources.category = 'job' AND articles.city IS NOT NULL AND articles.city != ''"
        )
        if not include_archived:
            query += " AND articles.archived = 0"
        rows = conn.execute(query).fetchall()
    finally:
        conn.close()

    by_city: dict[str, dict] = {}
    for row in rows:
        bucket = by_city.setdefault(row["city"], {"city": row["city"], "count": 0, "with_remuneration": 0, "values": []})
        bucket["count"] += 1
        if row["remuneration"]:
            bucket["with_remuneration"] += 1
            num = _parse_remuneration_number(row["remuneration"])
            if num is not None:
                bucket["values"].append(num)

    results = []
    for bucket in by_city.values():
        values = bucket.pop("values")
        bucket.update(_pay_summary_stats(values))
        results.append(bucket)
    results.sort(key=lambda b: (b["avg"] is None, -(b["avg"] or 0)))
    return results


def _time_bucket(iso_str: str, granularity: str) -> str:
    """Buckets an ISO timestamp into a period label at the given
    granularity -- done in Python rather than SQLite's `strftime` (which
    has no native week/quarter format) so week/month/quarter all go
    through one consistent, easily-testable function instead of three
    different SQL dialects. `datetime.fromisoformat` handles this app's
    stored `...+HH:MM`-offset format directly (3.11+)."""
    dt = datetime.fromisoformat(iso_str)
    if granularity == "week":
        iso_year, iso_week, _ = dt.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if granularity == "quarter":
        return f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"
    return f"{dt.year}-{dt.month:02d}"  # "month" (default) and anything unrecognized


@app.get("/api/jobs/analysis")


def jobs_analysis(granularity: str = "month"):
    """Job-side *volume* analytics for the dedicated Data Analysis page --
    how many offers per source, a period-by-period posting count broken
    down by tag, and a separate period-by-period *discovery* count.
    Deliberately a separate route from /api/jobs/stats (which is about
    *pay*, not volume) even though both back the same page -- two
    routes/two concerns, not one endpoint trying to answer every question.

    **`granularity`** ("week"/"month"/"quarter", default "month") --
    requested directly alongside `job_stats`' `bin_size` ("allow to
    manually change bin for all graph"): the *time* axis is this route's
    equivalent of a histogram's bin size, so it gets the same
    caller-adjustable treatment. See _time_bucket().

    Two different time series, on purpose, not one:
    - `over_time` buckets by `pub_date` -- when the *posting* itself is
      dated (its own listed date, or "now" as a fallback for a source
      with no real one, see persist_articles()). This answers "how many
      job postings existed in this period."
    - `new_jobs_over_time` buckets by `first_seen_at` -- when this app's
      own scraper *found* the row, a column that's never defaulted or
      backfilled, always the real insert moment. This answers a different
      question: "how much did the tracker itself grow in this period,"
      i.e. scraping/discovery activity, independent of whatever date a
      posting claims. The two can disagree a lot for a source whose
      pub_date reflects a real (possibly old) listing date -- a thesis
      posted months ago but only scraped this week shows up in a *past*
      `over_time` bucket but *this period's* `new_jobs_over_time`
      bucket."""
    conn = db.get_conn()
    try:
        source_rows = conn.execute(
            "SELECT sources.name AS source, COUNT(*) AS count FROM articles "
            "JOIN sources ON sources.id = articles.source_id "
            "WHERE sources.category = 'job' AND articles.archived = 0 "
            "GROUP BY sources.name ORDER BY count DESC"
        ).fetchall()
        time_rows = conn.execute(
            "SELECT articles.pub_date, articles.job_tag, articles.job_tags "
            "FROM articles JOIN sources ON sources.id = articles.source_id "
            "WHERE sources.category = 'job' AND articles.archived = 0 "
            "AND articles.pub_date IS NOT NULL"
        ).fetchall()
        new_jobs_rows = conn.execute(
            "SELECT articles.first_seen_at FROM articles "
            "JOIN sources ON sources.id = articles.source_id "
            "WHERE sources.category = 'job' AND articles.archived = 0"
        ).fetchall()
    finally:
        conn.close()

    by_source = [{"source": r["source"], "count": r["count"]} for r in source_rows]

    by_period: dict[str, dict] = {}
    for row in time_rows:
        period = _time_bucket(row["pub_date"], granularity)
        bucket = by_period.setdefault(period, {"month": period, "total": 0, "by_tag": {}})
        bucket["total"] += 1
        for tag in _effective_job_tags(row["job_tags"], row["job_tag"]) or ["Untagged"]:
            bucket["by_tag"][tag] = bucket["by_tag"].get(tag, 0) + 1
    over_time = sorted(by_period.values(), key=lambda b: b["month"])

    new_by_period: dict[str, int] = {}
    for row in new_jobs_rows:
        period = _time_bucket(row["first_seen_at"], granularity)
        new_by_period[period] = new_by_period.get(period, 0) + 1
    new_jobs_over_time = [{"month": p, "count": c} for p, c in sorted(new_by_period.items())]

    return {"by_source": by_source, "over_time": over_time, "new_jobs_over_time": new_jobs_over_time}


@app.get("/api/jobs/pending-duplicates")
def list_pending_indeed_duplicates():
    """Sponsored Indeed ("pagead") links that arrived with the same title
    as an already-active job offer but a completely different link (no
    stable job id to dedup by at all -- see scraper.scrape_email's
    docstring) -- staged for a manual "same job, discard" vs "coincidence,
    keep both" call instead of being silently imported as a duplicate or
    silently dropped. Only `status = 'pending'` rows: a resolved one stays
    in the table forever (see pending_indeed_duplicates' schema comment)
    purely so its link permanently blocks re-staging, not to be re-shown."""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT pending_indeed_duplicates.*, "
            "articles.title AS matched_title, articles.link AS matched_link, "
            "articles.pub_date AS matched_pub_date "
            "FROM pending_indeed_duplicates "
            "LEFT JOIN articles ON articles.id = pending_indeed_duplicates.matched_article_id "
            "WHERE pending_indeed_duplicates.status = 'pending' "
            "ORDER BY pending_indeed_duplicates.created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/jobs/pending-duplicates/{pending_id}/import")
def import_pending_indeed_duplicate(pending_id: int):
    """"Not a duplicate, keep both" -- creates the real article through
    scraper.persist_articles(), the exact same path scrape_email() itself
    would have used had this not been flagged as a possible duplicate, so
    job_tag classification / content_tag / remuneration-from-title
    extraction / the pub_date-defaults-to-now fallback all happen exactly
    as they would for any other freshly-scraped job article, rather than
    a second, hand-rolled INSERT drifting out of sync with that logic over
    time. Marks this row `imported` (not deleted) so it's never re-staged
    on a later scan of the same email."""
    conn = db.get_conn()
    try:
        pending = conn.execute(
            "SELECT * FROM pending_indeed_duplicates WHERE id = ? AND status = 'pending'",
            (pending_id,),
        ).fetchone()
        if not pending:
            raise HTTPException(404, "Pending duplicate not found (or already resolved)")
        source = conn.execute("SELECT * FROM sources WHERE id = ?", (pending["source_id"],)).fetchone()
        if not source:
            raise HTTPException(404, "Source for this pending duplicate no longer exists")
    finally:
        conn.close()

    article = scraper.Article(
        title=pending["title"],
        link=pending["link"],
        source_id=source["id"],
        source_name=source["name"],
        pub_date=datetime.fromisoformat(pending["pub_date"]) if pending["pub_date"] else None,
        category=source["category"],
        forced_job_tag=source["forced_job_tag"],
    )
    new_ids = scraper.persist_articles([article])

    conn = db.get_conn()
    try:
        conn.execute("UPDATE pending_indeed_duplicates SET status = 'imported' WHERE id = ?", (pending_id,))
        conn.commit()
    finally:
        conn.close()
    return {"imported": True, "article_id": new_ids[0] if new_ids else None}


@app.delete("/api/jobs/pending-duplicates/{pending_id}")
def discard_pending_indeed_duplicate(pending_id: int):
    """"Yes, it's a duplicate" -- marks the row `discarded` rather than
    deleting it, so the identical link doesn't get re-staged the next time
    fetch_indeed_jobs() re-reads the same email (it reads every message on
    every run, by design -- see its docstring)."""
    conn = db.get_conn()
    try:
        result = conn.execute(
            "UPDATE pending_indeed_duplicates SET status = 'discarded' WHERE id = ? AND status = 'pending'",
            (pending_id,),
        )
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(404, "Pending duplicate not found (or already resolved)")
        return {"discarded": True}
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
    """The "cumulative feed of all the feeds" -- every *news* source this
    app scrapes (category != "job"; see download_jobs_rss_feed for the
    separate job/thesis-offer feed, split out since a subscriber wanting
    physics news almost certainly doesn't want job postings mixed in, same
    exclude_category=job scoping Feed's own tab already uses), combined
    into one ordinary RSS 2.0 document any RSS reader can subscribe to,
    instead of following each source individually. Archived articles are
    excluded (get_articles()'s own default), same as everywhere else
    archiving is meant to actually hide something."""
    from fastapi.responses import Response

    articles = scraper.get_articles(exclude_category="job")
    xml = digest.render_rss_feed(
        articles,
        title="News Bot — News",
        link=str(request.base_url),
        description="Aggregated feed of every news source configured in News Bot (excludes job/thesis offers).",
    )
    return Response(content=xml, media_type="application/rss+xml")


@app.get("/api/jobs-feed.xml")
def download_jobs_rss_feed(request: Request):
    """The job/thesis-offer counterpart to download_rss_feed above -- same
    mechanics, just category="job" instead of excluded, for a subscriber
    who wants job postings in their RSS reader without physics news
    diluting it (or vice versa)."""
    from fastapi.responses import Response

    articles = scraper.get_articles(category="job")
    xml = digest.render_rss_feed(
        articles,
        title="News Bot — Jobs",
        link=str(request.base_url),
        description="Aggregated feed of every job/thesis-offer source configured in News Bot.",
    )
    return Response(content=xml, media_type="application/rss+xml")


# --------------------------------------------------------------------------
# Debug
# --------------------------------------------------------------------------

@app.get("/api/debug")
def debug_info():
    """Everything needed to answer "is the daily job actually going to run,
    and did it last time" without SSHing in and grepping journalctl -- built
    after a real incident where a manually-run (non-systemd) process died
    silently overnight (SSH session dropped, taking a `Ctrl+Z`+`bg`'d
    `python main.py` down with it via SIGHUP) and there was no record
    anywhere of what happened or didn't.

    Deliberately reads live off the running process's own objects
    (discord_bot.bot, scheduler.daily_digest_task) rather than only DB
    state, since the DB alone can't tell "the loop never started" apart
    from "it started and is just waiting for tonight" -- both look
    identical in the sources/settings tables alone.
    """
    import scheduler  # lazy: same reasoning as run_now(), avoids importing discord_bot at module load
    import discord_bot

    bot = discord_bot.bot
    task = scheduler.daily_digest_task

    conn = db.get_conn()
    try:
        # Never-checked sources first, then oldest-checked next -- surfaces
        # staleness at a glance (see the Debug tab's own explanatory hint).
        sources = conn.execute(
            "SELECT id, name, category, enabled, last_checked_at, last_status, last_error "
            "FROM sources ORDER BY (last_checked_at IS NULL) DESC, last_checked_at ASC, name"
        ).fetchall()
        article_count = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
        latest_article = conn.execute(
            "SELECT title, pub_date, first_seen_at FROM articles ORDER BY first_seen_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    # bot.latency is float('nan') before the gateway heartbeat has ever
    # completed a round trip -- NaN != NaN is the standard Python idiom for
    # detecting it, since math.isnan() would need an extra import here.
    latency_ms = round(bot.latency * 1000, 1) if bot.latency == bot.latency else None

    return {
        "server_time_utc": now.isoformat(),
        "process_started_at": PROCESS_STARTED_AT.isoformat(),
        "process_uptime_seconds": (now - PROCESS_STARTED_AT).total_seconds(),
        "discord": {
            "token_configured": bool(discord_bot.DISCORD_TOKEN),
            "gateway_ready": bot.is_ready(),
            "user": str(bot.user) if bot.user else None,
            "latency_ms": latency_ms,
            "guild_count": len(bot.guilds) if bot.is_ready() else None,
            "news_channel_id": discord_bot.get_news_channel_id(),
        },
        "scheduler": {
            # What Settings currently holds -- may differ from what the
            # running loop below was actually scheduled with, if the time
            # was changed via the web UI without a restart since (see
            # scheduler.py's daily_digest_task comment).
            "configured_hour": int(db.get_setting("digest_hour", "8")),
            "configured_minute": int(db.get_setting("digest_minute", "0")),
            "configured_timezone": db.get_setting("digest_timezone", "Europe/Paris"),
            "loop_running": task.is_running(),
            "loop_iterations_so_far": task.current_loop,
            "next_scheduled_run": task.next_iteration.isoformat() if task.next_iteration else None,
            "last_digest_run_date": db.get_setting("last_digest_run_date"),
        },
        "sources": [dict(s) for s in sources],
        "articles": {
            "total_count": article_count,
            "latest": dict(latest_article) if latest_article else None,
        },
    }


# --------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------

# No cache-busting used to exist, so browsers (and Cloudflare, which caches
# .js/.css by default) kept serving a stale app.js/style.css after a deploy.
# index.html now references each asset with its mtime as ?v=, and everything
# is sent no-cache so a changed file is always revalidated.
_NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/")
def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("style.css", "app.js"):
        v = int((STATIC_DIR / name).stat().st_mtime)
        html = html.replace(f'/static/{name}"', f'/static/{name}?v={v}"')
    return HTMLResponse(html, headers=_NO_CACHE)


class _NoCacheStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", _NoCacheStatic(directory=STATIC_DIR), name="static")
