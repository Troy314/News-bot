"""
The daily job: scrape every source, persist new articles, and notify both
configured channels (Discord + email) with only what's actually new since
the last run. Scheduled via discord.ext.tasks at whatever time is configured
in Settings (default 8:00 Europe/Paris); also runnable on demand via
!forcedigest or POST /api/run-now.
"""

import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from discord.ext import tasks

import db
import digest
import mailer
import scraper
import discord_bot

logger = logging.getLogger("news_bot.scheduler")


def _current_timezone() -> ZoneInfo:
    return ZoneInfo(db.get_setting("digest_timezone", "Europe/Paris"))


def _mark_ran_today() -> None:
    """Record that the daily job ran today (regardless of whether anything was
    new) -- catch_up_if_missed() uses this to tell "already checked today" from
    "app wasn't running when it should have checked"."""
    today = datetime.now(_current_timezone()).strftime("%Y-%m-%d")
    db.set_setting("last_digest_run_date", today)


async def run_daily_job() -> bool:
    """Scrape all sources, persist new articles, and notify Discord + email
    subscribers if there's anything new. Returns whether anything was sent."""
    logger.info("Running daily job: scraping all sources...")
    new_ids = scraper.scrape_and_persist_all()

    if not new_ids:
        logger.info("No new articles since the last run.")
        _mark_ran_today()
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
    _mark_ran_today()
    return sent_discord or bool(sent_emails)


def _digest_schedule_time() -> dtime:
    hour = int(db.get_setting("digest_hour", "8"))
    minute = int(db.get_setting("digest_minute", "0"))
    return dtime(hour=hour, minute=minute, tzinfo=_current_timezone())


async def catch_up_if_missed() -> None:
    """Called once on startup: if the app wasn't running at today's scheduled
    time (PC was off, crashed, etc), run the daily job immediately instead of
    silently waiting until tomorrow's schedule -- "missed" news still gets
    sent, just late instead of never. A no-op if today's run already happened
    (scheduled or manual), or if today's scheduled time hasn't arrived yet."""
    now = datetime.now(_current_timezone())
    today = now.strftime("%Y-%m-%d")

    if db.get_setting("last_digest_run_date", "") == today:
        logger.info("Daily digest already ran today -- nothing missed.")
        return
    if now.time() < _digest_schedule_time().replace(tzinfo=None):
        logger.info("Today's scheduled digest time hasn't arrived yet -- nothing missed.")
        return

    logger.info(f"Missed today's {_digest_schedule_time().strftime('%H:%M')} digest "
                "(app wasn't running) -- catching up now.")
    await run_daily_job()


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
