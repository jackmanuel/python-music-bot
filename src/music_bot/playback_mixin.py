import asyncio
import concurrent.futures
import functools
import logging
import os
import time

import discord
import yt_dlp

from config import FFMPEG_EXECUTABLE, LOUDNORM_ENABLED, MAX_SONG_DURATION_SECONDS, SONG_CACHE_DIR
from youtube import (
    FFMPEG_OPTIONS,
    YtDlpPipeOpusAudio,
    is_age_restricted_yt_dlp_error,
    is_livestream_info,
    run_yt_dlp_extractor,
    run_yt_dlp_search,
    run_yt_dlp_search_results,
)
from youtube_urls import (
    canonical_youtube_url,
    default_youtube_thumbnail_url,
    extract_youtube_video_id,
    normalize_search_query,
    youtube_id_from_cache_path,
)

logger = logging.getLogger(__name__)


class PlaybackMixin:
    @staticmethod
    def _song_references_cache_entry(song_info, youtube_id, file_path):
        if not song_info or not song_info.get('is_cached', False):
            return False
        if youtube_id and song_info.get('youtube_id') == youtube_id:
            return True
        if not file_path:
            return False
        return os.path.normcase(os.path.abspath(song_info.get('url', ''))) == os.path.normcase(
            os.path.abspath(file_path)
        )

    def _cache_entry_is_referenced(self, youtube_id, file_path):
        for song_info in getattr(self, 'current_song', {}).values():
            if self._song_references_cache_entry(song_info, youtube_id, file_path):
                return True

        for queue in getattr(self, 'queues', {}).values():
            for song_info in queue:
                if self._song_references_cache_entry(song_info, youtube_id, file_path):
                    return True
        return False

    def _process_pending_cache_evictions(self):
        """Delete rejected new downloads once no playback entry references them."""
        pending = getattr(self, 'pending_cache_evictions', {})
        db_manager = getattr(self, 'db_manager', None)
        results = {}
        for youtube_id, eviction in list(pending.items()):
            file_path = eviction['file_path']
            if self._cache_entry_is_referenced(youtube_id, file_path):
                results[youtube_id] = 'deferred'
                continue

            if not self.song_cache.get(youtube_id):
                pending.pop(youtube_id, None)
                if db_manager and hasattr(db_manager, 'delete_cached_song'):
                    db_manager.delete_cached_song(youtube_id)
                results[youtube_id] = 'deleted'
                continue

            if self.song_cache.remove(youtube_id, expected_file_path=file_path):
                pending.pop(youtube_id, None)
                if db_manager and hasattr(db_manager, 'delete_cached_song'):
                    db_manager.delete_cached_song(youtube_id)
                results[youtube_id] = 'deleted'
                logger.info(
                    "Deleted rejected new cache download for '%s' (%s).",
                    eviction['title'],
                    youtube_id,
                )
            else:
                results[youtube_id] = 'failed'
        return results

    @staticmethod
    def _is_valid_cached_file(file_path):
        if not file_path:
            return False
        try:
            return os.path.exists(file_path) and os.path.getsize(file_path) > 0
        except OSError:
            return False

    def _cleanup_stale_cache_entry(self, youtube_id):
        song_cache = getattr(self, 'song_cache', None)
        db_manager = getattr(self, 'db_manager', None)
        if song_cache and song_cache.get(youtube_id):
            song_cache.remove(youtube_id)
        if db_manager and hasattr(db_manager, 'delete_cached_song'):
            db_manager.delete_cached_song(youtube_id)

    def _upsert_cached_song_metadata(self, file_path, title, channel_name, duration, thumbnail_url):
        db_manager = getattr(self, 'db_manager', None)
        if not db_manager or not hasattr(db_manager, 'upsert_cached_song'):
            return
        cached_youtube_id = youtube_id_from_cache_path(file_path)
        if not cached_youtube_id:
            return
        if not title or duration is None:
            return
        db_manager.upsert_cached_song(
            youtube_id=cached_youtube_id,
            title=title,
            resolved_url=canonical_youtube_url(cached_youtube_id),
            channel_name=channel_name,
            duration=duration,
            thumbnail_url=thumbnail_url,
        )

    def _record_completed_query_cache(self, song_info):
        """Record normalized search query -> YouTube ID mapping for completed cached tracks."""
        if not song_info:
            return
        youtube_id = song_info.get('youtube_id')
        is_cached = song_info.get('is_cached', False)
        if not is_cached:
            if not song_info.get('created_cache_entry', False):
                return
            song_cache = getattr(self, 'song_cache', None)
            cached_file = song_cache.get(youtube_id) if (song_cache is not None and youtube_id) else None
            if not self._is_valid_cached_file(cached_file):
                return
        search_query = song_info.get('search_query_normalized') or song_info.get('source_query_normalized')
        if not youtube_id or not search_query:
            return
        db_manager = getattr(self, 'db_manager', None)
        if db_manager and hasattr(db_manager, 'upsert_cached_query'):
            if song_info.get('cache_query_only_if_missing', False):
                db_manager.upsert_cached_query(search_query, youtube_id, overwrite=False)
            else:
                db_manager.upsert_cached_query(search_query, youtube_id)

    def _try_cache_first_extract(self, query, normalized_query):
        """Return song_info directly from disk + SQLite cache without calling yt-dlp, or None."""
        song_cache = getattr(self, 'song_cache', None)
        db_manager = getattr(self, 'db_manager', None)
        if not song_cache or not db_manager:
            return None

        candidate_youtube_id = extract_youtube_video_id(query)
        matched_cached_query = None
        if not candidate_youtube_id and normalized_query and hasattr(db_manager, 'get_cached_query'):
            candidate_youtube_id = db_manager.get_cached_query(normalized_query)
            if candidate_youtube_id:
                matched_cached_query = normalized_query

        if not candidate_youtube_id:
            return None

        cached_file = song_cache.get(candidate_youtube_id)
        if not self._is_valid_cached_file(cached_file):
            if cached_file or matched_cached_query:
                logger.info(
                    "Cleaning up stale cache entry for YouTube ID %s (missing or empty file on disk).",
                    candidate_youtube_id,
                )
                self._cleanup_stale_cache_entry(candidate_youtube_id)
            return None

        if not hasattr(db_manager, 'get_cached_song'):
            return None

        cached_song = db_manager.get_cached_song(candidate_youtube_id)
        if not cached_song or not cached_song.get('title') or cached_song.get('duration') is None:
            return None

        duration = cached_song['duration']
        logger.info(
            "Cache-first fast-path hit for %r (youtube_id=%s, query_hit=%s): %s",
            query,
            candidate_youtube_id,
            bool(matched_cached_query),
            cached_file,
        )
        return {
            'title': cached_song['title'],
            'url': cached_file,
            'thumbnail': cached_song.get('thumbnail_url') or default_youtube_thumbnail_url(candidate_youtube_id),
            'duration': duration,
            'webpage_url': cached_song.get('resolved_url') or canonical_youtube_url(candidate_youtube_id),
            'channel': cached_song.get('channel_name') or 'Unknown Channel',
            'youtube_id': candidate_youtube_id,
            'start_time': None,
            'is_cached': True,
            'was_previously_cached': True,
            'created_cache_entry': False,
            'exceeds_cache_duration': bool(duration and duration > MAX_SONG_DURATION_SECONDS),
            'estimated_size_bytes': 0,
            'matched_cached_query': matched_cached_query,
            'search_query_normalized': normalized_query,
            'source_query_normalized': normalized_query,
        }

    def _request_cache_eviction(self, song_info):
        """Request eviction when this playback request created or scheduled a new cache entry."""
        youtube_id = song_info.get('youtube_id') if song_info else None
        bg_tasks = getattr(self, 'background_cache_tasks', None)
        is_bg_pending = bool(
            song_info
            and youtube_id
            and (
                song_info.get('background_caching', False)
                or (bg_tasks and youtube_id in bg_tasks)
            )
        )
        if is_bg_pending:
            cancelled = getattr(self, 'cancelled_background_caches', None)
            if cancelled is None:
                cancelled = set()
                self.cancelled_background_caches = cancelled
            cancelled.add(youtube_id)
            if not song_info.get('created_cache_entry', False):
                logger.info(
                    "Marked in-flight background cache for '%s' (%s) for cancellation.",
                    song_info.get('title', 'Unknown Title'),
                    youtube_id,
                )
                return 'cancelled'

        if not song_info or not song_info.get('created_cache_entry', False):
            return None

        file_path = song_info.get('cached_file_path') or song_info.get('url')
        if not youtube_id or not file_path:
            logger.warning("Cannot evict a new cache entry without its YouTube ID and file path.")
            return 'failed'

        pending = getattr(self, 'pending_cache_evictions', None)
        if pending is None:
            pending = {}
            self.pending_cache_evictions = pending
        pending[youtube_id] = {
            'file_path': file_path,
            'title': song_info.get('title', 'Unknown Title'),
        }
        return self._process_pending_cache_evictions().get(youtube_id, 'deferred')

    @staticmethod
    def _find_downloaded_cache_file(youtube_id, actual_video_data):
        """Locate the downloaded audio file in SONG_CACHE_DIR for a yt-dlp result."""
        ext = actual_video_data.get('ext', 'opus')
        extractor_name = actual_video_data.get('extractor', 'youtube')
        expected_filename_base = f"{extractor_name}-{youtube_id}"

        potential_path = str(SONG_CACHE_DIR / f"{expected_filename_base}.{ext}")
        if os.path.exists(potential_path):
            return potential_path, expected_filename_base

        try:
            for fname in os.listdir(SONG_CACHE_DIR):
                if fname.startswith(expected_filename_base) and not fname.startswith('.'):
                    return str(SONG_CACHE_DIR / fname), expected_filename_base
        except OSError:
            pass
        return None, expected_filename_base

    def _schedule_background_cache(self, song_info, download_query):
        """Schedule a low-priority background download + normalization into song_cache."""
        if getattr(self, 'is_shutting_down', False):
            return None

        youtube_id = song_info.get('youtube_id')
        song_cache = getattr(self, 'song_cache', None)
        if not youtube_id or song_cache is None:
            return None

        if self._is_valid_cached_file(song_cache.get(youtube_id)):
            return None

        cancelled = getattr(self, 'cancelled_background_caches', None)
        if cancelled is not None:
            cancelled.discard(youtube_id)

        tasks = getattr(self, 'background_cache_tasks', None)
        if tasks is None:
            tasks = {}
            self.background_cache_tasks = tasks

        existing_task = tasks.get(youtube_id)
        if existing_task and not existing_task.done():
            song_info['background_caching'] = True
            return existing_task

        song_info['background_caching'] = True
        task = asyncio.create_task(self._run_background_cache(song_info, download_query))
        tasks[youtube_id] = task
        return task

    async def _run_background_cache(self, song_info, download_query):
        """Download and normalize an uncached track in the background at low CPU priority."""
        youtube_id = song_info.get('youtube_id')
        title = song_info.get('title', 'Unknown Title')
        estimated_size_bytes = song_info.get('estimated_size_bytes', 0)

        try:
            if getattr(self, 'is_shutting_down', False):
                return

            cancelled = getattr(self, 'cancelled_background_caches', set())
            if youtube_id in cancelled:
                logger.info("Skipping cancelled background cache for '%s' (%s).", title, youtube_id)
                return

            song_cache = getattr(self, 'song_cache', None)
            if song_cache is None or self._is_valid_cached_file(song_cache.get(youtube_id)):
                return
            if not song_cache.can_accept_download(estimated_size_bytes):
                logger.info(
                    "Skipping background cache for '%s' (%s); cache size limit reached.",
                    title,
                    youtube_id,
                )
                return

            executor = getattr(self, 'background_cache_executor', None) or getattr(self, 'thread_executor', None)
            loop = asyncio.get_running_loop()
            logger.info("Starting low-priority background cache for '%s' (%s)...", title, youtube_id)
            downloaded_data = await loop.run_in_executor(
                executor,
                functools.partial(
                    run_yt_dlp_extractor,
                    download_query,
                    True,
                    low_priority=True,
                    threads=1,
                ),
            )

            if not downloaded_data or (isinstance(downloaded_data, dict) and downloaded_data.get('error')):
                logger.warning("Background cache download did not succeed for '%s' (%s).", title, youtube_id)
                return

            actual_video_data = downloaded_data
            if isinstance(downloaded_data, dict) and downloaded_data.get('entries'):
                actual_video_data = downloaded_data['entries'][0]

            found_file, expected_base = self._find_downloaded_cache_file(youtube_id, actual_video_data)
            if not found_file:
                logger.error(
                    "Background cached file not found for '%s' (%s). Expected base: %s",
                    title,
                    youtube_id,
                    expected_base,
                )
                return

            cancelled = getattr(self, 'cancelled_background_caches', set())
            if getattr(self, 'is_shutting_down', False) or youtube_id in cancelled:
                try:
                    os.remove(found_file)
                except OSError as removal_error:
                    logger.warning(
                        "Failed to remove cancelled background cache file %s: %s",
                        found_file,
                        removal_error,
                    )
                if isinstance(cancelled, set):
                    cancelled.discard(youtube_id)
                logger.info("Discarded cancelled background cache for '%s' (%s).", title, youtube_id)
                return

            if not song_cache.add(youtube_id, found_file):
                try:
                    os.remove(found_file)
                except OSError as removal_error:
                    logger.error(
                        "Failed to remove over-limit background cache file %s: %s",
                        found_file,
                        removal_error,
                    )
                song_cache.refresh_capacity()
                return

            resolved_title = song_info.get('title') or actual_video_data.get('title') or downloaded_data.get('title')
            resolved_thumbnail = song_info.get('thumbnail') or actual_video_data.get('thumbnail') or downloaded_data.get('thumbnail')
            resolved_duration = (
                song_info.get('duration')
                if song_info.get('duration') is not None
                else actual_video_data.get('duration')
            )
            resolved_channel = song_info.get('channel') or actual_video_data.get('channel') or downloaded_data.get('channel')
            self._upsert_cached_song_metadata(
                file_path=found_file,
                title=resolved_title,
                channel_name=resolved_channel,
                duration=resolved_duration,
                thumbnail_url=resolved_thumbnail,
            )

            song_info['created_cache_entry'] = True
            song_info['cached_file_path'] = found_file
            song_info['background_caching'] = False

            if song_info.get('completed_playback', False):
                self._record_completed_query_cache(song_info)

            logger.info("Background cached and ready: '%s' (%s) at %s", resolved_title or title, youtube_id, found_file)
        except asyncio.CancelledError:
            logger.info("Background cache task cancelled for '%s' (%s).", title, youtube_id)
            raise
        except Exception as exc:
            logger.warning("Background cache failed for '%s' (%s): %s", title, youtube_id, exc)
        finally:
            song_info['background_caching'] = False
            tasks = getattr(self, 'background_cache_tasks', None)
            if tasks and tasks.get(youtube_id) is asyncio.current_task():
                tasks.pop(youtube_id, None)

    def _upgrade_queued_song_to_cache(self, song_info):
        """Switch a queued stream entry to its cached .opus file if background caching finished."""
        if not song_info or song_info.get('is_cached', False):
            return False
        youtube_id = song_info.get('youtube_id')
        song_cache = getattr(self, 'song_cache', None)
        if not youtube_id or song_cache is None:
            return False
        cached_file = song_cache.get(youtube_id)
        if not self._is_valid_cached_file(cached_file):
            return False

        song_info['url'] = cached_file
        song_info['cached_file_path'] = cached_file
        song_info['is_cached'] = True
        logger.info(
            "Upgraded queued song '%s' (%s) to newly cached file: %s",
            song_info.get('title', 'Unknown Title'),
            youtube_id,
            cached_file,
        )
        return True

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
        """Extracts info using local cache first, falling back to yt-dlp in an executor."""
        if getattr(self, 'is_shutting_down', False):
            logger.info("Skipping info extraction because shutdown is in progress.")
            return None

        normalized_query = normalize_search_query(query)
        fast_path_info = self._try_cache_first_extract(query, normalized_query)
        if fast_path_info is not None:
            return fast_path_info

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

            if isinstance(data, dict) and data.get('error') == 'age_restricted':
                logger.warning(f"Cannot play age-restricted video for query '{query}'.")
                return data

            if isinstance(data, dict) and 'entries' in data:
                entries = data.get('entries') or []
                if not entries:
                    logger.warning(
                        f"yt-dlp returned no playable entries for '{query}'. "
                        "The search may have no results or its results may be unavailable."
                    )
                    return None

                logger.info(
                    f"yt-dlp returned {len(entries)} entries for '{query}', using the first result."
                )
                data = entries[0]

            if is_livestream_info(data):
                logger.warning(f"Refusing livestream '{data.get('title', 'Unknown')}' for query '{query}'.")
                return {
                    'error': 'livestream',
                    'title': data.get('title', 'Unknown Title'),
                    'webpage_url': data.get('webpage_url', query)
                }

            youtube_id = data.get('id')
            if not youtube_id:
                logger.warning(f"Could not extract YouTube ID for '{query}'.")
                return None

            duration = data.get('duration')
            exceeds_cache_duration = bool(duration and duration > MAX_SONG_DURATION_SECONDS)
            estimated_size_bytes = data.get('filesize') or data.get('filesize_approx') or 0

            cached_file = self.song_cache.get(youtube_id) if getattr(self, 'song_cache', None) else None
            if cached_file and not self._is_valid_cached_file(cached_file):
                logger.info(
                    "Cleaning up stale cache entry for YouTube ID %s after yt-dlp lookup (missing or empty file).",
                    youtube_id,
                )
                self._cleanup_stale_cache_entry(youtube_id)
                cached_file = None

            if cached_file:
                logger.info(f"Using cached file for '{query}': {cached_file}")
                title = data.get('title', 'Unknown Title')
                channel = data.get('channel', 'Unknown Channel')
                thumbnail = data.get('thumbnail')
                self._upsert_cached_song_metadata(
                    file_path=cached_file,
                    title=data.get('title'),
                    channel_name=data.get('channel'),
                    duration=duration,
                    thumbnail_url=thumbnail,
                )
                song_info = {
                    'title': title,
                    'url': cached_file,
                    'thumbnail': thumbnail,
                    'duration': duration,
                    'webpage_url': data.get('webpage_url', query),
                    'channel': channel,
                    'youtube_id': youtube_id,
                    'start_time': None,
                    'is_cached': True,
                    'was_previously_cached': True,
                    'created_cache_entry': False,
                    'exceeds_cache_duration': exceeds_cache_duration,
                    'estimated_size_bytes': estimated_size_bytes,
                    'matched_cached_query': None,
                    'search_query_normalized': normalized_query,
                    'source_query_normalized': normalized_query,
                }
                return song_info

            if download:
                if exceeds_cache_duration:
                    logger.warning(
                        f"Download requested for over-limit song '{query}' "
                        f"({duration}s). Returning stream info instead of caching because "
                        f"it exceeds the cache download limit ({MAX_SONG_DURATION_SECONDS}s)."
                    )
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
                        'was_previously_cached': False,
                        'created_cache_entry': False,
                        'exceeds_cache_duration': True,
                        'estimated_size_bytes': estimated_size_bytes,
                    }

                if not self.song_cache.can_accept_download(estimated_size_bytes):
                    logger.info(
                        f"Streaming '{query}' instead of caching because the cache size limit "
                        "has been reached."
                    )
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
                        'was_previously_cached': False,
                        'created_cache_entry': False,
                        'exceeds_cache_duration': False,
                        'estimated_size_bytes': estimated_size_bytes,
                    }

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

                if isinstance(downloaded_data, dict) and downloaded_data.get('error') == 'age_restricted':
                    logger.warning(f"Cannot download age-restricted video for query '{query}'.")
                    return downloaded_data

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
                    if not self.song_cache.add(youtube_id, found_file):
                        try:
                            os.remove(found_file)
                        except OSError as removal_error:
                            logger.error(
                                f"Failed to remove over-limit cache file {found_file}: {removal_error}"
                            )
                        self.song_cache.refresh_capacity()
                        return {
                            'title': data.get('title') or downloaded_data.get('title', 'Unknown Title'),
                            'url': data.get('url'),
                            'thumbnail': data.get('thumbnail') or downloaded_data.get('thumbnail'),
                            'duration': data.get('duration') or downloaded_data.get('duration'),
                            'webpage_url': data.get('webpage_url') or downloaded_data.get('webpage_url', query),
                            'channel': data.get('channel') or downloaded_data.get('channel', 'Unknown Channel'),
                            'youtube_id': youtube_id,
                            'start_time': None,
                            'is_cached': False,
                            'was_previously_cached': False,
                            'created_cache_entry': False,
                            'exceeds_cache_duration': False,
                            'estimated_size_bytes': estimated_size_bytes,
                        }
                    logger.info(f"Successfully downloaded and cached '{query}' at {found_file}")

                    resolved_title = data.get('title') or actual_video_data.get('title') or downloaded_data.get('title')
                    resolved_thumbnail = data.get('thumbnail') or actual_video_data.get('thumbnail') or downloaded_data.get('thumbnail')
                    resolved_duration = data.get('duration') if data.get('duration') is not None else (
                        actual_video_data.get('duration') if actual_video_data.get('duration') is not None else downloaded_data.get('duration')
                    )
                    resolved_channel = data.get('channel') or actual_video_data.get('channel') or downloaded_data.get('channel')
                    self._upsert_cached_song_metadata(
                        file_path=found_file,
                        title=resolved_title,
                        channel_name=resolved_channel,
                        duration=resolved_duration,
                        thumbnail_url=resolved_thumbnail,
                    )

                    song_info = {
                        'title': resolved_title or 'Unknown Title',
                        'url': found_file,
                        'thumbnail': resolved_thumbnail,
                        'duration': resolved_duration,
                        'webpage_url': data.get('webpage_url') or actual_video_data.get('webpage_url') or downloaded_data.get('webpage_url', query),
                        'channel': resolved_channel or 'Unknown Channel',
                        'youtube_id': youtube_id,
                        'start_time': None,
                        'is_cached': True,
                        'was_previously_cached': False,
                        'created_cache_entry': True,
                        'exceeds_cache_duration': False,
                        'estimated_size_bytes': estimated_size_bytes,
                        'matched_cached_query': None,
                        'search_query_normalized': normalized_query,
                        'source_query_normalized': normalized_query,
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
                    'was_previously_cached': False,
                    'created_cache_entry': False,
                    'exceeds_cache_duration': exceeds_cache_duration,
                    'estimated_size_bytes': estimated_size_bytes,
                    'matched_cached_query': None,
                    'search_query_normalized': normalized_query,
                    'source_query_normalized': normalized_query,
                }

        except Exception as e:
            if "Can't pickle" in str(e):
                logger.critical(f"Pickling error encountered despite fix attempt for '{query}': {e}", exc_info=True)
            elif isinstance(e, yt_dlp.utils.DownloadError):
                logger.error(f"yt-dlp DownloadError extracting info for '{query}': {e}")
                if is_age_restricted_yt_dlp_error(e):
                    return {'error': 'age_restricted'}
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
            finished_song = self.current_song[guild_id]
            finished_song['completed_playback'] = True
            request_id = finished_song.get('request_id')
            if request_id:
                self.db_manager.update_play_status(request_id, 'completed')
            self._record_completed_query_cache(finished_song)

        queue = self.get_queue(guild_id)
        if not queue:
            logger.info(f"Queue empty for guild {guild_id}.")
            self.current_song.pop(guild_id, None)
            self._process_pending_cache_evictions()
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
            self._process_pending_cache_evictions()
            self.last_activity[guild_id] = time.time()
            return

        next_song_info = queue.popleft()
        self._upgrade_queued_song_to_cache(next_song_info)
        next_song_info['start_time'] = None
        next_song_info['is_buffering'] = True
        self.current_song[guild_id] = next_song_info
        self._process_pending_cache_evictions()
        logger.debug(f"Attempting to play next URL: {next_song_info['url']}")

        if guild_id not in self.voice_clients or not self.voice_clients[guild_id].is_connected():
            logger.warning(f"Voice client not available or disconnected in guild {guild_id} when trying to play next.")
            self.current_song.pop(guild_id, None)
            self._process_pending_cache_evictions()
            return

        vc = self.voice_clients[guild_id]
        source = None
        try:
            if next_song_info.get('is_cached', False):
                source = await discord.FFmpegOpusAudio.from_probe(
                    next_song_info['url'], executable=FFMPEG_EXECUTABLE
                )
            elif next_song_info.get('webpage_url'):
                source = YtDlpPipeOpusAudio(
                    next_song_info['webpage_url'],
                    executable=FFMPEG_EXECUTABLE,
                    options=FFMPEG_OPTIONS['options'],
                )
                if hasattr(source, 'wait_until_ready'):
                    ready = await source.wait_until_ready()
                    if not ready:
                        if hasattr(source, 'cleanup'):
                            source.cleanup()
                        logger.error(
                            f"Stream failed to produce initial audio packet in guild {guild_id}: {next_song_info['title']}"
                        )
                        self.current_song.pop(guild_id, None)
                        self._process_pending_cache_evictions()
                        await self._play_next_async(guild_id)
                        return
            elif LOUDNORM_ENABLED:
                source = discord.FFmpegOpusAudio(
                    next_song_info['url'], **FFMPEG_OPTIONS
                )
            else:
                source = await discord.FFmpegOpusAudio.from_probe(
                    next_song_info['url'], **FFMPEG_OPTIONS
                )

            if getattr(self, 'is_shutting_down', False) or not vc.is_connected():
                if source is not None and hasattr(source, 'cleanup'):
                    source.cleanup()
                self.current_song.pop(guild_id, None)
                self._process_pending_cache_evictions()
                return

            next_song_info['start_time'] = time.time()
            next_song_info.pop('is_buffering', None)
            if 'request_id' in next_song_info:
                self.db_manager.update_play_start_timestamp(next_song_info['request_id'])
                self.db_manager.update_play_status(next_song_info['request_id'], 'playing')
            logger.info(f"Playing next song in guild {guild_id}: {next_song_info['title']}")
            vc.play(source, after=lambda e: self._play_next(guild_id, error=e))
            self.last_activity[guild_id] = time.time()
        except discord.ClientException as e:
             if source is not None and hasattr(source, 'cleanup'):
                 source.cleanup()
             logger.error(f"Discord ClientException while trying to play next in {guild_id}: {e}")
             self.current_song.pop(guild_id, None)
             self._process_pending_cache_evictions()
             await self._play_next_async(guild_id)
        except Exception as e:
            if source is not None and hasattr(source, 'cleanup'):
                source.cleanup()
            logger.exception(f"Unexpected error during playback setup in {guild_id}: {e}")
            self.current_song.pop(guild_id, None)
            self._process_pending_cache_evictions()
            await self._play_next_async(guild_id)
