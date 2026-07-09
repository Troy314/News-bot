"""
Discord News Scraper Bot
=========================

Follows particle-physics / gravitational-wave news sources and:
  1. Posts them on demand with a command  ->  !news <site> / !digest
  2. Automatically posts an HTML digest of every source to a configured
     channel every morning, pinging @everyone.

Two kinds of sources are supported (see SOURCES below):
  - "rss":  a straight RSS/Atom feed URL. Preferred whenever a site has one,
            since it's far more reliable than scraping rendered HTML.
  - "css":  CSS-selector scraping of a plain HTML page, for sites with no
            discoverable feed. Only use this as a fallback.

--------------------------------------------------------------------------
SETUP
--------------------------------------------------------------------------
1. pip install -r requirements.txt   (discord.py, requests, beautifulsoup4)
2. Create a `.env` file (or set env vars) with:
       DISCORD_TOKEN=your_bot_token_here
       NEWS_CHANNEL_ID=123456789012345678   # channel for the daily digest post
       DIGEST_TIMEZONE=Europe/Paris          # IANA tz name, default Europe/Paris
       DIGEST_HOUR=9                          # local hour (0-23), default 9
       DIGEST_MINUTE=0                        # local minute, default 0
3. Edit the SOURCES dict below to add/remove sources.
4. Run:  python news_bot.py

--------------------------------------------------------------------------
HOW CSS SELECTORS WORK (kind="css" sources only)
--------------------------------------------------------------------------
For each site you provide:
  - "url":              the page to scrape
  - "article_selector":  CSS selector matching each article "card"/container
  - "title_selector":    CSS selector (relative to each article) for the title text
  - "link_selector":     CSS selector (relative to each article) for the <a> tag
                          (often the same element as the title, or its parent)
  - "base_url":          prepended to relative links (e.g. "https://example.com")
  - "date_selector":     optional. CSS selector (relative to each article) for a
                          date element. Uses its `datetime` attribute if present
                          (e.g. <time datetime="...">), else falls back to
                          scanning its text for a "Month D, YYYY" or "MM.DD.YYYY"
                          pattern.
  - "image_selector":    optional. CSS selector (relative to each article) for
                          an <img> tag; its `src` is used as the showcase image.
  - "user_agent":         optional. Overrides USER_AGENT for this source only,
                          for sites that block non-browser User-Agent strings
                          (e.g. interactions.org's feed 403s the default UA).
                          This also applies to "rss" sources.

Example for a fictional site where each article looks like:
    <div class="post-card">
        <a class="post-link" href="/news/123-big-story">
            <h2 class="post-title">Big Story Happened</h2>
        </a>
    </div>

    "article_selector": "div.post-card",
    "title_selector":   "h2.post-title",
    "link_selector":    "a.post-link",
"""

import os
import io
import re
import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, time as dtime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

import discord
from discord.ext import commands, tasks

from dotenv import load_dotenv
load_dotenv()

# --------------------------------------------------------------------------
# CONFIGURATION
# --------------------------------------------------------------------------

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "PUT_YOUR_TOKEN_HERE")
NEWS_CHANNEL_ID = int(os.environ.get("NEWS_CHANNEL_ID", "0"))  # channel for the daily digest post
DIGEST_TIMEZONE = ZoneInfo(os.environ.get("DIGEST_TIMEZONE", "Europe/Paris"))
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "9"))
DIGEST_MINUTE = int(os.environ.get("DIGEST_MINUTE", "0"))
COMMAND_PREFIX = "!"
MAX_ARTICLES_PER_FETCH = 10   # how many articles to grab from a source
REQUEST_TIMEOUT = 10         # seconds
USER_AGENT = "Mozilla/5.0 (compatible; NewsScraperBot/1.0)"

