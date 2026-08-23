"""
Scraping engine: fetches articles from DB-configured sources (RSS/Atom feeds
or CSS-selector-scraped HTML pages), and feed autodiscovery for the "add a
website" flow in the web UI.

This is the same parsing logic the project used when sources were a
hardcoded dict (see git history on news_bot.py before the app rewrite) --
only the source of configuration changed, from SOURCES[...] to DB rows.
"""

import re
import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests
import soupsieve as sv
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


# Ordered, first-match-wins keyword rules for *topic* tagging (what the
# article is actually about physics-wise -- neutrino, muon, gravitational
# waves, etc -- not what kind of article it is). Title text is all we
# reliably have across every source (CSS-scraped sources have no body text
# at all, and RSS descriptions vary too much in quality/length), so this
# classifies on title only. Each pattern covers both English and French,
# since several primary sources publish in French (CERN's /fr/feed/, IN2P3,
# CNRS News) -- see CLAUDE.md's "Content tagging" section for the full
# English/French term-equivalence table this was built from; extend that
# table (and this list in lockstep) rather than adding French terms only
# here, so the two don't drift apart.
CONTENT_TAG_RULES = [
    ("Gravitational Waves", re.compile(
        r"gravitational waves?|\bligo\b|\bvirgo\b|\bkagra\b|\bgwtc\b|ondes? gravitationnelles?", re.I)),
    ("Neutrino", re.compile(r"\bneutrinos?\b", re.I)),
    ("Muon", re.compile(r"\bmuons?\b", re.I)),
    ("Higgs", re.compile(r"\bhiggs\b", re.I)),
    ("Dark Matter", re.compile(r"dark matter|mati[eè]re noire", re.I)),
    ("Dark Energy", re.compile(r"dark energy|[ée]nergie noire", re.I)),
    ("Antimatter", re.compile(r"antimatter|antiproton|antimati[eè]re", re.I)),
    ("Black Holes", re.compile(r"black holes?|trous? noirs?", re.I)),
    ("Superconductivity", re.compile(r"superconduct\w*|supraconduct\w*", re.I)),
    ("Quantum Computing", re.compile(
        r"quantum comput\w*|\bqubits?\b|ordinateur quantique|informatique quantique", re.I)),
    ("Standard Model", re.compile(r"standard model|mod[eè]le standard", re.I)),
    ("Accelerator & Collider", re.compile(
        r"\blhc\b|large hadron collider|\baccelerators?\b|\bcolliders?\b|\bbeamlines?\b|"
        r"acc[ée]l[ée]rateurs?|collisionneurs?", re.I)),
    ("Cosmic Rays", re.compile(r"cosmic rays?|cosmic particles?|rayons? cosmiques?", re.I)),
    ("Detector Technology", re.compile(r"\bdetectors?\b|\bd[ée]tecteurs?\b|\bsensors?\b|\bcapteurs?\b", re.I)),
    ("Exotic Particles", re.compile(
        r"exotic particles?|tetraquarks?|pentaquarks?|\bbaryons?\b|\bhadrons?\b|particules? exotiques?", re.I)),
]
DEFAULT_CONTENT_TAG = "General Physics"


def classify_article(title: str) -> str:
    for tag, pattern in CONTENT_TAG_RULES:
        if pattern.search(title):
            return tag
    return DEFAULT_CONTENT_TAG


# --------------------------------------------------------------------------
# JOB TAGS (user-editable title classification for category="job" sources)
# --------------------------------------------------------------------------
# Unlike CONTENT_TAG_RULES above (a fixed physics-topic taxonomy baked into
# this file), job-offer "type" tags (Fellowship/PhD/Internship/...) are
# user-defined via the Settings tab and stored in the job_tags table --
# db.py's DEFAULT_JOB_TAGS only seeds the table's *first* run, same "seed
# once, then it's the user's data" contract as db.py's other seeded tables.
# No default fallback tag the way General Physics is for content_tag --
# "no keyword matched this title" is a real, legitimate, distinct state
# here (most job postings won't match any of the 5 seeded tags), not
# something to force into a catch-all.

