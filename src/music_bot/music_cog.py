import concurrent.futures
import logging
import os
from collections import deque

from discord.ext import commands

from cache_commands_mixin import CacheCommandsMixin
from config import DATABASE_FILE, SONG_CACHE_DIR
from database_manager import DatabaseManager
from formatting import format_duration
from playback_mixin import PlaybackMixin
from queue_commands_mixin import QueueCommandsMixin
from song_cache import SongCache
from stats_commands_mixin import StatsCommandsMixin
from voice_commands_mixin import VoiceCommandsMixin
from voice_lifecycle_mixin import VoiceLifecycleMixin

logger = logging.getLogger(__name__)


class MusicCog(
    PlaybackMixin,
    VoiceCommandsMixin,
    QueueCommandsMixin,
    StatsCommandsMixin,
    CacheCommandsMixin,
    VoiceLifecycleMixin,
    commands.Cog,
):
    def __init__(self, bot):
        self.bot = bot
        self.queues = {}
        self.current_song = {}
        self.voice_clients = {}
        self.last_activity = {}
        self.is_shutting_down = False
        self.song_cache = SongCache(SONG_CACHE_DIR)
        cpu_cores = os.cpu_count() or 1
        max_workers = max(1, cpu_cores // 4)
        logger.info(f"Initializing ProcessPoolExecutor with max_workers={max_workers}")
        self.process_executor = concurrent.futures.ProcessPoolExecutor(max_workers=max_workers)
        # ThreadPoolExecutor for download operations - avoids pickling issues with yt-dlp
        # when downloading (especially for SoundCloud, where yt-dlp returns unpickleable objects)
        self.thread_executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

        self.db_manager = DatabaseManager(DATABASE_FILE)

        self.inactivity_check.start()

    def begin_shutdown(self):
        self.is_shutting_down = True

    def cog_unload(self):
        self.begin_shutdown()
        logger.info("Shutting down ProcessPoolExecutor...")
        self.process_executor.shutdown(wait=False, cancel_futures=True)
        logger.info("Shutting down ThreadPoolExecutor...")
        self.thread_executor.shutdown(wait=False, cancel_futures=True)
        logger.info("Cancelling inactivity check task.")
        self.inactivity_check.cancel()

    def get_queue(self, guild_id):
        """Gets the queue for a guild, creating it if it doesn't exist."""
        if guild_id not in self.queues:
            self.queues[guild_id] = deque()
        return self.queues[guild_id]

    def _format_duration(self, seconds: float) -> str:
        return format_duration(seconds)
