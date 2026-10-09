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
import html
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests
import soupsieve as sv
from bs4 import BeautifulSoup

import db
import email_import

logger = logging.getLogger("news_bot.scraper")

MAX_ARTICLES_PER_FETCH = 10
# A source's very first scrape (sources.last_checked_at still NULL, the
# same column _record_source_health stamps on every run -- no separate
# tracking needed) pulls a deeper backlog than routine runs do, so a newly
# added source starts with real history instead of just whatever happened
# to be the 10 most recent items the day it was added.
FIRST_SCRAPE_LIMIT = 50
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


# Best-effort salary/remuneration extraction for category="job" articles --
# only title + excerpt are ever available at scrape time (a css source has
# no body text at all; even rss/inspirehep's excerpt is a short blurb, not
# the full posting), so this only ever catches a listing that happens to
# advertise pay right there rather than "see the full posting" -- expected
# to have partial coverage, not universal. That's why this is paired with
# a manual edit control in the UI (PUT /api/articles/:id/remuneration) --
# the reliable path is the user filling it in themselves; extraction is a
# head start, not the whole feature. Stored as the matched *text* verbatim
# (not a parsed number) since a range ("35 000 - 40 000 €") or a currency/
# period ("brut annuel", "per year") carries real information a bare
# number would lose -- GET /api/jobs/stats does its own separate, cruder
# numeric parse of whatever's stored (manual or extracted) when it needs an
# actual average.
_REMUNERATION_NUM = r"\d{1,3}(?:[\s.,]\d{3})+|\d+\s*[kK]"
_REMUNERATION_CUR = r"€|£|\$"
_REMUNERATION_SUFFIX = (
    r"(?:brut|net|annuel(?:le)?|annually|annual|mensuel(?:le)?|/\s*an|/\s*mois|"
    r"/\s*year|/\s*month|per\s*year|per\s*month|p\.?a\.?)"
)
REMUNERATION_RE = re.compile(
    rf"(?:{_REMUNERATION_CUR})?\s*(?:{_REMUNERATION_NUM})\s*(?:{_REMUNERATION_CUR})?"
    rf"(?:\s*(?:-|–|to|à)\s*(?:{_REMUNERATION_CUR})?\s*(?:{_REMUNERATION_NUM})\s*(?:{_REMUNERATION_CUR})?)?"
    rf"(?:\s*{_REMUNERATION_SUFFIX}){{0,2}}",
    re.I,
)


def extract_remuneration(*texts: str | None) -> str | None:
    for text in texts:
        if not text:
            continue
        match = REMUNERATION_RE.search(text)
        if match and any(c.isdigit() for c in match.group(0)):
            return re.sub(r"\s+", " ", match.group(0)).strip()
    return None


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


