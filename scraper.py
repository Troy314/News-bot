"""
Scraping engine: fetches articles from DB-configured sources (RSS/Atom feeds
or CSS-selector-scraped HTML pages), and feed autodiscovery for the "add a
website" flow in the web UI.

This is the same parsing logic the project used when sources were a
hardcoded dict (see git history on news_bot.py before the app rewrite) --
only the source of configuration changed, from SOURCES[...] to DB rows.
"""

import re
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import db

logger = logging.getLogger("news_bot.scraper")

MAX_ARTICLES_PER_FETCH = 10
REQUEST_TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (compatible; NewsScraperBot/1.0)"


class ScrapeError(Exception):
    """Raised when a source's fetch/parse fails outright (network error, non-200,
    totally malformed response) -- as opposed to fetching fine but finding zero
    articles, which is a distinct ("empty") outcome, not necessarily broken."""


# Ordered, first-match-wins keyword rules for content-based tagging. Title
# text is all we reliably have across every source (CSS-scraped sources have
# no body/description at all, and RSS descriptions vary wildly in quality),
# so this classifies on title only -- deliberately simple, no ML/API call,
# tuned against real titles seen across this project's 20 sources (obituaries
# in "(1931-2025)" parenthetical form, "X welcomes its new management"
# spokesperson posts, CERN Courier's old jobs-feed-style postings, etc).
CONTENT_TAG_RULES = [
    ("Obituary", re.compile(r"\bobituary\b|\bin memoriam\b|\bin memory of\b|passed away|\bdies at\b|\(\d{4}[–-]\d{4}\)", re.I)),
    ("Award", re.compile(r"\baward\b|\bprize\b|\bnobel\b|\bmedal\b|\bhonou?r\b", re.I)),
    ("Personnel", re.compile(r"\bspokesperson\b|\bappoint|welcomes its new|new (team|management)|\belected\b|new director", re.I)),
    ("Discovery", re.compile(r"\bdiscover|\bobserv|\bdetect|\bevidence\b|\bmeasur|\bbreakthrough\b|first[- ]ever|\breveals?\b", re.I)),
    ("Event", re.compile(r"\bworkshop\b|\bconference\b|\bsymposium\b|\bsummit\b|\bseminar\b", re.I)),
    ("Jobs", re.compile(r"\bpostdoc\b|\bvacanc|\bhiring\b|\bphd (studentship|position)|\bjob (opening|posting)\b", re.I)),
    ("Funding", re.compile(r"\bfunding\b|\bbudget\b|\bgrant\b|[£$]\d|\bmillion\b|\binvest", re.I)),
    ("Outreach", re.compile(r"\bstudent|\bschool\b|\beducation\b|\boutreach", re.I)),
]
DEFAULT_CONTENT_TAG = "News"


def classify_article(title: str) -> str:
    for tag, pattern in CONTENT_TAG_RULES:
        if pattern.search(title):
            return tag
    return DEFAULT_CONTENT_TAG


class Article:
    def __init__(
        self,
        title: str,
        link: str,
        source_id: int,
        source_name: str,
        pub_date: datetime | None = None,
        image_url: str | None = None,
        content_tag: str | None = None,
    ):
        self.title = title.strip()
        self.link = link.strip()
        self.source_id = source_id
        self.source_name = source_name
        self.pub_date = pub_date
        self.image_url = image_url.strip() if image_url else None
        self.content_tag = content_tag or classify_article(self.title)

    def __repr__(self):
        return f"<Article {self.source_name!r} {self.title!r}>"


# --------------------------------------------------------------------------
# FETCHING
# --------------------------------------------------------------------------

def _fetch(url: str, user_agent: str = USER_AGENT, timeout: int = REQUEST_TIMEOUT, quiet: bool = False) -> requests.Response | None:
    try:
        resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        # Feed-path guessing (detect_feed's COMMON_FEED_PATHS probing) expects
        # most candidates to 404 -- that's a normal outcome, not worth an
        # error-level log line scaring whoever's watching the console.
        log = logger.debug if quiet else logger.error
        log(f"Failed to fetch {url}: {e}")
        return None


def fetch_html(url: str, user_agent: str = USER_AGENT, quiet: bool = False) -> str | None:
    resp = _fetch(url, user_agent, quiet=quiet)
    return resp.text if resp else None


def fetch_bytes(url: str, user_agent: str = USER_AGENT, quiet: bool = False) -> bytes | None:
    resp = _fetch(url, user_agent, quiet=quiet)
    return resp.content if resp else None


