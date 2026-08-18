"""
The Discord side of the app: on-demand commands (!news, !sites, !digest,
!forcedigest) plus notify_discord(), used by scheduler.py to post the daily
digest. Sources and articles all come from the DB (db.py/scraper.py) now,
not a hardcoded dict.
"""

import io
import os
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

import db
import digest
import scraper

logger = logging.getLogger("news_bot.discord")

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
COMMAND_PREFIX = "!"

intents = discord.Intents.default()
intents.message_content = True  # needed to read command text

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


def get_news_channel_id() -> int:
    return int(db.get_setting("news_channel_id", "0"))


def get_all_sources() -> list:
    conn = db.get_conn()
    try:
        return conn.execute("SELECT * FROM sources ORDER BY name").fetchall()
    finally:
        conn.close()


def get_source_by_name(name: str):
    conn = db.get_conn()
    try:
        return conn.execute(
            "SELECT * FROM sources WHERE lower(name) = lower(?)", (name,)
        ).fetchone()
    finally:
        conn.close()


def make_embed(article: scraper.Article) -> discord.Embed:
    embed = discord.Embed(
        title=article.title,
        url=article.link,
        color=discord.Color.blurple(),
        timestamp=article.pub_date or datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Source: {article.source_name}")
    if article.image_url:
        embed.set_image(url=article.image_url)
    return embed


def build_digest_file(articles: list[scraper.Article], page_title: str, filename_prefix: str) -> discord.File:
    html_page = digest.render_digest_html(articles, page_title=page_title)
    buffer = io.BytesIO(html_page.encode("utf-8"))
    filename = f"{filename_prefix}-{datetime.now(timezone.utc):%Y-%m-%d}.html"
    return discord.File(buffer, filename=filename)


async def notify_discord(articles: list[scraper.Article]) -> bool:
    """Post the given (already-new) articles to the configured channel as a
    downloadable digest file, pinging @everyone. Returns whether it sent."""
    channel_id = get_news_channel_id()
    if channel_id == 0:
        logger.info("news_channel_id isn't set -- skipping Discord notification.")
        return False

    channel = bot.get_channel(channel_id)
    if channel is None:
        logger.warning(f"Could not find Discord channel with id {channel_id}")
        return False

    file = build_digest_file(articles, "New Particle Physics News", "digest")
    try:
        await channel.send(
            content=f"@everyone {len(articles)} new article(s) today — here's the digest.",
            file=file,
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
        return True
    except discord.DiscordException as e:
        logger.error(f"Failed to send digest to Discord: {e}")
        return False


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (id={bot.user.id})")
    sources = get_all_sources()
    logger.info(f"Configured sources: {', '.join(s['name'] for s in sources) or '(none configured)'}")

    import scheduler  # lazy: scheduler imports this module too

    scheduler.start()


@bot.command(name="news")
async def news_command(ctx: commands.Context, site: str = None):
    """Usage: !news <site_name> -- live-scrapes one configured source and
    posts its current articles."""
    sources = get_all_sources()
    if not sources:
        await ctx.send("No sources are configured yet. Add one from the web app.")
        return

    if site is None:
        available = ", ".join(s["name"] for s in sources)
        await ctx.send(f"Please specify a site. Available: `{available}`")
        return

    async with ctx.typing():
        source = get_source_by_name(site)
        if source is None:
            available = ", ".join(s["name"] for s in sources)
            await ctx.send(f"Unknown site `{site}`. Available: `{available}`")
            return

        articles = scraper.scrape_source(source)
        scraper.persist_articles(articles)
        if not articles:
            await ctx.send(f"No articles found for `{source['name']}` (the feed/page may be temporarily unreachable).")
            return

        await ctx.send(f"**Latest from {source['name']}:**")
        for article in articles:
            await ctx.send(embed=make_embed(article))


@bot.command(name="sites")
async def sites_command(ctx: commands.Context):
    """List all configured sources."""
    sources = get_all_sources()
    if not sources:
        await ctx.send("No sources configured.")
        return
    lines = [f"- **{s['name']}** ({s['kind']}, {s['category']}) — {s['url']}" for s in sources]
    await ctx.send("Configured sources:\n" + "\n".join(lines))


@bot.command(name="digest")
async def digest_command(ctx: commands.Context):
    """Usage: !digest -- posts the full current digest (every article in the
    DB, month-grouped, filterable by source) as a file."""
    async with ctx.typing():
        articles = scraper.get_articles()
        file = build_digest_file(articles, "Particle Physics News", "digest")
        await ctx.send(
            content="Here's the full digest — download and open it in a browser.",
            file=file,
        )


@bot.command(name="forcedigest")
async def force_digest_command(ctx: commands.Context):
    """Usage: !forcedigest -- immediately runs the daily job (scrape, persist,
    and post any new articles with @everyone), without waiting for the
    schedule. Useful for testing the automatic pipeline on demand."""
    import scheduler  # lazy: scheduler imports this module too

    await ctx.send("Forcing the daily job now...")
    posted = await scheduler.run_daily_job()
    if not posted:
        await ctx.send("Done — no new articles since the last run.")