# Auto-archived the moment a job is classified into one of these tags --
# requested directly ("by default... should be archived") after a one-time
# manual bulk-archive (see CLAUDE.md) kept needing to be redone as new
# low-signal offers were scraped, and after the client-side hidden-tags
# filter (a per-browser localStorage toggle) proved too easy to silently
# diverge from. This makes the policy a real, permanent server-side rule
# instead: applied in persist_articles() at insert time and in
# retag_jobs() when a keyword change reclassifies an existing row into one
# of these tags. Deliberately one-directional -- it never *un*-archives a
# row whose tag changes away from this set, since that could silently
# undo an archive decision made for an unrelated reason.
AUTO_ARCHIVE_JOB_TAGS = {"Internship", "Professorship", "Physicist", "Fellowship", "Post-Doc"}


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
            "SELECT articles.id, articles.title, articles.job_tag, articles.job_tags, "
            "sources.forced_job_tag FROM articles "
            "JOIN sources ON sources.id = articles.source_id "
            "WHERE sources.category = 'job'"
        ).fetchall()
        if not rows:
            return 0
        updates = []
        for r in rows:
            new_tag = r["forced_job_tag"] or classify_job(r["title"], job_tags)
            manual = json.loads(r["job_tags"]) if r["job_tags"] is not None else None
            if manual is not None and r["job_tag"] and r["job_tag"] in manual and new_tag != r["job_tag"]:
                # Swap the old auto tag for the new one inside a manual list,
                # so a retag never silently drops tags the user added by hand.
                manual = [new_tag if t == r["job_tag"] else t for t in manual]
                manual = list(dict.fromkeys(t for t in manual if t))
            updates.append((new_tag, json.dumps(manual) if manual is not None else None,
                             # Only archive when every tag on the row is low-signal --
                             # a PhD that also carries a hand-added Post-Doc must stay.
                             all(t in AUTO_ARCHIVE_JOB_TAGS for t in (manual or [new_tag])), r["id"]))
        # archived only ever flips 0 -> 1 here (MAX keeps a 1 from an earlier,
        # unrelated archive decision), never back to 0 -- see AUTO_ARCHIVE_JOB_TAGS.
        conn.executemany(
            "UPDATE articles SET job_tag = ?, job_tags = COALESCE(?, job_tags), "
            "archived = MAX(archived, ?) WHERE id = ?",
            updates,
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
        remuneration: str | None = None,
        city: str | None = None,
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
        # Best-effort, see extract_remuneration() -- only attempted for
        # category="job" (a news article has no reason to carry a salary
        # field, and running the regex over every physics-news title would
        # just be wasted work for a value nothing ever reads there).
        self.remuneration = remuneration or (
            extract_remuneration(self.title, self.excerpt) if category == "job" else None
        )
        # Application deadline for a job/thesis offer -- see sources.deadline_selector.
        # Unrelated to pub_date (when the *listing* was posted); this is when it
        # *closes*. Only ever set for category="job" sources that expose one
        # (most don't), None everywhere else.
        self.deadline = deadline
        # Free-text job location (e.g. "Nantes - 44") -- see
        # sources.location_selector. Stored verbatim, same "don't over-parse
        # a free-text field" reasoning as remuneration (a department code
        # suffix is real information a bare city name would lose). Most
        # sources don't expose one; None there, same as deadline.
        self.city = city.strip() if city else None
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
DC_NS = "{http://purl.org/dc/elements/1.1/}"
RSS1_NS = "{http://purl.org/rss/1.0/}"

BARE_AMPERSAND_RE = re.compile(rb"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)")


def normalize_image_url(src: str) -> str:
    """Discord embeds / <img> tags need a fully-qualified URL; upgrade
    protocol-relative ("//host/...") image URLs, which some sites embed."""
    return f"https:{src}" if src.startswith("//") else src


def first_img_src(html: str | None) -> str | None:
    """Pull the *largest* <img src> out of an HTML fragment (e.g. an item's
    content:encoded or description), for feeds with no dedicated image tag.

    Despite the name (kept for the two existing callers' sake -- derive_title
    doesn't care about size, and this is still "the first/best guess"), this
    is NOT simply the first <img> in document order -- SPIP (the CMS behind
    several IN2P3 lab sites: LPNHE confirmed directly, likely others sharing
    the same theme) auto-inserts a small ~110x42 "spip_logo" icon at the very
    top of every article's content, *before* any real photo further down.
    Picking the first <img> unconditionally grabbed that icon instead of the
    actual, much larger (e.g. 500x684) content photo sitting right after it
    in the same feed item -- confirmed by inspecting LPNHE's raw feed
    directly, not guessed. Now picks whichever <img> has the largest
    width*height (parsed from its own attributes, no network fetch needed);
    an image with no size attributes at all is treated as area 0, so a
    feed that provides dimensions on its real photo but not its icon (or
    vice versa) is still handled correctly, and a feed where *no* image
    anywhere has dimensions just falls back to the first one, same as
    before this fix -- no worse than the old behavior in that case, only
    better once real size info exists."""
    if not html:
        return None
    imgs = BeautifulSoup(html, "html.parser").find_all("img")
    if not imgs:
        return None

    def area(img) -> int:
        try:
            return int(img.get("width", 0)) * int(img.get("height", 0))
        except (ValueError, TypeError):
            return 0

    best = max(imgs, key=area) if any(area(i) for i in imgs) else imgs[0]
    src = best.get("src", "").strip()
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
    <description>, an Atom entry's <summary>/<content>, or (since
    scrape_inspirehep) InspireHEP's own JSON `description` field -- every
    kind="css" source has no body text on the page at all to draw one from
    (see "Content tagging" above for the same limitation already
    documented for classify_article()), so their articles simply have no
    excerpt rather than a fabricated one."""
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

    # A plain, no-namespace <image> child -- not part of any RSS spec at
    # the item level (only the *channel*'s own logo, per RSS 0.91/2.0), but
    # CERN Courier's WordPress feed adds one per item anyway as its real
    # featured-image field -- confirmed on 160/160 real items, always a
    # genuine "frontis"-style article photo, never blank. Checked before
    # falling back to guessing from body content: a feed that bothers to
    # provide this explicitly is a more reliable signal than picking the
    # largest <img> out of the article body, which can just as easily be
    # an unrelated advertiser's logo sitting in the content (seen on a real
    # CERN Courier item: the only in-body <img> was an MDPI sponsor logo,
    # completely unrelated to the actual article).
    image_tag = item.findtext("image")
    if image_tag and image_tag.strip():
        return normalize_image_url(image_tag.strip())

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
    """Yield dicts with title/link/pub_date/image from RSS 2.0, RSS 1.0/RDF,
    or Atom feed bytes."""
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

    # RSS 1.0/RDF: <item> lives as a direct child of the root <rdf:RDF>
    # (a sibling of <channel>, not nested inside it like RSS 2.0), *and*
    # RDF feeds declare a bare `xmlns="http://purl.org/rss/1.0/"` default
    # namespace on the root -- meaning every unprefixed element (item,
    # title, link, description alike) actually needs the RSS1_NS prefix
    # for ElementTree to find it at all; a plain "item"/"title" lookup
    # (which works fine for ordinary RSS 2.0, since those feeds don't
    # declare a default namespace) silently matches nothing here. The date
    # is Dublin Core's <dc:date> (real ISO 8601, e.g.
    # "2026-09-24T10:00:00+00:00") instead of RFC 822 <pubDate>. Hit for
    # real on APS Physics Magazine's feed (feeds.aps.org) -- confirmed by
    # inspecting that feed directly (including the first, wrong attempt at
    # this fix, which used unprefixed lookups and silently found nothing),
    # not assumed from the RDF spec alone.
    items = root.findall(f"./{RSS1_NS}item")
    if items:
        for item in items:
            title = (item.findtext(f"{RSS1_NS}title") or "").strip()
            link = (item.findtext(f"{RSS1_NS}link") or "").strip()
            if not title:
                title = derive_title(item.findtext(f"{RSS1_NS}description"))
            if title and link:
                pub_date = None
                date_text = item.findtext(f"{DC_NS}date")
                if date_text:
                    try:
                        pub_date = datetime.fromisoformat(date_text.strip())
                        if not pub_date.tzinfo:
                            pub_date = pub_date.replace(tzinfo=timezone.utc)
                    except ValueError:
                        pub_date = None
                yield {
                    "title": title,
                    "link": link,
                    "pub_date": pub_date,
                    "image": extract_rss_image(item),
                    "excerpt": extract_excerpt(item.findtext(f"{RSS1_NS}description")),
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


def scrape_rss(source: "sqlite3.Row", limit: int = MAX_ARTICLES_PER_FETCH) -> list[Article]:
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

    # Same optional noise filter scrape_css applies (see its own comment for
    # why it's a regex checked before capping to `limit`, not after) -- an
    # RSS feed can just as easily mix relevant and irrelevant items as a CSS
    # listing page (e.g. ESA's jobs feed has no working topic filter of its
    # own, so every posting comes through and this is the only filter that
    # actually narrows it down).
    if source["title_filter"]:
        entries = [e for e in entries if re.search(source["title_filter"], e["title"], re.I)]

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
        for entry in entries[:limit]
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


JET_ENGINE_SETTINGS_RE = re.compile(r'data-nav="([^"]*)"')
JET_ENGINE_QUERIED_ID_RE = re.compile(r'data-queried-id="([^"]+)"')
JET_ENGINE_PAGES_RE = re.compile(r'data-pages="(\d+)"')
JET_ENGINE_GRID_WIDGET_RE = re.compile(
    r'class="[^"]*elementor-widget-jet-listing-grid[^"]*"[^>]*data-id="([0-9a-f]+)"'
)


def _php_form_fields(prefix: str, value, out: list[tuple[str, str]]) -> None:
    """Flattens a Python value (from JSON) into PHP/WordPress's bracket-array
    form-encoding (key[sub]=..., key[]=... per list item) -- the shape
    JetEngine's own frontend JS sends its AJAX pagination requests in (see
    _fetch_jetengine_extra_pages). A JSON boolean becomes the literal string
    "true"/"false" (not Python's True/False, and not dropped) since that's
    what the real browser-sent payload contains for e.g. widget_settings
    fields like "inline_columns_css":false -- confirmed against a real
    captured request, not assumed."""
    if isinstance(value, dict):
        for k, v in value.items():
            _php_form_fields(f"{prefix}[{k}]", v, out)
    elif isinstance(value, list):
        for item in value:
            _php_form_fields(f"{prefix}[]", item, out)
    elif isinstance(value, bool):
        out.append((prefix, "true" if value else "false"))
    elif value is None:
        out.append((prefix, ""))
    else:
        out.append((prefix, str(value)))


def _fetch_jetengine_extra_pages(page1_html: str, source: "sqlite3.Row") -> list:
    """For sources.paginate=1: JetEngine's "Load More" listing widget (used
    by both the Chambéry and Lyon 1 job sources so far) never puts page 2+
    in a real URL at all -- everything past page 1 only exists behind a
    same-origin AJAX POST (action=jet_engine_ajax, handler=listing_load_more)
    whose payload is almost entirely the widget's own settings, verbatim.
    Rather than hand-guess that payload (tried once, 404'd -- see CLAUDE.md's
    "Still page-1-only" note this superseded), every value needed is
    extracted fresh from page 1's own HTML on every scrape:
      - the `query`/`widget_settings` dicts, from the listing wrapper's own
        `data-nav="..."` JSON blob (HTML-entity-escaped) -- confirmed by
        inspecting a real page: this is the *exact* JSON the widget's own
        JS reads to build its request, so replaying it back is inherently
        correct, not a guess. Not `data-settings` (a much more generic
        attribute used all over an Elementor page for unrelated widgets) --
        an easy mix-up caught only by re-checking the real HTML byte for
        byte after the first regex silently matched nothing.
      - `page_settings.queried_id`, from the listing wrapper's own
        `data-queried-id="331|WP_Post"`-shaped attribute.
      - `page_settings.element_id`, from the *widget's own* Elementor
        `data-id` (the `elementor-widget-jet-listing-grid` div) -- a
        different id than `widget_settings._element_id` (a fixed JetEngine
        string like "joblisting-list"), easy to conflate.
      - total page count, from `data-pages="N"` on the same wrapper.
    Verified this actually works with a real request (dropping the
    session cookie a browser capture included -- turned out unnecessary,
    this endpoint needs no auth/session at all) before relying on it here,
    not assumed from the payload shape alone.

    Returns every page-2+ container matching source["article_selector"],
    to be appended to page 1's own containers. Never raises -- a paginated
    source that fails to extract/fetch extra pages still returns whatever
    page 1 alone gave scrape_css, same graceful-degradation shape as every
    other optional field on a source."""
    settings_match = JET_ENGINE_SETTINGS_RE.search(page1_html)
    queried_id_match = JET_ENGINE_QUERIED_ID_RE.search(page1_html)
    element_id_match = JET_ENGINE_GRID_WIDGET_RE.search(page1_html)
    pages_match = JET_ENGINE_PAGES_RE.search(page1_html)
    if not (settings_match and queried_id_match and element_id_match and pages_match):
        logger.warning(
            f"{source['name']}: paginate=1 but couldn't find JetEngine's load-more "
            "settings on page 1 -- the page's markup may have changed. Scraping page 1 only."
        )
        return []

    total_pages = int(pages_match.group(1))
    if total_pages <= 1:
        return []

    try:
        settings = json.loads(html.unescape(settings_match.group(1)))
        query, widget_settings = settings["query"], settings["widget_settings"]
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"{source['name']}: couldn't parse JetEngine's embedded settings JSON: {e}")
        return []

    queried_id = queried_id_match.group(1)
    element_id = element_id_match.group(1)
    listing_id = widget_settings.get("lisitng_id", "")  # sic -- real typo'd field name in the payload
    user_agent = source["user_agent"] or USER_AGENT

    extra: list = []
    for page_num in range(2, total_pages + 1):
        fields: list[tuple[str, str]] = [("action", "jet_engine_ajax"), ("handler", "listing_load_more")]
        _php_form_fields("query", query, fields)
        _php_form_fields("widget_settings", widget_settings, fields)
        _php_form_fields(
            "page_settings",
            {"post_id": False, "queried_id": queried_id, "element_id": element_id, "page": page_num},
            fields,
        )
        fields += [("listing_type", "false"), ("isEditMode", "false"), ("addedPostCSS[]", listing_id)]

        try:
            resp = requests.post(
                source["url"].split("?")[0] + "?nocache=1",
                data=fields,
                headers={"X-Requested-With": "XMLHttpRequest", "User-Agent": user_agent},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            page_html = resp.json()["data"]["html"]
        except (requests.RequestException, ValueError, KeyError) as e:
            logger.warning(f"{source['name']}: page {page_num} of {total_pages} failed to load: {e}")
            break

        page_soup = BeautifulSoup(page_html, "html.parser")
        extra.extend(page_soup.select(source["article_selector"]))

    return extra


def scrape_css(source: "sqlite3.Row", limit: int = MAX_ARTICLES_PER_FETCH) -> list[Article]:
    """Scrape a single CSS-selector-configured source, most recent first
    (order as they appear on the page)."""
    user_agent = source["user_agent"] or USER_AGENT
    html = fetch_html(source["url"], user_agent)
    if not html:
        raise ScrapeError(f"Could not fetch {source['url']}")

    soup = BeautifulSoup(html, "html.parser")
    containers = soup.select(source["article_selector"])
    if source["paginate"]:
        containers = containers + _fetch_jetengine_extra_pages(html, source)

    articles: list[Article] = []
    for container in containers:
        if len(articles) >= limit:
            # Capping the *matching* count, not the raw container count --
            # with a title_filter set, a relevant posting could easily sit
            # past the page's first `limit` raw entries (a busy listing
            # page, most of it irrelevant). Slicing containers up front,
            # before filtering, would silently never see anything past
            # that point; scanning everything and capping the kept results
            # instead means a filtered source actually finds its matches
            # wherever they are on the page, not just among the first few.
            # For a source with no title_filter this is a no-op -- every
            # container matches, so it still stops at the same `limit`'th
            # one, same order as before.
            break
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

        # Optional per-source noise filter for a listing page that mixes
        # relevant and irrelevant postings (e.g. a whole university's job
        # board) -- a regex (case-insensitive), so "communication|médiation"
        # expresses an OR the same way CONTENT_TAG_RULES's patterns already
        # do elsewhere in this file. None/blank means "keep everything",
        # same as every other optional selector field. Unlike job_tags
        # classification (which tags *for display filtering* but still
        # stores every posting), this skips the article before it's ever
        # persisted -- for a source whose entire listing you never want to
        # see, not just want tagged differently.
        if source["title_filter"] and not re.search(source["title_filter"], title, re.I):
            continue

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

        city = None
        if source["location_selector"]:
            location_el = container.select_one(source["location_selector"])
            if location_el:
                city = location_el.get_text(" ", strip=True)

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
                city=city,
            )
        )

    return articles


def scrape_email(source: "sqlite3.Row") -> list[Article]:
    """kind="email": IMAP-fetched, not a URL fetch at all -- source["url"]
    is just a human-readable label for this source row (schema requires a
    non-null url), never actually requested. See email_import.py for why
    this exists instead of scraping Indeed's site directly (Cloudflare-
    blocked) and for the IMAP/parsing mechanics themselves; this function
    is only the thin Article-construction wrapper, kept here (not in
    email_import.py) so Article stays owned by one module, matching every
    other scrape_* function's shape.

    A `jk=`-bearing link's stored `link` is kept exactly as Indeed sent it
    (tracking tokens and all) -- an earlier version rewrote it down to a
    bare canonical `viewjob?jk=` form so plain link-based `INSERT OR
    IGNORE` dedup would catch a repeat; that broke real clicks (Indeed's
    bot-detection 401s a bare `/viewjob?jk=` hit, see
    email_import.extract_indeed_jk's docstring), so dedup for these now
    happens here instead, via an explicit `jk` lookup against already-
    stored links (`link LIKE '%jk=<key>%'`) rather than exact-link
    equality -- a repeat is skipped *before* ever reaching
    persist_articles(), regardless of how different its tracking params
    look from the first time this job was seen.
    A sponsored "pagead" link has no stable id at all (confirmed directly:
    the same posting's link differs completely, tracking token included,
    across separate emails), so neither link-equality nor a `jk` lookup can
    catch a repeat of one of those. Rather than silently creating a
    duplicate article (the bug reported: "some indeed jobs are imported
    multiple times") or silently guessing two same-titled postings are the
    same job (they might not be), a pagead job whose title exactly matches
    an already-active job/thesis-offer article is *not* returned as an
    Article at all -- it's staged in pending_indeed_duplicates instead,
    for the user to manually import or discard from the Jobs tab. INSERT
    OR IGNORE on that table's own UNIQUE(link) makes this safe to
    re-run indefinitely, same as the rest of this app's email import --
    fetch_indeed_jobs() re-reads every message every run, so without this
    the identical candidate would otherwise get re-staged on every single
    scrape after being discarded once (see that table's own schema
    comment in db.py for why `status` isn't a plain delete-on-resolve)."""
    jobs = email_import.fetch_indeed_jobs()
    articles = []
    # Jobs already accepted earlier in THIS batch. Nothing is persisted until
    # the caller runs persist_articles() (which returns the new ids the
    # scheduler notifies about), so the DB checks below can't see a copy from
    # the same digest -- these sets cover that gap. Without them, a job that
    # appears several times in one digest (same jk, or the same pagead title)
    # was created once per copy: four identical "Adaptation au changement
    # climatique" rows in one scrape.
    seen_jks = set()
    seen_titles = set()
    conn = db.get_conn()
    try:
        for job in jobs:
            jk = email_import.extract_indeed_jk(job["link"])
            title_key = job["title"].strip().lower()
            if jk:
                if jk in seen_jks:
                    continue
                already_have = conn.execute(
                    "SELECT 1 FROM articles WHERE link LIKE ?", (f"%jk={jk}%",)
                ).fetchone()
                if already_have:
                    continue
                seen_jks.add(jk)
            else:
                existing = conn.execute(
                    "SELECT articles.id FROM articles JOIN sources ON sources.id = articles.source_id "
                    "WHERE sources.category = 'job' AND articles.archived = 0 "
                    "AND lower(trim(articles.title)) = lower(trim(?))",
                    (job["title"],),
                ).fetchone()
                if existing or title_key in seen_titles:
                    # Same-run repeat has no DB id yet, so matched_article_id stays NULL.
                    conn.execute(
                        "INSERT OR IGNORE INTO pending_indeed_duplicates "
                        "(source_id, title, link, pub_date, matched_article_id) VALUES (?, ?, ?, ?, ?)",
                        (
                            source["id"], job["title"], job["link"],
                            job["pub_date"].isoformat() if job["pub_date"] else None,
                            existing["id"] if existing else None,
                        ),
                    )
                    continue
                seen_titles.add(title_key)
            article = Article(
                title=job["title"],
                link=job["link"],
                source_id=source["id"],
                source_name=source["name"],
                pub_date=job["pub_date"],
                category=source["category"],
                forced_job_tag=source["forced_job_tag"],
            )
            articles.append(article)
        conn.commit()
    finally:
        conn.close()
    return articles


def scrape_inspirehep(source: "sqlite3.Row", limit: int = MAX_ARTICLES_PER_FETCH) -> list[Article]:
    """kind="inspirehep": InspireHEP (the HEP-physics literature/jobs
    database run by CERN/DESY/Fermilab/SLAC et al.) has a real, public,
    documented JSON REST API -- source["url"] is a full API query URL
    (e.g. `https://inspirehep.net/api/jobs?sort=mostrecent&size=25&...
    &status=open&rank=PHD&field_of_interest=hep-ex`), not a page to
    CSS-scrape, same "prefer a real API over scraping HTML" preference
    this app already has for Indeed (see email_import.py). A dedicated
    kind/function rather than a generic "json" source, matching how
    scrape_rss hardcodes RSS/Atom's shape rather than being configurable
    -- InspireHEP's JSON shape is this specific site's, not a general
    contract. No selector fields used at all (article_selector etc. stay
    NULL for this kind); `limit` still caps how many are kept, same as
    every other kind."""
    user_agent = source["user_agent"] or USER_AGENT
    resp = _fetch(source["url"], user_agent)
    if resp is None:
        raise ScrapeError(f"Could not fetch {source['url']}")
    try:
        hits = resp.json()["hits"]["hits"]
    except (ValueError, KeyError) as e:
        raise ScrapeError(f"Unexpected InspireHEP response shape: {e}")

    articles: list[Article] = []
    for hit in hits[:limit]:
        meta = hit.get("metadata", {})
        title = (meta.get("position") or "").strip()
        record_id = hit.get("id")
        if not title or not record_id:
            continue

        pub_date = None
        created = hit.get("created")
        if created:
            try:
                pub_date = datetime.fromisoformat(created)
            except ValueError:
                pub_date = None

        deadline = None
        deadline_date = meta.get("deadline_date")
        if deadline_date:
            try:
                deadline = datetime.strptime(deadline_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                deadline = None

        articles.append(
            Article(
                title=title,
                link=f"https://inspirehep.net/jobs/{record_id}",
                source_id=source["id"],
                source_name=source["name"],
                pub_date=pub_date,
                deadline=deadline,
                excerpt=extract_excerpt(meta.get("description")),
                category=source["category"],
                forced_job_tag=source["forced_job_tag"],
            )
        )

    return articles


def scrape_doctorat_gouv(source: "sqlite3.Row", limit: int = MAX_ARTICLES_PER_FETCH) -> list[Article]:
    """kind="doctorat_gouv": the "Plateforme nationale du doctorat"
    (app.doctorat.gouv.fr), a government-run Angular SPA with a real,
    public, same-origin JSON API at `/api` -- undocumented (no public
    API docs found as of this writing), discovered by reading the
    page's own bundled `main-*.js` for its Angular services' base URLs,
    same "prefer a real API over scraping HTML" preference this app
    already has for Indeed/InspireHEP. source["url"] is a full
    `/api/propositions-these` query URL, filtered to exactly one
    laboratory via its *exact* `laboratoire` filter value (see
    `/api/filters/all`'s own `laboratoire` list -- matching is
    whole-string, not fuzzy or substring). A lab not yet on that list,
    or with zero current subjects, returns a real, correctly-empty
    result rather than an error -- the platform is still beta and many
    labs' subjects still only go through ADUM directly, not this
    platform yet (12 of the 15 labs requested when this was set up
    matched the filter list at all; only one, IJCLab, had any open
    subjects -- see CLAUDE.md "Adding a source").

    `link` is deliberately the platform's OWN stable detail-page URL
    (`/proposition?id=<id>`, confirmed to resolve with a 200), not the
    subject's own `urlCandidature` field -- that field is frequently an
    ADUM URL, because this platform mirrors ADUM submissions, and this
    app already has two ADUM sources (`ADUM: particule`, `ADUM
    Paris-Saclay: PHENIICS`) that could independently discover the
    exact same subject under its *original* ADUM link. Using the
    platform's own id-keyed URL instead means this source's links can
    never collide with an ADUM source's links for the same subject --
    a plain, one-way improvement over relying on exact-link dedup to
    catch it, not a workaround for a problem left otherwise unhandled.

    Every date field here (`dateMiseEnLigne`, `dateLimiteCandidature`)
    comes back as a naive ISO datetime with no UTC offset -- same
    `.replace(tzinfo=timezone.utc)` treatment as every other naive
    source date in this file (see scrape_inspirehep's `deadline_date`
    just above), not because the platform's own clock is actually UTC,
    but so every Article.pub_date/.deadline in this codebase stays
    uniformly tz-aware regardless of its source's real precision."""
    user_agent = source["user_agent"] or USER_AGENT
    resp = _fetch(source["url"], user_agent)
    if resp is None:
        raise ScrapeError(f"Could not fetch {source['url']}")
    try:
        items = resp.json()["content"]
    except (ValueError, KeyError) as e:
        raise ScrapeError(f"Unexpected doctorat.gouv.fr response shape: {e}")

    articles: list[Article] = []
    for item in items[:limit]:
        title = (item.get("theseTitre") or "").strip()
        subject_id = item.get("id")
        if not title or subject_id is None:
            continue

        pub_date = None
        raw_date = item.get("dateMiseEnLigne") or item.get("dateCreation")
        if raw_date:
            try:
                pub_date = datetime.fromisoformat(raw_date).replace(tzinfo=timezone.utc)
            except ValueError:
                pub_date = None

        deadline = None
        raw_deadline = item.get("dateLimiteCandidature")
        if raw_deadline:
            try:
                deadline = datetime.fromisoformat(raw_deadline).replace(tzinfo=timezone.utc)
            except ValueError:
                deadline = None

        articles.append(
            Article(
                title=title,
                link=f"https://app.doctorat.gouv.fr/proposition?id={subject_id}",
                source_id=source["id"],
                source_name=source["name"],
                pub_date=pub_date,
                deadline=deadline,
                excerpt=extract_excerpt(item.get("resume")),
                category=source["category"],
                forced_job_tag=source["forced_job_tag"],
                city=(item.get("etablissementVille") or "").strip() or None,
            )
        )

    return articles


def scrape_source(source: "sqlite3.Row") -> list[Article]:
    # kind="email" is excluded on purpose -- IMAP fetches whatever's
    # UNSEEN, there's no "N most recent items" concept to raise for a
    # first run the way a URL fetch has.
    limit = FIRST_SCRAPE_LIMIT if source["last_checked_at"] is None else MAX_ARTICLES_PER_FETCH
    if source["kind"] == "rss":
        return scrape_rss(source, limit=limit)
    if source["kind"] == "email":
        return scrape_email(source)
    if source["kind"] == "inspirehep":
        return scrape_inspirehep(source, limit=limit)
    if source["kind"] == "doctorat_gouv":
        return scrape_doctorat_gouv(source, limit=limit)
    return scrape_css(source, limit=limit)


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
            archived = 1 if job_tag in AUTO_ARCHIVE_JOB_TAGS else 0
            cur = conn.execute(
                "INSERT OR IGNORE INTO articles "
                "(source_id, title, link, pub_date, image_url, content_tag, deadline, job_tag, excerpt, remuneration, city, archived) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    a.source_id, a.title, a.link, pub_date.isoformat(),
                    a.image_url, a.content_tag,
                    a.deadline.isoformat() if a.deadline else None,
                    job_tag, a.excerpt, a.remuneration, a.city, archived,
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
        remuneration=row["remuneration"] if "remuneration" in row.keys() else None,
    )
    article.id = row["id"]
    return article


def get_articles(
    only_notified: bool | None = None,
    ids: list[int] | None = None,
    include_archived: bool = False,
    category: str | None = None,
    exclude_category: str | None = None,
) -> list[Article]:
    """Read articles back out of the DB, newest first.

    only_notified=False -> only articles not yet sent out (new since last
    digest run); ids -> only these specific article ids (used to re-fetch
    the rows persist_articles() just inserted, already joined to source
    names). include_archived mirrors web.py's list_articles() -- archived
    articles are excluded by default from both, since an archived job
    listing showing up in a digest download or the aggregate RSS feed
    would defeat the point of archiving it. category/exclude_category
    mirror list_articles()' own same-named params -- added so the RSS feed
    can be split in two (Feed's news vs Jobs' listings, see
    download_rss_feed/download_jobs_rss_feed in web.py) instead of one
    feed mixing both, the same category split every other view in this app
    already has."""
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
        if category:
            clauses.append("sources.category = ?")
            params.append(category)
        if exclude_category:
            clauses.append("sources.category != ?")
            params.append(exclude_category)
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