# --------------------------------------------------------------------------
# RSS / ATOM PARSING
# --------------------------------------------------------------------------

ATOM_NS = "{http://www.w3.org/2005/Atom}"
MEDIA_NS = "{http://search.yahoo.com/mrss/}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"

BARE_AMPERSAND_RE = re.compile(rb"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)")


def normalize_image_url(src: str) -> str:
    """Discord embeds / <img> tags need a fully-qualified URL; upgrade
    protocol-relative ("//host/...") image URLs, which some sites embed."""
    return f"https:{src}" if src.startswith("//") else src


def first_img_src(html: str | None) -> str | None:
    """Pull the first <img src> out of an HTML fragment (e.g. an item's
    content:encoded or description), for feeds with no dedicated image tag."""
    if not html:
        return None
    img = BeautifulSoup(html, "html.parser").find("img")
    src = img.get("src", "").strip() if img else ""
    return normalize_image_url(src) if src else None


def extract_rss_image(item: ET.Element) -> str | None:
    thumb = item.find(f"{MEDIA_NS}thumbnail")
    if thumb is not None and thumb.get("url"):
        return normalize_image_url(thumb.get("url").strip())

    media = item.find(f"{MEDIA_NS}content")
    if media is not None and media.get("url"):
        return normalize_image_url(media.get("url").strip())

    enclosure = item.find("enclosure")
    if enclosure is not None and (enclosure.get("type") or "").startswith("image/"):
        url = (enclosure.get("url") or "").strip()
        return normalize_image_url(url) if url else None

    return first_img_src(item.findtext(f"{CONTENT_NS}encoded")) or first_img_src(
        item.findtext("description")
    )


