"""
The daily job: scrape every source, persist new articles, and notify both
configured channels (Discord + email) with only what's actually new since
the last run. Scheduled via discord.ext.tasks at whatever time is configured
in Settings (default 8:00 Europe/Paris); also runnable on demand via
!forcedigest or POST /api/run-now.
"""

import logging
from datetime import time as dtime
from zoneinfo import ZoneInfo

from discord.ext import tasks

import db
import digest
import mailer
import scraper
import discord_bot

logger = logging.getLogger("news_bot.scheduler")


async def run_daily_job() -> bool:
    """Scrape all sources, persist new articles, and notify Discord + email
    subscribers if there's anything new. Returns whether anything was sent."""
    logger.info("Running daily job: scraping all sources...")
    new_ids = scraper.scrape_and_persist_all()

    if not new_ids:
        logger.info("No new articles since the last run.")
        return False

    new_articles = scraper.get_articles(ids=new_ids)
    logger.info(f"{len(new_articles)} new article(s) found.")

    sent_discord = await discord_bot.notify_discord(new_articles)

    html_body = digest.render_digest_html(new_articles, page_title="New Particle Physics News")
    sent_emails = mailer.send_digest_email(
        subject=f"Particle Physics News — {len(new_articles)} new article(s)",
        html_body=html_body,
    )
    if sent_emails:
        logger.info(f"Emailed digest to {sent_emails} subscriber(s).")

    scraper.mark_notified(new_ids)
    return sent_discord or bool(sent_emails)


def _digest_schedule_time() -> dtime:
    hour = int(db.get_setting("digest_hour", "8"))
    minute = int(db.get_setting("digest_minute", "0"))
    tz = ZoneInfo(db.get_setting("digest_timezone", "Europe/Paris"))
    return dtime(hour=hour, minute=minute, tzinfo=tz)


# tasks.loop needs its `time=` argument at decoration time, so the schedule
# is fixed to whatever Settings held when the process started -- changing
# the digest time via the web UI takes effect on the next restart, not live.
# (A live-reschedule would mean tearing down and recreating this Loop object
# mid-run; not worth the complexity for a value that changes rarely.)
@tasks.loop(time=_digest_schedule_time())
async def daily_digest_task():
    await run_daily_job()


@daily_digest_task.before_loop
async def before_daily_digest_task():
    await discord_bot.bot.wait_until_ready()


def start():
    if not daily_digest_task.is_running():
        daily_digest_task.start()
    logger.info(f"Daily digest scheduled for {_digest_schedule_time()}")