def get_job_tags() -> list[dict]:
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT id, name, keywords FROM job_tags ORDER BY sort_order, id"
        ).fetchall()
        return [{"id": r["id"], "name": r["name"], "keywords": json.loads(r["keywords"])} for r in rows]
    finally:
        conn.close()


def classify_job(title: str, job_tags: list[dict]) -> str | None:
    """First-tag-whose-keyword-matches wins, same "ordered, first match"
    shape as classify_article -- ordering here is `job_tags.sort_order`
    (falling back to `id`/creation order for ties or un-set rows), editable
    per-tag via the Settings tab's move-up/move-down controls. **Ordering
    is a real fix, not just cosmetic**: Post-Doc is seeded first
    specifically so a title like "Post-doctorant en cosmologie" (a real
    LPSC posting) matches Post-Doc's own keywords before PhD's ever get
    checked -- see the word-boundary caveat below for why the *matching*
    logic alone can't resolve that case.

    Matches on a **word boundary**, not a plain substring -- caught during
    testing: "thèse" as a bare substring matched inside "Synthèse de
    nouveaux éléments..." (a materials-science posting with zero relation
    to a thesis), purely because "synthèse" happens to contain "thèse" as
    a run of characters. `\\bthèse\\b` doesn't have this problem (no word
    boundary between "syn" and "thèse" in "synthèse", since they're one
    contiguous word) while still matching a standalone "thèse" correctly.
    This does NOT catch every compound-word collision -- "doctorant" still
    matches inside "post-doctorant" (a hyphen *is* a word boundary), which
    is exactly why Post-Doc's own keywords (checked first, see above) are
    the real fix for that specific case rather than the matching logic
    itself. A source can also bypass this keyword matching entirely via
    `sources.forced_job_tag` (see `persist_articles()`/`retag_jobs()`) for
    sites whose *entire* listing is reliably one type of offer regardless
    of title wording."""
    for tag in job_tags:
        for keyword in tag["keywords"]:
            if keyword and re.search(rf"\b{re.escape(keyword)}\b", title, re.I):
                return tag["name"]
    return None


def retag_jobs() -> int:
    """Recompute job_tag for every category="job" article using the
    *current* job_tags table -- run after the user edits/adds/deletes a tag
    in Settings, so keyword changes actually apply to already-scraped
    listings, not just future ones. Mirrors retag_all_articles()'s "full
    recompute" shape, scoped to job articles and triggerable from the UI
    (unlike retag_all_articles, which is a run-by-hand one-liner) since tag
    keywords are expected to change routinely, not just once at a scheme
    redesign. Also applies each row's source's `forced_job_tag` override
    (see `persist_articles()`), so pointing a source at a forced tag for
    the first time retags its *existing* rows immediately rather than only
    ever affecting new scrapes -- this is what actually applied "always
    PhD"/"always Outreach" overrides to sources that already had articles
    when the override was set. Returns how many rows were updated."""
    job_tags = get_job_tags()
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT articles.id, articles.title, sources.forced_job_tag FROM articles "
            "JOIN sources ON sources.id = articles.source_id "
            "WHERE sources.category = 'job'"
        ).fetchall()
        if not rows:
            return 0
        conn.executemany(
            "UPDATE articles SET job_tag = ? WHERE id = ?",
            [(r["forced_job_tag"] or classify_job(r["title"], job_tags), r["id"]) for r in rows],
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


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
        deadline: datetime | None = None,
        category: str | None = None,
        forced_job_tag: str | None = None,
        excerpt: str | None = None,
    ):
        self.title = title.strip()
        self.link = link.strip()
        self.source_id = source_id
        self.source_name = source_name
        self.pub_date = pub_date
        self.image_url = image_url.strip() if image_url else None
        self.content_tag = content_tag or classify_article(self.title)
        # Short plain-text blurb, only ever populated for kind="rss"
        # sources (see extract_excerpt()) -- None for every kind="css"
        # source, which has no body text on the page to draw one from.
        self.excerpt = excerpt.strip() if excerpt else None
        # Application deadline for a job/thesis offer -- see sources.deadline_selector.
        # Unrelated to pub_date (when the *listing* was posted); this is when it
        # *closes*. Only ever set for category="job" sources that expose one
        # (most don't), None everywhere else.
        self.deadline = deadline
        # The source's category ("job", "website", ...) -- carried through
        # only so persist_articles() knows whether to run job-tag
        # classification (see classify_job()); not itself persisted onto the
        # articles row (sources.category via the JOIN is the real source of
        # truth for display, this is just in-flight plumbing).
        self.category = category
        # A source-level override (sources.forced_job_tag) that always wins
        # over classify_job()'s keyword matching -- for sites whose entire
        # listing is reliably one type of offer (e.g. a lab's own "Theses"
        # page) regardless of how any individual title happens to be
        # worded. None for every source that doesn't set one (the common
        # case), in which case classify_job() runs as normal.
        self.forced_job_tag = forced_job_tag

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


