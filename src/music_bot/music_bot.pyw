import asyncio
import logging

import discord
import nacl
import yt_dlp
from discord.ext import commands

from config import CACHE_DOWNLOADS_ENABLED, DISCORD_TOKEN, DATABASE_FILE, FFMPEG_EXECUTABLE, LOG_FILE, MAX_SONG_DURATION_SECONDS
from database_manager import DatabaseManager
from formatting import format_duration
from logging_config import setup_logging
from music_cog import MusicCog
from web_server import start_web_server

logger = logging.getLogger(__name__)


def log_startup_configuration():
    logger.info(f"Using FFmpeg executable located at: {FFMPEG_EXECUTABLE}")
    logger.info(f"Database file located at: {DATABASE_FILE}")
    logger.info(f"Log file located at: {LOG_FILE}")
    logger.info(
        f"Maximum cache download duration: {MAX_SONG_DURATION_SECONDS} seconds "
        f"({format_duration(MAX_SONG_DURATION_SECONDS)})"
    )
    if CACHE_DOWNLOADS_ENABLED:
        logger.info("Cache mode: download missing songs and reuse existing cached songs.")
    else:
        logger.info("Cache mode: reuse existing cached songs, stream uncached songs.")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, case_insensitive=True)


@bot.event
async def on_ready():
    """Called when the bot is ready and connected to Discord."""
    db_manager = DatabaseManager(DATABASE_FILE)
    db_manager.cleanup_queued_songs()

    logger.info(f'Logged in as {bot.user.name} ({bot.user.id})')
    logger.info(f'Discord.py Version: {discord.__version__}')
    logger.info(f'PyNaCl Version: {nacl.__version__}')
    logger.info(f'yt-dlp Version: {yt_dlp.version.__version__}')
    logger.info('-------------------')
    logger.info('Bot is ready and online.')
    logger.info('-------------------')

    await bot.change_presence(activity=discord.Game(name="Music | !help"))


async def main():
    if not DISCORD_TOKEN or DISCORD_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.critical("DISCORD_BOT_TOKEN not found or not set correctly. Halting.")
        return

    async with bot:
        await bot.add_cog(MusicCog(bot))

        web_server_task = asyncio.create_task(start_web_server(bot))

        logger.info("Starting bot...")
        try:
            await bot.start(DISCORD_TOKEN)
        except discord.LoginFailure:
            logger.critical("Login failed: Invalid Discord token provided.")
        finally:
            logger.info("Bot has been closed. Cleaning up remaining tasks.")
            if not web_server_task.done():
                web_server_task.cancel()


if __name__ == "__main__":
    setup_logging(log_file_path=LOG_FILE)
    log_startup_configuration()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received. Shutting down.")
    finally:
        logger.info("Application has finished.")