def parse_rss_date(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_feed_items(content: bytes):
    """Yield dicts with title/link/pub_date/image from RSS 2.0 or Atom feed bytes."""
    # Some feeds ship literal "&" instead of "&amp;" in item text, which is
    # invalid XML and makes ET.fromstring reject the whole document -- patch
    # just the bare ampersands before parsing.
    content = BARE_AMPERSAND_RE.sub(b"&amp;", content.lstrip())
    root = ET.fromstring(content)

    items = root.findall("./channel/item")
    if items:
        for item in items:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if title and link:
                yield {
                    "title": title,
                    "link": link,
                    "pub_date": parse_rss_date(item.findtext("pubDate")),
                    "image": extract_rss_image(item),
                }
        return

    for entry in root.findall(f"{ATOM_NS}entry"):
        title = (entry.findtext(f"{ATOM_NS}title") or "").strip()
        link = ""
        for link_el in entry.findall(f"{ATOM_NS}link"):
            if link_el.get("rel") in (None, "alternate"):
                link = (link_el.get("href") or "").strip()
                break
        if title and link:
            date_text = entry.findtext(f"{ATOM_NS}published") or entry.findtext(f"{ATOM_NS}updated")
            pub_date = None
            if date_text:
                try:
                    pub_date = datetime.fromisoformat(date_text.strip())
                    if not pub_date.tzinfo:
                        pub_date = pub_date.replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
            yield {
                "title": title,
                "link": link,
                "pub_date": pub_date,
                "image": first_img_src(entry.findtext(f"{ATOM_NS}content")),
            }


def scrape_rss(source: "sqlite3.Row") -> list[Article]:
    user_agent = source["user_agent"] or USER_AGENT
    content = fetch_bytes(source["url"], user_agent)
    if not content:
        raise ScrapeError(f"Could not fetch {source['url']}")

    try:
        entries = list(parse_feed_items(content))
    except ET.ParseError as e:
        # Some sources intermittently return a truncated/malformed response
        # (rate limiting, a transient proxy error, etc) -- one retry clears
        # most of these rather than losing the source for this whole cycle.
        logger.warning(f"Feed for {source['name']} failed to parse ({e}), retrying once...")
        content = fetch_bytes(source["url"], user_agent)
        if not content:
            raise ScrapeError(f"Could not fetch {source['url']} (retry)")
        try:
            entries = list(parse_feed_items(content))
        except ET.ParseError as e2:
            raise ScrapeError(f"Feed did not parse: {e2}")

    return [
        Article(
            title=entry["title"],
            link=entry["link"],
            source_id=source["id"],
            source_name=source["name"],
            pub_date=entry["pub_date"],
            image_url=entry["image"],
        )
        for entry in entries[:MAX_ARTICLES_PER_FETCH]
    ]


# --------------------------------------------------------------------------
# CSS-SELECTOR SCRAPING
# --------------------------------------------------------------------------

TEXT_DATE_PATTERNS = [
    (re.compile(r"[A-Z][a-z]+\s+\d{1,2},\s*\d{4}"), "%B %d, %Y"),
    (re.compile(r"\d{2}\.\d{2}\.\d{4}"), "%m.%d.%Y"),
]


def parse_css_date(date_el) -> datetime | None:
    if date_el is None:
        return None

    dt_attr = date_el.get("datetime")
    if dt_attr:
        try:
            dt = datetime.fromisoformat(dt_attr.strip())
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    text = date_el.get_text(" ", strip=True)
    for pattern, fmt in TEXT_DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        normalized = re.sub(r"\s+", " ", match.group(0))
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def resolve_url(href: str, base_url: str) -> str:
    if href.startswith("//"):
        return f"https:{href}"
    if href.startswith("http"):
        return href
    return base_url.rstrip("/") + "/" + href.lstrip("/")


def scrape_css(source: "sqlite3.Row") -> list[Article]:
    """Scrape a single CSS-selector-configured source, most recent first
    (order as they appear on the page)."""
    user_agent = source["user_agent"] or USER_AGENT
    html = fetch_html(source["url"], user_agent)
    if not html:
        raise ScrapeError(f"Could not fetch {source['url']}")

    soup = BeautifulSoup(html, "html.parser")
    containers = soup.select(source["article_selector"])

    articles: list[Article] = []
    for container in containers[:MAX_ARTICLES_PER_FETCH]:
        title_el = container.select_one(source["title_selector"])
        link_el = container.select_one(source["link_selector"])

        if not title_el or not link_el:
            continue

        title = title_el.get_text(strip=True)
        href = link_el.get("href", "").strip()
        if not href or not title:
            continue
        link = resolve_url(href, source["base_url"])

        pub_date = None
        if source["date_selector"]:
            pub_date = parse_css_date(container.select_one(source["date_selector"]))

        image_url = None
        if source["image_selector"]:
            image_el = container.select_one(source["image_selector"])
            src = image_el.get("src", "").strip() if image_el else ""
            if src:
                image_url = resolve_url(src, source["base_url"])

        articles.append(
            Article(
                title=title,
                link=link,
                source_id=source["id"],
                source_name=source["name"],
                pub_date=pub_date,
                image_url=image_url,
            )
        )

    return articles


def scrape_source(source: "sqlite3.Row") -> list[Article]:
    if source["kind"] == "rss":
        return scrape_rss(source)
    return scrape_css(source)


def _record_source_health(source_id: int, status: str, error: str | None) -> None:
    conn = db.get_conn()
    try:
        conn.execute(
            "UPDATE sources SET last_checked_at = datetime('now'), last_status = ?, last_error = ? "
            "WHERE id = ?",
            (status, error, source_id),
        )
        conn.commit()
    finally:
        conn.close()


def scrape_source_safe(source: "sqlite3.Row") -> list[Article]:
    """scrape_source(), but never raises, and always records the source's
    health (last_checked_at/last_status/last_error) as a side effect -- this
    is what powers the Sources tab's "is this working" indicator. Status is
    "ok" (fetched, found articles), "empty" (fetched fine, zero articles --
    often means CSS selectors need updating), or "error" (fetch/parse
    genuinely failed)."""
    try:
        articles = scrape_source(source)
        status, error = ("ok" if articles else "empty"), None
    except ScrapeError as e:
        articles, status, error = [], "error", str(e)
    except Exception as e:
        logger.exception(f"Unexpected error scraping {source['name']}")
        articles, status, error = [], "error", str(e)

    _record_source_health(source["id"], status, error)
    return articles


def scrape_all_sources() -> dict[str, list[Article]]:
    conn = db.get_conn()
    try:
        sources = conn.execute("SELECT * FROM sources WHERE enabled = 1").fetchall()
    finally:
        conn.close()
    return {s["name"]: scrape_source_safe(s) for s in sources}


# --------------------------------------------------------------------------
# PERSISTENCE (dedup by link, tracks which articles are new)
# --------------------------------------------------------------------------

def persist_articles(articles: list[Article]) -> list[int]:
    """Insert articles not already in the DB (deduped by link). Returns the
    ids of newly-inserted rows."""
    if not articles:
        return []
    conn = db.get_conn()
    new_ids = []
    try:
        for a in articles:
            cur = conn.execute(
                "INSERT OR IGNORE INTO articles (source_id, title, link, pub_date, image_url, content_tag) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (a.source_id, a.title, a.link, a.pub_date.isoformat() if a.pub_date else None, a.image_url, a.content_tag),
            )
            if cur.rowcount:
                new_ids.append(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return new_ids


def scrape_and_persist_all() -> list[int]:
    """Scrape every enabled source and persist new articles. Returns the ids
    of newly-inserted articles (empty if nothing new)."""
    results = scrape_all_sources()
    all_articles = [a for articles in results.values() for a in articles]
    return persist_articles(all_articles)


def backfill_content_tags() -> int:
    """One-time (but safely re-runnable) backfill: compute content_tag for
    any article that predates the tagging feature. Article.__init__ already
    falls back to classify_article() transparently on read, so this isn't
    required for correctness -- it's here so every consumer (in particular
    web.py's /api/articles, which reads raw rows via its own SQL rather than
    through Article) sees a real value without each one needing its own
    fallback. Returns how many rows were updated."""
    conn = db.get_conn()
    try:
        rows = conn.execute("SELECT id, title FROM articles WHERE content_tag IS NULL").fetchall()
        if not rows:
            return 0
        conn.executemany(
            "UPDATE articles SET content_tag = ? WHERE id = ?",
            [(classify_article(r["title"]), r["id"]) for r in rows],
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# FEED AUTODISCOVERY (for the "add a website" UI flow)
# --------------------------------------------------------------------------

COMMON_FEED_PATHS = ["/feed/", "/rss.xml", "/feed.xml", "/rss/", "/atom.xml", "/index.rss"]


def _looks_like_feed(content: bytes | None) -> bool:
    if not content:
        return False
    try:
        list(parse_feed_items(content))
        return True
    except ET.ParseError:
        return False


def detect_feed(url: str) -> str | None:
    """Best-effort feed autodiscovery for a website URL: checks the page's
    own <link rel=alternate> tags first, then a handful of common feed
    paths. Returns the feed URL if one parses successfully, else None (the
    caller should fall back to the manual CSS-selector form)."""
    html = fetch_html(url)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("link", rel="alternate"):
            feed_type = link.get("type", "")
            href = link.get("href")
            if href and ("rss" in feed_type or "atom" in feed_type):
                candidate = urljoin(url, href)
                if _looks_like_feed(fetch_bytes(candidate, quiet=True)):
                    return candidate

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    for path in COMMON_FEED_PATHS:
        candidate = origin + path
        if _looks_like_feed(fetch_bytes(candidate, quiet=True)):
            return candidate

    return None


# --------------------------------------------------------------------------
# READING BACK FROM THE DB
# --------------------------------------------------------------------------

def _row_to_article(row) -> Article:
    pub_date = datetime.fromisoformat(row["pub_date"]) if row["pub_date"] else None
    # content_tag is None for rows scraped before this feature existed --
    # Article.__init__ transparently backfills it via classify_article() on
    # read, no migration script needed. It's just not written back to the
    # row, so it's recomputed (cheap: a handful of regex checks) each time.
    article = Article(
        title=row["title"],
        link=row["link"],
        source_id=row["source_id"],
        source_name=row["source_name"],
        pub_date=pub_date,
        image_url=row["image_url"],
        content_tag=row["content_tag"] if "content_tag" in row.keys() else None,
    )
    article.id = row["id"]
    return article


def get_articles(only_notified: bool | None = None, ids: list[int] | None = None) -> list[Article]:
    """Read articles back out of the DB, newest first.

    only_notified=False -> only articles not yet sent out (new since last
    digest run); ids -> only these specific article ids (used to re-fetch
    the rows persist_articles() just inserted, already joined to source
    names)."""
    conn = db.get_conn()
    try:
        query = (
            "SELECT articles.*, sources.name AS source_name FROM articles "
            "JOIN sources ON sources.id = articles.source_id"
        )
        clauses, params = [], []
        if only_notified is not None:
            clauses.append("articles.notified = ?")
            params.append(0 if only_notified is False else 1)
        if ids is not None:
            if not ids:
                return []
            clauses.append(f"articles.id IN ({','.join('?' for _ in ids)})")
            params.extend(ids)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY articles.pub_date DESC"
        rows = conn.execute(query, params).fetchall()
        return [_row_to_article(r) for r in rows]
    finally:
        conn.close()


def mark_notified(article_ids: list[int]) -> None:
    if not article_ids:
        return
    conn = db.get_conn()
    try:
        conn.executemany(
            "UPDATE articles SET notified = 1 WHERE id = ?",
            [(i,) for i in article_ids],
        )
        conn.commit()
    finally:
        conn.close()
