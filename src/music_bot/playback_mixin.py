import asyncio
import concurrent.futures
import logging
import os
import time

import discord
import yt_dlp

from config import FFMPEG_EXECUTABLE, MAX_SONG_DURATION_SECONDS, SONG_CACHE_DIR
from youtube import FFMPEG_OPTIONS, is_livestream_info, run_yt_dlp_extractor, run_yt_dlp_search, run_yt_dlp_search_results

logger = logging.getLogger(__name__)


class PlaybackMixin:
    async def _search_results(self, query, result_count=5):
        """Searches yt-dlp for flat video results without blocking the event loop."""
        if getattr(self, 'is_shutting_down', False):
            logger.info("Skipping search results because shutdown is in progress.")
            return []

        loop = asyncio.get_event_loop()
        try:
            logger.debug(f"Submitting yt-dlp result search for '{query}' to process pool.")
            results = await loop.run_in_executor(
                self.process_executor,
                run_yt_dlp_search_results,
                query,
                result_count
            )
            logger.debug(f"Successfully retrieved {len(results)} search result(s) for '{query}'.")
            return results
        except Exception as e:
            logger.exception(f"Unexpected error while searching for '{query}': {e}")
            return []

    async def _extract_info(self, query, download=False):
        """Extracts info using yt-dlp in an executor to avoid blocking."""
        if getattr(self, 'is_shutting_down', False):
            logger.info("Skipping info extraction because shutdown is in progress.")
            return None

        loop = asyncio.get_event_loop()
        try:
            logger.debug(f"Submitting yt-dlp search for '{query}' to process pool.")
            data = await loop.run_in_executor(
                self.process_executor,
                run_yt_dlp_search,
                query
            )
            logger.debug(f"Successfully retrieved search data for '{query}' from process pool.")

            if getattr(self, 'is_shutting_down', False):
                logger.info("Discarding extracted info because shutdown is in progress.")
                return None

            if not data:
                logger.warning(f"Search returned no data for '{query}'.")
                return None

            if 'entries' in data:
                logger.info(f"Found multiple entries for '{query}', using first result.")
                data = data['entries'][0]

            if is_livestream_info(data):
                logger.warning(f"Refusing livestream '{data.get('title', 'Unknown')}' for query '{query}'.")
                return {
                    'error': 'livestream',
                    'title': data.get('title', 'Unknown Title'),
                    'webpage_url': data.get('webpage_url', query)
                }

            duration = data.get('duration')
            if duration and duration > MAX_SONG_DURATION_SECONDS:
                logger.warning(f"Song '{data.get('title', 'Unknown')}' exceeds maximum duration limit "
                             f"({duration}s > {MAX_SONG_DURATION_SECONDS}s)")
                return {
                    'error': 'duration_exceeded',
                    'title': data.get('title', 'Unknown Title'),
                    'duration': duration,
                    'max_duration': MAX_SONG_DURATION_SECONDS,
                    'webpage_url': data.get('webpage_url', query)
                }

            youtube_id = data.get('id')
            if not youtube_id:
                logger.warning(f"Could not extract YouTube ID for '{query}'.")
                return None

            cached_file = self.song_cache.get(youtube_id)
            if cached_file and os.path.exists(cached_file):
                logger.info(f"Using cached file for '{query}': {cached_file}")
                song_info = {
                    'title': data.get('title', 'Unknown Title'),
                    'url': cached_file,
                    'thumbnail': data.get('thumbnail'),
                    'duration': data.get('duration'),
                    'webpage_url': data.get('webpage_url', query),
                    'channel': data.get('channel', 'Unknown Channel'),
                    'youtube_id': youtube_id,
                    'start_time': None,
                    'is_cached': True,
                    'was_previously_cached': True
                }
                return song_info

            if download:
                if getattr(self, 'is_shutting_down', False):
                    logger.info("Skipping download because shutdown is in progress.")
                    return None

                logger.info(f"Downloading '{query}' to cache...")
                # Use ThreadPoolExecutor for downloads to avoid pickling issues
                downloaded_data = await loop.run_in_executor(
                    self.thread_executor,
                    run_yt_dlp_extractor,
                    query,
                    True
                )

                if getattr(self, 'is_shutting_down', False):
                    logger.info("Discarding downloaded info because shutdown is in progress.")
                    return None
                
                # Dynamic filename check
                # When downloading from a search, yt-dlp returns the search wrapper in 'entries'
                # Use the actual video entry for correct extractor/extension info.
                actual_video_data = downloaded_data
                if 'entries' in downloaded_data and downloaded_data['entries']:
                    actual_video_data = downloaded_data['entries'][0]
                
                ext = actual_video_data.get('ext', 'opus')
                
                # Get the extractor name from yt-dlp (e.g., 'youtube', 'soundcloud')
                # The outtmpl uses %(extractor)s-%(id)s.%(ext)s format
                # Use the actual video's extractor, not the search wrapper's extractor
                extractor_name = actual_video_data.get('extractor', 'youtube')
                
                # Note: yt-dlp might sanitize the filename, but our outtmpl is simple
                expected_filename_base = f"{extractor_name}-{youtube_id}"
                
                found_file = None
                potential_path = str(SONG_CACHE_DIR / f"{expected_filename_base}.{ext}")
                
                if os.path.exists(potential_path):
                    found_file = potential_path
                else:
                    for fname in os.listdir(SONG_CACHE_DIR):
                        if fname.startswith(expected_filename_base):
                            found_file = str(SONG_CACHE_DIR / fname)
                            break
                
                if found_file:
                    self.song_cache.add(youtube_id, found_file)
                    logger.info(f"Successfully downloaded and cached '{query}' at {found_file}")
                    
                    song_info = {
                        'title': data.get('title') or downloaded_data.get('title', 'Unknown Title'),
                        'url': found_file,
                        'thumbnail': data.get('thumbnail') or downloaded_data.get('thumbnail'),
                        'duration': data.get('duration') or downloaded_data.get('duration'),
                        'webpage_url': data.get('webpage_url') or downloaded_data.get('webpage_url', query),
                        'channel': data.get('channel') or downloaded_data.get('channel', 'Unknown Channel'),
                        'youtube_id': youtube_id,
                        'start_time': None,
                        'is_cached': True,
                        'was_previously_cached': False
                    }
                    return song_info
                else:
                    logger.error(f"Downloaded file not found. Expected base: {expected_filename_base}")
                    return None
            else:
                return {
                    'title': data.get('title', 'Unknown Title'),
                    'url': data.get('url'),
                    'thumbnail': data.get('thumbnail'),
                    'duration': data.get('duration'),
                    'webpage_url': data.get('webpage_url', query),
                    'channel': data.get('channel', 'Unknown Channel'),
                    'youtube_id': youtube_id,
                    'start_time': None,
                    'is_cached': False,
                    'was_previously_cached': False
                }

        except Exception as e:
            if "Can't pickle" in str(e):
                 logger.critical(f"Pickling error encountered despite fix attempt for '{query}': {e}", exc_info=True)
            elif isinstance(e, yt_dlp.utils.DownloadError):
                 logger.error(f"yt-dlp DownloadError extracting info for '{query}': {e}")
            elif isinstance(e, concurrent.futures.process.BrokenProcessPool):
                 logger.error(f"Process Pool Broken during info extraction for '{query}'. It might be shutting down or crashed: {e}")
            else:
                logger.exception(f"Unexpected error during info extraction process for '{query}': {e}")
            return None


    def _play_next(self, guild_id, error=None):
        """Callback function executed after a song finishes or errors.
        
        This is a sync callback from vc.play's 'after' parameter.
        It schedules the async _play_next_async coroutine to handle
        the actual playback setup (since FFmpegOpusAudio.from_probe is async).
        """
        if getattr(self, 'is_shutting_down', False):
            logger.info(f"Not starting next song in guild {guild_id}; shutdown is in progress.")
            return

        if error:
            logger.error(f'Player error in guild {guild_id}: {error}')

        was_skipped = self.current_song.get(guild_id, {}).get('was_skipped', False)

        if not was_skipped and self.current_song.get(guild_id):
            request_id = self.current_song[guild_id].get('request_id')
            if request_id:
                self.db_manager.update_play_status(request_id, 'completed')

        queue = self.get_queue(guild_id)
        if not queue:
            logger.info(f"Queue empty for guild {guild_id}.")
            self.current_song.pop(guild_id, None)
            self.last_activity[guild_id] = time.time()
            return

        future = asyncio.run_coroutine_threadsafe(
            self._play_next_async(guild_id), self.bot.loop
        )
        future.add_done_callback(
            lambda f: logger.error(f"Error in _play_next_async for guild {guild_id}: {f.exception()}") if f.exception() else None
        )

    async def _play_next_async(self, guild_id):
        """Async handler for playing the next song in the queue.
        
        Uses FFmpegOpusAudio.from_probe() which probes the file to detect
        its codec and can passthrough Opus audio directly without re-encoding.
        """
        if getattr(self, 'is_shutting_down', False):
            logger.info(f"Not starting next song in guild {guild_id}; shutdown is in progress.")
            return

        queue = self.get_queue(guild_id)
        if not queue:
            logger.info(f"Queue empty for guild {guild_id} (async check).")
            self.current_song.pop(guild_id, None)
            self.last_activity[guild_id] = time.time()
            return

        next_song_info = queue.popleft()
        next_song_info['start_time'] = time.time()
        self.current_song[guild_id] = next_song_info
        if 'request_id' in next_song_info:
            self.db_manager.update_play_start_timestamp(next_song_info['request_id'])
            self.db_manager.update_play_status(next_song_info['request_id'], 'playing')
        logger.info(f"Playing next song in guild {guild_id}: {next_song_info['title']}")
        logger.debug(f"Attempting to play next URL: {next_song_info['url']}")

        if guild_id not in self.voice_clients or not self.voice_clients[guild_id].is_connected():
            logger.warning(f"Voice client not available or disconnected in guild {guild_id} when trying to play next.")
            self.current_song.pop(guild_id, None)
            return

        vc = self.voice_clients[guild_id]
        try:
            if next_song_info.get('is_cached', False):
                local_ffmpeg_options = {
                    'executable': FFMPEG_EXECUTABLE
                }
            else:
                local_ffmpeg_options = FFMPEG_OPTIONS
            
            source = await discord.FFmpegOpusAudio.from_probe(
                next_song_info['url'], **local_ffmpeg_options
            )
            vc.play(source, after=lambda e: self._play_next(guild_id, error=e))
            self.last_activity[guild_id] = time.time()
        except discord.ClientException as e:
             logger.error(f"Discord ClientException while trying to play next in {guild_id}: {e}")
             self.current_song.pop(guild_id, None)
             await self._play_next_async(guild_id)
        except Exception as e:
            logger.exception(f"Unexpected error during playback setup in {guild_id}: {e}")
            self.current_song.pop(guild_id, None)
            await self._play_next_async(guild_id)