# EDIT THIS: add one entry per source you want to follow.
SOURCES = {
    "CERN": {
        "kind": "rss",
        "url": "https://home.cern/fr/feed/",
    },
    "ATLAS": {
        "kind": "rss",
        "url": "https://atlas.cern/updates/feed.xml",
    },
    "CMS": {
        "kind": "rss",
        "url": "https://cms.cern/taxonomy/term/1090/feed/all",
    },
    "LHCb": {
        "kind": "rss",
        "url": "https://lhcb-outreach.web.cern.ch/feed/",
    },
    "CERNCourier": {
        "kind": "rss",
        "url": "https://cerncourier.com/feed/",
    },
    "IN2P3": {
        "kind": "rss",
        "url": "https://www.in2p3.cnrs.fr/rss.xml",
    },
    "Virgo": {
        "kind": "rss",
        "url": "https://www.virgo-gw.eu/feed/",
    },
    "MPG": {
        "kind": "rss",
        "url": "https://www.mpg.de/12642524/particle-physics.rss",
    },
    "ScienceNews": {
        "kind": "rss",
        "url": "https://www.sciencenews.org/topic/particle-physics/feed",
    },
    "Guardian": {
        "kind": "rss",
        "url": "https://www.theguardian.com/science/particlephysics/rss",
    },
    "PhysicsToday": {
        "kind": "rss",
        "url": "https://physicstoday.aip.org/particle-physics.rss",
    },
    "TheConversation": {
        "kind": "rss",
        "url": "https://theconversation.com/topics/particle-physics-1449/articles.atom",
    },
    "Nature": {
        "kind": "rss",
        "url": "https://www.nature.com/subjects/particle-physics/nature.rss",
    },
    "PhysOrg": {
        "kind": "rss",
        "url": "https://phys.org/rss-feed/physics-news/particle-physics/",
    },
    # Blocks the bot's own User-Agent string with a 403 but allows a
    # browser-like one -- override just for this source.
    "Interactions": {
        "kind": "rss",
        "url": "https://www.interactions.org/index.rss",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
    },
    # No usable news feed exists for these three (ALICE: Drupal site with
    # the aggregator module disabled; LIGO: /news.rss endpoint returns a
    # broken 406; IPPOG: /rss.xml is the generic site feed and mixes in
    # member-profile pages, not just news) -- fall back to scraping their
    # HTML news listing directly.
    "ALICE": {
        "kind": "css",
        "url": "https://alice.cern/index.php/news",
        "article_selector": "div.views-row",
        "title_selector": "h3 a",
        "link_selector": "h3 a",
        "base_url": "https://alice.cern",
        "date_selector": "h5 time",
        "image_selector": "img",
    },
    "LIGO": {
        "kind": "css",
        "url": "https://www.ligo.caltech.edu/news",
        "article_selector": "div.panel:has(.thumb)",
        "title_selector": "h3",
        "link_selector": "a",
        "base_url": "https://www.ligo.caltech.edu",
        "date_selector": "p strong",
        "image_selector": "img",
    },
    "IPPOG": {
        "kind": "css",
        "url": "https://ippog.org/news",
        "article_selector": "div.views-row",
        "title_selector": "h3.resourceTitle a",
        "link_selector": "h3.resourceTitle a",
        "base_url": "https://ippog.org",
        "date_selector": "time",
        "image_selector": "img",
    },
    # /rss.xml exists but is CNRS News' site-wide feed across all topics
    # (space, chemistry, etc), not scoped to particle physics -- scrape the
    # topic listing page directly instead.
    "CNRSNews": {
        "kind": "css",
        "url": "https://news.cnrs.fr/particle-physics",
        "article_selector": "div.views-row",
        "title_selector": ".views-field-title a",
        "link_selector": ".views-field-title a",
        "base_url": "https://news.cnrs.fr",
        "date_selector": ".date-display-single",
        "image_selector": ".views-field-field-visuel-principal img",
    },
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("news_bot")


# --------------------------------------------------------------------------
# SCRAPER
# --------------------------------------------------------------------------

class Article:
    def __init__(
        self,
        title: str,
        link: str,
        site: str,
        pub_date: datetime | None = None,
        image_url: str | None = None,
    ):
        self.title = title.strip()
        self.link = link.strip()
        self.site = site
        self.pub_date = pub_date
        self.image_url = image_url.strip() if image_url else None

    def __repr__(self):
        return f"<Article {self.site!r} {self.title!r}>"


def _fetch(url: str, user_agent: str = USER_AGENT) -> requests.Response | None:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": user_agent},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return None


def fetch_html(url: str, user_agent: str = USER_AGENT) -> str | None:
    resp = _fetch(url, user_agent)
    return resp.text if resp else None


def fetch_bytes(url: str, user_agent: str = USER_AGENT) -> bytes | None:
    resp = _fetch(url, user_agent)
    return resp.content if resp else None


ATOM_NS = "{http://www.w3.org/2005/Atom}"
MEDIA_NS = "{http://search.yahoo.com/mrss/}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"


def normalize_image_url(src: str) -> str:
    """Discord embeds require a fully-qualified URL; upgrade protocol-relative
    ("//host/...") image URLs, which some sites embed in their HTML."""
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


BARE_AMPERSAND_RE = re.compile(rb"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)")


def parse_feed_items(content: bytes):
    """Yield dicts with title/link/pub_date/image from RSS 2.0 or Atom feed bytes."""
    # Some feeds (e.g. cerncourier.com) ship literal "&" instead of "&amp;" in
    # item text, which is invalid XML and makes ET.fromstring reject the
    # whole document -- patch just the bare ampersands before parsing.
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


def scrape_rss(site_name: str) -> list[Article]:
    config = SOURCES[site_name]
    user_agent = config.get("user_agent", USER_AGENT)
    content = fetch_bytes(config["url"], user_agent)
    if not content:
        return []

    try:
        entries = list(parse_feed_items(content))
    except ET.ParseError as e:
        # Some sources intermittently return a truncated/malformed response
        # (rate limiting, a transient proxy error, etc) -- one retry clears
        # most of these rather than losing the source for this whole cycle.
        logger.warning(f"Feed for {site_name} failed to parse ({e}), retrying once...")
        content = fetch_bytes(config["url"], user_agent)
        if not content:
            return []
        try:
            entries = list(parse_feed_items(content))
        except ET.ParseError as e2:
            logger.error(f"Failed to parse feed for {site_name}: {e2}")
            return []

    return [
        Article(
            title=entry["title"],
            link=entry["link"],
            site=site_name,
            pub_date=entry["pub_date"],
            image_url=entry["image"],
        )
        for entry in entries[:MAX_ARTICLES_PER_FETCH]
    ]


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


def scrape_css(site_name: str) -> list[Article]:
    """Scrape a single CSS-selector-configured site and return a list of
    Article objects, most recent first (order as they appear on the page)."""
    config = SOURCES[site_name]

    html = fetch_html(config["url"], config.get("user_agent", USER_AGENT))
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    containers = soup.select(config["article_selector"])

    articles: list[Article] = []
    for container in containers[:MAX_ARTICLES_PER_FETCH]:
        title_el = container.select_one(config["title_selector"])
        link_el = container.select_one(config["link_selector"])

        if not title_el or not link_el:
            continue

        title = title_el.get_text(strip=True)
        href = link_el.get("href", "").strip()
        if not href:
            continue
        link = resolve_url(href, config["base_url"])
        if not title:
            continue

        pub_date = None
        if config.get("date_selector"):
            pub_date = parse_css_date(container.select_one(config["date_selector"]))

        image_url = None
        if config.get("image_selector"):
            image_el = container.select_one(config["image_selector"])
            src = image_el.get("src", "").strip() if image_el else ""
            if src:
                image_url = resolve_url(src, config["base_url"])

        articles.append(
            Article(title=title, link=link, site=site_name, pub_date=pub_date, image_url=image_url)
        )

    return articles


def scrape_site(site_name: str) -> list[Article]:
    config = SOURCES.get(site_name)
    if not config:
        raise ValueError(f"Unknown site: {site_name}")

    if config["kind"] == "rss":
        return scrape_rss(site_name)
    return scrape_css(site_name)


def scrape_all_sites() -> dict[str, list[Article]]:
    return {name: scrape_site(name) for name in SOURCES}


def resolve_source(name: str) -> str | None:
    """Case-insensitive lookup of a configured source name."""
    name = name.lower()
    for key in SOURCES:
        if key.lower() == name:
            return key
    return None


# --------------------------------------------------------------------------
# DISCORD BOT
# --------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True  # needed to read command text

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


def make_embed(article: Article) -> discord.Embed:
    embed = discord.Embed(
        title=article.title,
        url=article.link,
        color=discord.Color.blurple(),
        timestamp=article.pub_date or datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Source: {article.site}")
    if article.image_url:
        embed.set_image(url=article.image_url)
    return embed


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (id={bot.user.id})")
    logger.info(f"Configured sources: {', '.join(SOURCES.keys()) or '(none configured)'}")
    logger.info(f"Daily digest scheduled for {DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d} {DIGEST_TIMEZONE}")
    if not post_daily_digest.is_running():
        post_daily_digest.start()


@bot.command(name="news")
async def news_command(ctx: commands.Context, site: str = None):
    """Usage: !news <site_name>   or   !news all"""
    if not SOURCES:
        await ctx.send("No sources are configured yet. Edit the SOURCES dict in news_bot.py.")
        return

    if site is None:
        available = ", ".join(SOURCES.keys())
        await ctx.send(
            f"Please specify a site. Available sites: `{available}`\n"
            f"Or use `{COMMAND_PREFIX}news all` to check every configured site."
        )
        return

    site = site.lower()

    async with ctx.typing():
        if site == "all":
            results = await asyncio.to_thread(scrape_all_sites)
            any_sent = False
            for site_name, articles in results.items():
                if not articles:
                    continue
                any_sent = True
                await ctx.send(f"**Latest from {site_name}:**")
                for article in articles:
                    await ctx.send(embed=make_embed(article))
            if not any_sent:
                await ctx.send("Couldn't find any articles on the configured sites.")
            return

        resolved = resolve_source(site)
        if resolved is None:
            available = ", ".join(SOURCES.keys())
            await ctx.send(f"Unknown site `{site}`. Available: `{available}`")
            return

        articles = await asyncio.to_thread(scrape_site, resolved)
        if not articles:
            await ctx.send(f"No articles found for `{resolved}` (the feed/page may be temporarily unreachable).")
            return

        await ctx.send(f"**Latest from {resolved}:**")
        for article in articles:
            await ctx.send(embed=make_embed(article))


@bot.command(name="sites")
async def sites_command(ctx: commands.Context):
    """List all configured sites."""
    if not SOURCES:
        await ctx.send("No sites configured.")
        return
    lines = [f"- **{name}** ({cfg['kind']}) — {cfg['url']}" for name, cfg in SOURCES.items()]
    await ctx.send("Configured sites:\n" + "\n".join(lines))


async def build_digest_file() -> discord.File:
    # Imported lazily: generate_digest imports scrape_all_sites/Article back
    # from this module, so importing it at module load time would deadlock
    # on the circular import. By call time news_bot is fully initialized.
    import generate_digest

    html_page = await asyncio.to_thread(generate_digest.generate)
    buffer = io.BytesIO(html_page.encode("utf-8"))
    filename = f"digest-{datetime.now(DIGEST_TIMEZONE):%Y-%m-%d}.html"
    return discord.File(buffer, filename=filename)


@bot.command(name="digest")
async def digest_command(ctx: commands.Context):
    """Usage: !digest -- scrapes every configured source and posts a
    browsable HTML digest (month-grouped, filterable by source) as a file."""
    async with ctx.typing():
        file = await build_digest_file()
        await ctx.send(
            content="Here's the latest digest — download and open it in a browser.",
            file=file,
        )


@tasks.loop(time=dtime(hour=DIGEST_HOUR, minute=DIGEST_MINUTE, tzinfo=DIGEST_TIMEZONE))
async def post_daily_digest():
    """Every morning: scrape all sources, build the HTML digest, and post it
    to NEWS_CHANNEL_ID with an @everyone ping."""
    if NEWS_CHANNEL_ID == 0:
        return  # auto-posting disabled if no channel configured

    channel = bot.get_channel(NEWS_CHANNEL_ID)
    if channel is None:
        logger.warning(f"Could not find channel with id {NEWS_CHANNEL_ID}")
        return

    logger.info("Generating morning digest...")
    try:
        file = await build_digest_file()
        await channel.send(
            content="@everyone Good morning! Here's today's particle physics digest.",
            file=file,
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
        logger.info("Posted morning digest.")
    except discord.DiscordException as e:
        logger.error(f"Failed to send digest to Discord: {e}")


@post_daily_digest.before_loop
async def before_post_daily_digest():
    await bot.wait_until_ready()


# --------------------------------------------------------------------------
# ENTRYPOINT
# --------------------------------------------------------------------------

if __name__ == "__main__":
    if DISCORD_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise SystemExit(
            "Set the DISCORD_TOKEN environment variable (or edit the script) before running."
        )
    bot.run(DISCORD_TOKEN)