TITLE_FALLBACK_MAX_LEN = 100
EXCERPT_MAX_LEN = 300


def html_to_text(html_fragment: str | None, max_len: int | None = None) -> str:
    """Strip HTML down to plain text, truncated at a word boundary --
    shared by derive_title() (title fallback) and extract_excerpt() below,
    since both are "turn a feed's raw <description>/<summary> HTML into a
    short, readable plain-text blurb" with only the length differing."""
    if not html_fragment:
        return ""
    text = BeautifulSoup(html_fragment, "html.parser").get_text(" ", strip=True)
    if max_len and len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "…"
    return text


def derive_title(description_html: str | None) -> str:
    """Microblog-style RSS items (Mastodon toots, and social feeds
    generally) often have no <title> at all -- a post isn't "titled"
    content the way a blog post is, so the generator just omits the tag
    rather than emitting an empty one. Falls back to the post's own text,
    truncated at a word boundary, so those items aren't silently dropped
    for lacking something they were never going to have."""
    return html_to_text(description_html, TITLE_FALLBACK_MAX_LEN)


def extract_excerpt(description_html: str | None) -> str | None:
    """A short plain-text blurb for an article, from an RSS item's
    <description> or an Atom entry's <summary>/<content> -- only ever
    available for kind="rss" sources; kind="css" sources have no body
    text on the page at all (see "Content tagging" above for the same
    limitation already documented for classify_article()), so their
    articles simply have no excerpt rather than a fabricated one."""
    text = html_to_text(description_html, EXCERPT_MAX_LEN)
    return text or None


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
            if not title:
                title = derive_title(item.findtext("description"))
            if title and link:
                yield {
                    "title": title,
                    "link": link,
                    "pub_date": parse_rss_date(item.findtext("pubDate")),
                    "image": extract_rss_image(item),
                    "excerpt": extract_excerpt(item.findtext("description")),
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
            content_html = entry.findtext(f"{ATOM_NS}content")
            yield {
                "title": title,
                "link": link,
                "pub_date": pub_date,
                "image": first_img_src(content_html),
                "excerpt": extract_excerpt(entry.findtext(f"{ATOM_NS}summary") or content_html),
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
            category=source["category"],
            forced_job_tag=source["forced_job_tag"],
            excerpt=entry.get("excerpt"),
        )
        for entry in entries[:MAX_ARTICLES_PER_FETCH]
    ]


# --------------------------------------------------------------------------
# CSS-SELECTOR SCRAPING
# --------------------------------------------------------------------------

TEXT_DATE_PATTERNS = [
    (re.compile(r"[A-Z][a-z]+\s+\d{1,2},\s*\d{4}"), "%B %d, %Y"),
    (re.compile(r"\d{2}\.\d{2}\.\d{4}"), "%m.%d.%Y"),
    # JUNO's news list: "(2026.06.12)", Y.M.D order -- distinguished from the
    # MM.DD.YYYY pattern above by a 4-digit first group, so the two can't
    # ambiguously match the same text.
    (re.compile(r"\d{4}\.\d{2}\.\d{2}"), "%Y.%m.%d"),
    # Slash-separated dates ("jusqu'au 12/05/2026", IPHC's job-offer deadline
    # text) -- treated as day/month/year, same default assumption
    # FRENCH_DATE_RE makes for every other numeric-ish French date in this
    # file (this app's own sources are overwhelmingly French/European, not
    # American, so DD/MM/YYYY is the safer default here, not MM/DD/YYYY).
    (re.compile(r"\d{2}/\d{2}/\d{4}"), "%d/%m/%Y"),
]

