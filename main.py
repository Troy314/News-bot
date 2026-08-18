"""
Entrypoint: runs the FastAPI web app (uvicorn) and the Discord bot together
in one process, one asyncio event loop. This is what should be launched on
PC login (see the autostart .desktop entry) or kept running as the
project's "local server."

Run:  python main.py
"""

import os
import logging
from pathlib import Path

from dotenv import load_dotenv

# Load .env from next to this file, not the process's working directory --
# matters when launched from autostart, where the cwd isn't guaranteed to
# be the project directory. Must happen before any other project module is
# imported, since db.py/discord_bot.py/mailer.py all read os.environ at
# import or call time.
load_dotenv(Path(__file__).resolve().parent / ".env")

import asyncio
import uvicorn

import db
import seed_sources
import discord_bot
import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("news_bot.main")

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))


async def run_web():
    config = uvicorn.Config(web.app, host=HOST, port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def run_discord():
    if not DISCORD_TOKEN:
        logger.warning("DISCORD_TOKEN isn't set -- Discord bot and daily notifications are disabled. "
                        "The web app will still run.")
        return
    async with discord_bot.bot:
        await discord_bot.bot.start(DISCORD_TOKEN)


async def main():
    db.init_db()
    n = seed_sources.seed()
    if n:
        logger.info(f"Seeded {n} default source(s) on first run.")

    logger.info(f"Web app: http://{HOST}:{PORT}")
    await asyncio.gather(run_web(), run_discord())


if __name__ == "__main__":
    asyncio.run(main())