# Abbreviated English month names ("Oct 9, 2025", SuperNEMO's news list) --
# %B in TEXT_DATE_PATTERNS above only matches a *full* month name ("October"),
# so "Oct 9, 2025" falls through it; %b would parse it but is exactly the
# locale-dependent strptime behavior this file otherwise avoids (see
# FRENCH_MONTHS's comment). Hand-rolled for the same reason, same
# `name.startswith(abbrev)` shape as `_french_month_number` -- every English
# month's standard 3-letter abbreviation is already a distinguishing prefix
# of its own full name, so one table covers "Oct"/"October" alike with no
# separate full-name list needed.
EN_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
EN_DATE_MDY_RE = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})")
# Auger's news list uses "02 April 2026" (day first, no comma) -- the same
# day-then-month-then-year order parse_french_date already has to handle,
# just with an English month name.
EN_DATE_DMY_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\.?,?\s+(\d{4})")


def _english_month_number(name: str) -> int | None:
    name = name.lower().rstrip(".")
    for abbrev, num in EN_MONTHS.items():
        if name.startswith(abbrev):
            return num
    return None


def parse_english_date(text: str) -> datetime | None:
    for regex, groups_are_dmy in ((EN_DATE_DMY_RE, True), (EN_DATE_MDY_RE, False)):
        match = regex.search(text)
        if not match:
            continue
        a, b, year = match.groups()
        day, month_name = (a, b) if groups_are_dmy else (b, a)
        month = _english_month_number(month_name)
        if not month:
            continue
        try:
            return datetime(int(year), month, int(day), tzinfo=timezone.utc)
        except ValueError:
            continue
    return None

# French sites (CERN's /fr/feed/, IN2P3, CNRS News, CPPM) use "24 juil. 2026,
# 09:09:38"-style dates -- not covered by TEXT_DATE_PATTERNS/strptime, which
# is locale-dependent for month names anyway (relying on the server having a
# French locale installed would be fragile). Parsed by hand instead, same
# "don't trust locale-sensitive date parsing" reasoning as the RFC822/ISO
# date handling elsewhere in this file.
FRENCH_MONTHS = {
    "janv": 1, "fevr": 2, "févr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6,
    "juil": 7, "aout": 8, "août": 8, "sept": 9, "oct": 10, "nov": 11, "dec": 12, "déc": 12,
}
FRENCH_DATE_RE = re.compile(
    r"(\d{1,2})\s+([a-zéû]+)\.?\s+(\d{4})(?:,?\s*(\d{1,2}):(\d{2}):(\d{2}))?", re.I
)

# GANIL's news list uses the reverse order, "juin 15, 2026" (month day, year
# -- the same shape as the English TEXT_DATE_PATTERNS "Month D, YYYY" entry,
# just with a French month name, so that pattern's %B strptime can't parse
# it under a non-French locale). Tried as a fallback, not the primary
# pattern, since day-month-year is what every other French source here uses.
FRENCH_DATE_MDY_RE = re.compile(r"([a-zéû]+)\.?\s+(\d{1,2}),?\s+(\d{4})", re.I)


def _french_month_number(name: str) -> int | None:
    # Every French full month name starts with its standard abbreviation
    # (janvier->janv, février->févr, juillet->juil, ...), so one lookup
    # table covers both "3 juillet 2026" (full) and "24 juil. 2026" (abbr)
    # -- no need to enumerate full names separately.
    name = name.lower().rstrip(".")
    for abbrev, num in FRENCH_MONTHS.items():
        if name.startswith(abbrev):
            return num
    return None


def parse_french_date(text: str) -> datetime | None:
    match = FRENCH_DATE_RE.search(text)
    if match:
        day, month_name, year, hour, minute, second = match.groups()
        month = _french_month_number(month_name)
        if month:
            try:
                return datetime(
                    int(year), month, int(day),
                    int(hour or 0), int(minute or 0), int(second or 0),
                    tzinfo=timezone.utc,
                )
            except ValueError:
                pass

    match = FRENCH_DATE_MDY_RE.search(text)
    if match:
        month_name, day, year = match.groups()
        month = _french_month_number(month_name)
        if month:
            try:
                return datetime(int(year), month, int(day), tzinfo=timezone.utc)
            except ValueError:
                pass

    return None


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

    return parse_english_date(text) or parse_french_date(text)


YEAR_RE = re.compile(r"\b\d{4}\b")


def find_preceding_year(container, year_selector: str) -> str | None:
    """Some listing pages (Subatech's news/actualites page, e.g.) group
    articles under a year heading rather than repeating the year on every
    item -- each article's own date_selector only ever yields day+month
    ("22 juil."), with the year living in a separate <h3> that precedes a
    whole block of articles, not inside any single article's own container.
    A plain CSS selector can't express "nearest preceding sibling of an
    ancestor", so this walks backwards from the container in actual document
    order (BeautifulSoup's find_all_previous, nearest first) and tests each
    element against year_selector with soupsieve.match -- already a bs4
    dependency here for :has() support, so no new package needed."""
    for el in container.find_all_previous(True):
        if sv.match(year_selector, el):
            return el.get_text(strip=True)
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
        # A blank title/link_selector means "the article container itself is
        # the title/link element" -- some listing pages have no wrapping
        # element around a bare <a> for select_one() to find as a descendant
        # (e.g. Subatech's theses page: <a> directly under <ul>, no <li>).
        # Every pre-existing source has a real, non-blank selector here, so
        # this is a no-op for them.
        title_el = container.select_one(source["title_selector"]) if source["title_selector"] else container
        link_el = container.select_one(source["link_selector"]) if source["link_selector"] else container

        if not title_el or not link_el:
            continue

        title = title_el.get_text(strip=True)
        href = link_el.get("href", "").strip()
        if not href:
            # Some pages (CPPM's news listing, e.g.) have no real per-article
            # <a href>, only an in-page anchor id used for scroll-to-section
            # navigation (<a id="...">, no href at all). Fall back to that:
            # the article's "link" becomes the scraped page itself plus that
            # anchor, which is a genuine, distinct, working URL even though
            # it's not a separate page.
            anchor_id = (link_el.get("id") or "").strip()
            if anchor_id:
                href = f"{source['url']}#{anchor_id}"
        if not href or not title:
            continue
        link = resolve_url(href, source["base_url"])

        pub_date = None
        if source["date_selector"]:
            date_el = container.select_one(source["date_selector"])
            pub_date = parse_css_date(date_el)
            if pub_date is None and date_el is not None and source["date_year_selector"]:
                date_text = date_el.get_text(" ", strip=True)
                if date_text and not YEAR_RE.search(date_text):
                    year_text = find_preceding_year(container, source["date_year_selector"])
                    if year_text and YEAR_RE.search(year_text):
                        pub_date = parse_french_date(f"{date_text} {year_text}")

        image_url = None
        if source["image_selector"]:
            image_el = container.select_one(source["image_selector"])
            src = image_el.get("src", "").strip() if image_el else ""
            if src:
                image_url = resolve_url(src, source["base_url"])

        deadline = None
        if source["deadline_selector"]:
            deadline = parse_css_date(container.select_one(source["deadline_selector"]))

        articles.append(
            Article(
                title=title,
                link=link,
                source_id=source["id"],
                source_name=source["name"],
                pub_date=pub_date,
                image_url=image_url,
                deadline=deadline,
                category=source["category"],
                forced_job_tag=source["forced_job_tag"],
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
    # Only fetched if at least one article is from a job source -- a plain
    # news-only run (the common case) never touches the job_tags table.
    job_tags = None
    conn = db.get_conn()
    new_ids = []
    try:
        for a in articles:
            job_tag = None
            if a.category == "job":
                if a.forced_job_tag:
                    job_tag = a.forced_job_tag
                else:
                    if job_tags is None:
                        job_tags = get_job_tags()
                    job_tag = classify_job(a.title, job_tags)
            # A source with no date_selector, or one whose date text didn't
            # parse, would otherwise leave pub_date NULL -- landing the
            # article in Feed/Jobs' "Unknown date" bucket forever. Default
            # it to right now (the moment scraping actually found it)
            # instead, same fallback `first_seen_at` already recorded
            # independently -- this only ever fires for a genuinely new
            # row (INSERT OR IGNORE skips existing ones untouched), so an
            # article's defaulted date is fixed at first-seen time, not
            # recomputed on every later scrape.
            pub_date = a.pub_date or datetime.now(timezone.utc)
            cur = conn.execute(
                "INSERT OR IGNORE INTO articles "
                "(source_id, title, link, pub_date, image_url, content_tag, deadline, job_tag, excerpt) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    a.source_id, a.title, a.link, pub_date.isoformat(),
                    a.image_url, a.content_tag,
                    a.deadline.isoformat() if a.deadline else None,
                    job_tag, a.excerpt,
                ),
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


def backfill_excerpts(limit: int | None = None) -> int:
    """One-time (but safely re-runnable) backfill: fills in `excerpt` for
    already-scraped kind="rss" articles that predate that column, by
    re-fetching each affected source's *current* feed and matching each
    missing-excerpt article by `link`. Only kind="rss" sources are
    attempted -- kind="css" sources have no body text on the page at all
    to draw an excerpt from (see extract_excerpt()), so their articles are
    left alone rather than treated as a bug. **Not guaranteed to recover
    every row**: an older article's link may have already rolled off its
    source's current feed (most feeds only expose their N most recent
    items), in which case it simply can't be backfilled this way and stays
    without an excerpt -- there's no way to recover text that's no longer
    published anywhere.

    Sources are fetched at most once each regardless of how many of their
    articles need backfilling (grouped by source_id first), same "don't
    hammer a site with redundant requests" discipline as every other
    fetch loop in this file. `limit` caps how many missing-excerpt
    articles (most-recent-first) are attempted, for a deliberately partial
    backfill instead of re-fetching every configured RSS source's feed --
    e.g. `backfill_excerpts(limit=20)` for just the 20 most recent gaps.
    Not called automatically anywhere; run by hand
    (`python -c "import scraper; scraper.backfill_excerpts()"`).
    Returns how many rows were actually updated."""
    conn = db.get_conn()
    try:
        query = (
            "SELECT articles.id, articles.link, sources.id AS source_id, "
            "sources.url, sources.user_agent FROM articles "
            "JOIN sources ON sources.id = articles.source_id "
            "WHERE articles.excerpt IS NULL AND sources.kind = 'rss' "
            "ORDER BY articles.pub_date DESC"
        )
        if limit is not None:
            query += " LIMIT ?"
            rows = conn.execute(query, (limit,)).fetchall()
        else:
            rows = conn.execute(query).fetchall()
    finally:
        conn.close()

    if not rows:
        return 0

    by_source: dict[int, list] = {}
    for r in rows:
        by_source.setdefault(r["source_id"], []).append(r)

    updated = 0
    conn = db.get_conn()
    try:
        for article_rows in by_source.values():
            source_url = article_rows[0]["url"]
            user_agent = article_rows[0]["user_agent"] or USER_AGENT
            content = fetch_bytes(source_url, user_agent, quiet=True)
            if not content:
                continue
            try:
                entries = list(parse_feed_items(content))
            except ET.ParseError:
                continue
            excerpt_by_link = {e["link"]: e["excerpt"] for e in entries if e.get("excerpt")}
            for r in article_rows:
                excerpt = excerpt_by_link.get(r["link"])
                if excerpt:
                    conn.execute("UPDATE articles SET excerpt = ? WHERE id = ?", (excerpt, r["id"]))
                    updated += 1
        conn.commit()
    finally:
        conn.close()
    return updated


def retag_all_articles() -> int:
    """Recompute content_tag for *every* article, not just NULL ones --
    unlike backfill_content_tags(), this is for when CONTENT_TAG_RULES
    itself changes (e.g. the event-type -> physics-topic redesign) and
    already-tagged rows need to move to the new scheme too. Not called
    automatically anywhere (retagging on every startup would be pointless
    overhead for a scheme that isn't changing regularly) -- run it by hand
    (`python -c "import scraper; scraper.retag_all_articles()"`) after a
    CONTENT_TAG_RULES change. Returns how many rows were updated."""
    conn = db.get_conn()
    try:
        rows = conn.execute("SELECT id, title FROM articles").fetchall()
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

# Appended to the *exact given URL's own path* (not the site's root origin)
# -- covers per-account/per-page feeds like Reddit's "/r/name/.rss" or a
# Mastodon-style "/@user.rss", which a site-root guess would never find.
PATH_SUFFIXES = ["/.rss", ".rss", "/.atom", "/rss", "/rss.xml", "/feed"]


def _looks_like_feed(content: bytes | None) -> bool:
    if not content:
        return False
    try:
        # Well-formed HTML (properly closed tags, no bare &, etc -- common
        # from modern frameworks) can parse as valid XML with zero
        # <channel>/<item> or <atom:entry> matches. Checking only "did
        # ET.fromstring not raise" made a bare homepage look like a feed --
        # a real result needs actual items, not just parseable-as-XML.
        return len(list(parse_feed_items(content))) > 0
    except ET.ParseError:
        return False


def detect_feed(url: str) -> str | None:
    """Best-effort feed autodiscovery for a website (or social profile) URL.
    Order: (1) the given URL might already *be* a feed -- e.g. a YouTube
    "feeds/videos.xml?channel_id=..." URL found by hand, since YouTube's
    human-facing channel page is cookie-consent-walled and can't be
    autodiscovered from, but the feed endpoint itself is plain public XML;
    (2) the page's own <link rel=alternate> tags; (3) feed suffixes appended
    to the *exact given URL* (Reddit's "/r/x/.rss", Mastodon-style
    "/@user.rss" -- per-account feeds a site-root guess would miss); (4) a
    handful of common paths against the site's root origin. Returns the feed
    URL if one parses successfully, else None (the caller should fall back
    to the manual CSS-selector form)."""
    content = fetch_bytes(url, quiet=True)
    if _looks_like_feed(content):
        return url

    if content:
        html = content.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("link", rel="alternate"):
            feed_type = link.get("type", "")
            href = link.get("href")
            if href and ("rss" in feed_type or "atom" in feed_type):
                candidate = urljoin(url, href)
                if _looks_like_feed(fetch_bytes(candidate, quiet=True)):
                    return candidate

    base = url.rstrip("/")
    for suffix in PATH_SUFFIXES:
        candidate = base + suffix
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
    deadline = (
        datetime.fromisoformat(row["deadline"])
        if "deadline" in row.keys() and row["deadline"]
        else None
    )
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
        deadline=deadline,
        excerpt=row["excerpt"] if "excerpt" in row.keys() else None,
    )
    article.id = row["id"]
    return article


def get_articles(
    only_notified: bool | None = None,
    ids: list[int] | None = None,
    include_archived: bool = False,
) -> list[Article]:
    """Read articles back out of the DB, newest first.

    only_notified=False -> only articles not yet sent out (new since last
    digest run); ids -> only these specific article ids (used to re-fetch
    the rows persist_articles() just inserted, already joined to source
    names). include_archived mirrors web.py's list_articles() -- archived
    articles are excluded by default from both, since an archived job
    listing showing up in a digest download or the aggregate RSS feed
    would defeat the point of archiving it."""
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
        if not include_archived:
            clauses.append("articles.archived = 0")
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
