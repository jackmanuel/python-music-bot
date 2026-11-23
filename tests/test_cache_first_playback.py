from collections import deque
import concurrent.futures
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src" / "music_bot"
sys.path.insert(0, str(SRC_DIR))

from cache_commands_mixin import CacheCommandsMixin
from database_manager import DatabaseManager
from music_cog import MusicCog
from playback_mixin import PlaybackMixin
from queue_commands_mixin import QueueCommandsMixin
from song_cache import SongCache
from voice_commands_mixin import VoiceCommandsMixin
from youtube_urls import (
    canonical_youtube_url,
    default_youtube_thumbnail_url,
    normalize_search_query,
)


class CachedTablesAndReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "music_log.db"
        self.manager = DatabaseManager(str(self.db_path))

    def tearDown(self):
        self.temp_dir.cleanup()

    def _insert_play_history(
        self,
        *,
        query,
        resolved_title,
        resolved_url,
        channel_name="Test Channel",
        duration=180,
        play_status="completed",
        timestamp="2026-01-01T00:00:00+00:00",
        user_id=1,
        guild_id=10,
    ):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO play_history (
                    request_timestamp, user_id, user_name, guild_id,
                    query, resolved_title, resolved_url, channel_name, duration, play_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    str(user_id),
                    f"User {user_id}",
                    str(guild_id),
                    query,
                    resolved_title,
                    resolved_url,
                    channel_name,
                    duration,
                    play_status,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_tables_created_and_foreign_key_cascade_works(self):
        conn = self.manager._get_db_connection()
        try:
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("cached_songs", tables)
            self.assertIn("cached_queries", tables)
            fk_enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            self.assertEqual(fk_enabled, 1)

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO cached_queries (normalized_query, youtube_id) VALUES (?, ?)",
                    ("orphan query", "dQw4w9WgXcQ"),
                )
        finally:
            conn.close()

        self.assertTrue(
            self.manager.upsert_cached_song(
                youtube_id="dQw4w9WgXcQ",
                title="Never Gonna Give You Up",
                channel_name="Rick Astley",
                duration=213,
                thumbnail_url=None,
            )
        )
        self.assertTrue(
            self.manager.upsert_cached_query("rick roll", "dQw4w9WgXcQ")
        )
        self.assertEqual(self.manager.get_cached_query("rick roll"), "dQw4w9WgXcQ")

        # Fallback thumbnail on read when NULL in DB.
        cached = self.manager.get_cached_song("dQw4w9WgXcQ")
        self.assertEqual(cached["resolved_url"], canonical_youtube_url("dQw4w9WgXcQ"))
        self.assertEqual(cached["thumbnail_url"], default_youtube_thumbnail_url("dQw4w9WgXcQ"))

        # Upserting cached_songs must update in place without cascading cached_queries.
        self.assertTrue(
            self.manager.upsert_cached_song(
                youtube_id="dQw4w9WgXcQ",
                title="Never Gonna Give You Up (4K)",
                channel_name="Rick Astley",
                duration=213,
                thumbnail_url="https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
            )
        )
        self.assertEqual(self.manager.get_cached_query("rick roll"), "dQw4w9WgXcQ")
        self.assertEqual(
            self.manager.get_cached_song("dQw4w9WgXcQ")["thumbnail_url"],
            "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
        )

        # Deleting from cached_songs cascades to cached_queries.
        self.assertTrue(self.manager.delete_cached_song("dQw4w9WgXcQ"))
        self.assertIsNone(self.manager.get_cached_song("dQw4w9WgXcQ"))
        self.assertIsNone(self.manager.get_cached_query("rick roll"))

    def test_startup_reconcile_prunes_stale_and_backfills_songs_and_queries(self):
        # Stale song + query in DB that is no longer on disk.
        self.manager.upsert_cached_song("staleVideo1", "Stale Song", duration=100)
        self.manager.upsert_cached_query("stale query", "staleVideo1")

        # Play history for cached song A across multiple plays & guilds.
        self._insert_play_history(
            query="  Shared   Query  ",
            resolved_title="Song A Old Title",
            resolved_url="https://youtu.be/videoAAAA11",
            channel_name="Channel A Old",
            duration=200,
            play_status="completed",
            timestamp="2026-01-01T10:00:00+00:00",
            guild_id=10,
        )
        self._insert_play_history(
            query="https://www.youtube.com/watch?v=videoAAAA11",
            resolved_title="Song A New Title",
            resolved_url="https://www.youtube.com/watch?v=videoAAAA11",
            channel_name="Channel A New",
            duration=201,
            play_status="completed",
            timestamp="2026-01-02T10:00:00+00:00",
            guild_id=20,
        )
        # Skipped query should NOT be backfilled.
        self._insert_play_history(
            query="skipped query",
            resolved_title="Song A New Title",
            resolved_url="https://www.youtube.com/watch?v=videoAAAA11",
            channel_name="Channel A New",
            duration=201,
            play_status="skipped",
            timestamp="2026-01-03T10:00:00+00:00",
        )
        # ytsearch prefix query should NOT be backfilled.
        self._insert_play_history(
            query="ytsearch1:prefix query",
            resolved_title="Song A New Title",
            resolved_url="https://www.youtube.com/watch?v=videoAAAA11",
            channel_name="Channel A New",
            duration=201,
            play_status="completed",
            timestamp="2026-01-03T11:00:00+00:00",
        )
        # Song B overrides "shared query" with a newer completed play.
        self._insert_play_history(
            query="shared query",
            resolved_title="Song B",
            resolved_url="https://www.youtube.com/watch?v=videoBBBB22",
            channel_name="Channel B",
            duration=150,
            play_status="completed",
            timestamp="2026-01-04T10:00:00+00:00",
        )
        # Song C has no duration in play_history -> skipped during backfill.
        self._insert_play_history(
            query="no duration song",
            resolved_title="No Duration",
            resolved_url="https://www.youtube.com/watch?v=videoCCCC33",
            duration=None,
            play_status="completed",
            timestamp="2026-01-04T12:00:00+00:00",
        )
        # Uncached song D in play_history -> must NOT appear in cached_songs or cached_queries.
        self._insert_play_history(
            query="uncached query",
            resolved_title="Uncached Song",
            resolved_url="https://www.youtube.com/watch?v=videoDDDD44",
            duration=180,
            play_status="completed",
            timestamp="2026-01-05T10:00:00+00:00",
        )

        # Disk has videoAAAA11, videoBBBB22, videoCCCC33, and videoEEEE55 (no history).
        summary = self.manager.reconcile_cache(
            {"videoAAAA11", "videoBBBB22", "videoCCCC33", "videoEEEE55"}
        )

        self.assertEqual(summary["pruned_songs"], 1)
        self.assertEqual(summary["backfilled_songs"], 2)
        self.assertEqual(summary["backfilled_queries"], 1)

        # Stale row + its query were pruned.
        self.assertIsNone(self.manager.get_cached_song("staleVideo1"))
        self.assertIsNone(self.manager.get_cached_query("stale query"))

        # Song A backfilled with most recent metadata across guilds.
        song_a = self.manager.get_cached_song("videoAAAA11")
        self.assertIsNotNone(song_a)
        self.assertEqual(song_a["title"], "Song A New Title")
        self.assertEqual(song_a["channel_name"], "Channel A New")
        self.assertEqual(song_a["duration"], 201)
        self.assertEqual(song_a["resolved_url"], canonical_youtube_url("videoAAAA11"))

        # Song B backfilled, and "shared query" maps to latest completed play (Song B).
        self.assertIsNotNone(self.manager.get_cached_song("videoBBBB22"))
        self.assertEqual(self.manager.get_cached_query("shared query"), "videoBBBB22")

        # Unusable / uncached / non-completed / URL / prefix queries excluded.
        self.assertIsNone(self.manager.get_cached_song("videoCCCC33"))
        self.assertIsNone(self.manager.get_cached_song("videoDDDD44"))
        self.assertIsNone(self.manager.get_cached_song("videoEEEE55"))
        self.assertIsNone(self.manager.get_cached_query("skipped query"))
        self.assertIsNone(self.manager.get_cached_query("ytsearch1:prefix query"))
        self.assertIsNone(self.manager.get_cached_query("uncached query"))
        self.assertIsNone(
            self.manager.get_cached_query("https://www.youtube.com/watch?v=videoAAAA11")
        )

    def test_song_cache_youtube_ids_on_disk_ignores_non_youtube_and_missing_files(self):
        cache_dir = Path(self.temp_dir.name) / "song_cache"
        cache_dir.mkdir()
        yt_file = cache_dir / "youtube-dQw4w9WgXcQ.opus"
        yt_file.write_bytes(b"audio")
        missing_yt_file = cache_dir / "youtube-missing1234.opus"
        missing_yt_file.write_bytes(b"audio")
        sc_file = cache_dir / "soundcloud-123456.opus"
        sc_file.write_bytes(b"audio")

        cache = SongCache(cache_dir)
        missing_yt_file.unlink()

        self.assertEqual(cache.youtube_ids_on_disk(), {"dQw4w9WgXcQ"})


class CacheFirstPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.cache_dir = self.root / "song_cache"
        self.cache_dir.mkdir()
        self.db_path = self.root / "music_log.db"

        self.mixin = PlaybackMixin()
        self.mixin.song_cache = SongCache(self.cache_dir)
        self.mixin.db_manager = DatabaseManager(str(self.db_path))
        self.mixin.current_song = {}
        self.mixin.queues = {}
        self.mixin.pending_cache_evictions = {}
        self.mixin.last_activity = {}
        self.mixin.get_queue = lambda guild_id: self.mixin.queues.setdefault(guild_id, deque())
        self.mixin.process_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.mixin.thread_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    async def asyncTearDown(self):
        self.mixin.process_executor.shutdown(wait=True)
        self.mixin.thread_executor.shutdown(wait=True)
        self.temp_dir.cleanup()

    def _create_cached_song(
        self,
        youtube_id="dQw4w9WgXcQ",
        title="Never Gonna Give You Up",
        channel="Rick Astley",
        duration=213,
        thumbnail=None,
        content=b"opus-audio-bytes",
    ):
        file_path = self.cache_dir / f"youtube-{youtube_id}.opus"
        file_path.write_bytes(content)
        self.mixin.song_cache.add(youtube_id, str(file_path))
        self.mixin.db_manager.upsert_cached_song(
            youtube_id=youtube_id,
            title=title,
            channel_name=channel,
            duration=duration,
            thumbnail_url=thumbnail,
        )
        return file_path

    async def test_direct_youtube_urls_bypass_yt_dlp(self):
        cached_path = self._create_cached_song(youtube_id="dQw4w9WgXcQ")

        urls = [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?t=42",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        ]

        with patch("playback_mixin.run_yt_dlp_search") as mock_search:
            for url in urls:
                info = await self.mixin._extract_info(url, download=False)
                self.assertIsNotNone(info)
                self.assertEqual(info["title"], "Never Gonna Give You Up")
                self.assertEqual(info["url"], str(cached_path))
                self.assertEqual(info["youtube_id"], "dQw4w9WgXcQ")
                self.assertEqual(info["webpage_url"], canonical_youtube_url("dQw4w9WgXcQ"))
                self.assertEqual(info["thumbnail"], default_youtube_thumbnail_url("dQw4w9WgXcQ"))
                self.assertEqual(info["channel"], "Rick Astley")
                self.assertEqual(info["duration"], 213)
                self.assertTrue(info["is_cached"])
                self.assertTrue(info["was_previously_cached"])
                self.assertFalse(info["created_cache_entry"])
                self.assertIsNone(info["matched_cached_query"])
                self.assertIsNone(info["search_query_normalized"])

        mock_search.assert_not_called()

    async def test_exact_normalized_query_bypasses_yt_dlp_and_typos_miss(self):
        cached_path = self._create_cached_song(
            youtube_id="dQw4w9WgXcQ",
            thumbnail="https://i.ytimg.com/vi/dQw4w9WgXcQ/custom.jpg",
        )
        self.mixin.db_manager.upsert_cached_query("rick astley never gonna", "dQw4w9WgXcQ")

        with patch("playback_mixin.run_yt_dlp_search") as mock_search:
            info = await self.mixin._extract_info("  Rick   ASTLEY   never gonna  ", download=False)

        mock_search.assert_not_called()
        self.assertIsNotNone(info)
        self.assertEqual(info["url"], str(cached_path))
        self.assertEqual(info["thumbnail"], "https://i.ytimg.com/vi/dQw4w9WgXcQ/custom.jpg")
        self.assertEqual(info["matched_cached_query"], "rick astley never gonna")
        self.assertEqual(info["search_query_normalized"], "rick astley never gonna")
        self.assertEqual(info["source_query_normalized"], "rick astley never gonna")

        # Typo must NOT match and must fall through to yt-dlp.
        with patch("playback_mixin.run_yt_dlp_search", return_value=None) as mock_typo_search:
            typo_result = await self.mixin._extract_info("rick astley nevr gonna", download=False)

        self.assertIsNone(typo_result)
        mock_typo_search.assert_called_once_with("rick astley nevr gonna")

    async def test_cached_file_without_db_metadata_falls_back_and_populates_cached_songs(self):
        file_path = self.cache_dir / "youtube-abc12345678.opus"
        file_path.write_bytes(b"audio-bytes")
        self.mixin.song_cache.add("abc12345678", str(file_path))

        yt_data = {
            "id": "abc12345678",
            "title": "Populated Title",
            "duration": 185,
            "channel": "Populated Channel",
            "webpage_url": "https://www.youtube.com/watch?v=abc12345678",
            "thumbnail": "https://i.ytimg.com/vi/abc12345678/hqdefault.jpg",
        }

        with patch("playback_mixin.run_yt_dlp_search", return_value=yt_data) as mock_search:
            first_result = await self.mixin._extract_info(
                "https://www.youtube.com/watch?v=abc12345678",
                download=False,
            )

        mock_search.assert_called_once()
        self.assertTrue(first_result["is_cached"])
        self.assertIsNotNone(self.mixin.db_manager.get_cached_song("abc12345678"))

        # Subsequent lookup hits the fast path without yt-dlp.
        with patch("playback_mixin.run_yt_dlp_search") as mock_second_search:
            second_result = await self.mixin._extract_info(
                "https://youtu.be/abc12345678",
                download=False,
            )

        mock_second_search.assert_not_called()
        self.assertEqual(second_result["title"], "Populated Title")
        self.assertEqual(second_result["url"], str(file_path))

    async def test_lazy_cleanup_on_missing_or_empty_file_falls_back_to_yt_dlp(self):
        cached_path = self._create_cached_song(youtube_id="deleted1234")
        self.mixin.db_manager.upsert_cached_query("deleted track", "deleted1234")

        # Delete the file externally from disk.
        cached_path.unlink()

        stream_data = {
            "id": "deleted1234",
            "title": "Recovered Song",
            "url": "https://example.com/stream",
            "webpage_url": "https://www.youtube.com/watch?v=deleted1234",
            "channel": "Channel",
            "duration": 190,
        }

        with patch("playback_mixin.run_yt_dlp_search", return_value=stream_data) as mock_search:
            result = await self.mixin._extract_info("deleted track", download=False)

        mock_search.assert_called_once_with("deleted track")
        self.assertFalse(result["is_cached"])
        self.assertEqual(result["url"], "https://example.com/stream")
        self.assertIsNone(self.mixin.song_cache.get("deleted1234"))
        self.assertIsNone(self.mixin.db_manager.get_cached_song("deleted1234"))
        self.assertIsNone(self.mixin.db_manager.get_cached_query("deleted track"))

    async def test_lazy_cleanup_on_zero_byte_file(self):
        zero_path = self._create_cached_song(youtube_id="emptyFile12", content=b"")
        self.mixin.db_manager.upsert_cached_query("empty track", "emptyFile12")

        with patch("playback_mixin.run_yt_dlp_search", return_value=None) as mock_search:
            await self.mixin._extract_info("empty track", download=False)

        mock_search.assert_called_once_with("empty track")
        self.assertFalse(zero_path.exists())
        self.assertIsNone(self.mixin.song_cache.get("emptyFile12"))
        self.assertIsNone(self.mixin.db_manager.get_cached_song("emptyFile12"))
        self.assertIsNone(self.mixin.db_manager.get_cached_query("empty track"))

    async def test_new_download_upserts_cached_songs_and_eviction_deletes_it(self):
        search_data = {
            "id": "newVideo123",
            "title": "Brand New Song",
            "url": "https://example.com/stream",
            "webpage_url": "https://www.youtube.com/watch?v=newVideo123",
            "channel": "New Channel",
            "duration": 145,
            "thumbnail": "https://i.ytimg.com/vi/newVideo123/hqdefault.jpg",
        }

        def fake_download(_query, _download):
            (self.cache_dir / "youtube-newVideo123.opus").write_bytes(b"audio")
            return {
                "id": "newVideo123",
                "title": "Brand New Song",
                "ext": "opus",
                "extractor": "youtube",
            }

        with (
            patch("playback_mixin.SONG_CACHE_DIR", self.cache_dir),
            patch("playback_mixin.run_yt_dlp_search", return_value=search_data),
            patch("playback_mixin.run_yt_dlp_extractor", side_effect=fake_download),
        ):
            song_info = await self.mixin._extract_info("brand new song", download=True)

        self.assertTrue(song_info["is_cached"])
        self.assertTrue(song_info["created_cache_entry"])
        cached_row = self.mixin.db_manager.get_cached_song("newVideo123")
        self.assertIsNotNone(cached_row)
        self.assertEqual(cached_row["title"], "Brand New Song")

        # Add a query mapping and then evict the new download.
        self.mixin.db_manager.upsert_cached_query("brand new song", "newVideo123")
        status = self.mixin._request_cache_eviction(song_info)
        self.assertEqual(status, "deleted")
        self.assertIsNone(self.mixin.db_manager.get_cached_song("newVideo123"))
        self.assertIsNone(self.mixin.db_manager.get_cached_query("brand new song"))


class QueryCacheWriteAndInvalidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.cache_dir = self.root / "song_cache"
        self.cache_dir.mkdir()
        self.db_path = self.root / "music_log.db"
        self.db_manager = DatabaseManager(str(self.db_path))
        self.song_cache = SongCache(self.cache_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _seed_cached_song(self, youtube_id="song1234567", title="Test Song"):
        file_path = self.cache_dir / f"youtube-{youtube_id}.opus"
        file_path.write_bytes(b"audio-data")
        self.song_cache.add(youtube_id, str(file_path))
        self.db_manager.upsert_cached_song(
            youtube_id=youtube_id,
            title=title,
            channel_name="Test Channel",
            duration=200,
        )
        return file_path

    class _DummyVoiceClient:
        def __init__(self, playing=True):
            self._playing = playing
            self.stopped = False
            self.played = []
            self.channel = object()

        def is_connected(self):
            return True

        def is_playing(self):
            return self._playing

        def is_paused(self):
            return False

        def stop(self):
            self.stopped = True
            self._playing = False

        def play(self, source, after=None):
            self.played.append((source, after))
            self._playing = True

    class _DummyContext:
        def __init__(self, voice_channel=None):
            self.author = MagicMock()
            self.author.id = 42
            self.author.__str__ = lambda _self: "Alice"
            self.author.voice = MagicMock(channel=voice_channel) if voice_channel else None
            self.guild = MagicMock()
            self.guild.id = 10
            self.guild.name = "Test Server"
            self.guild.voice_client = None
            self.messages = []
            self.embeds = []

        async def send(self, content=None, *, embed=None):
            if content is not None:
                self.messages.append(content)
            if embed is not None:
                self.embeds.append(embed)
            msg = MagicMock()
            msg.edit = AsyncMock()
            return msg

    def test_natural_completion_writes_cached_query_for_cached_track_only(self):
        file_path = self._seed_cached_song("song1234567")

        mixin = PlaybackMixin()
        mixin.db_manager = self.db_manager
        mixin.song_cache = self.song_cache
        mixin.queues = {}
        mixin.last_activity = {}
        mixin.pending_cache_evictions = {}
        mixin.get_queue = lambda guild_id: mixin.queues.setdefault(guild_id, deque())

        request_id = self.db_manager.log_song_request(
            user_id=1,
            user_name="Alice",
            guild_id=10,
            query="My Favorite Song",
            resolved_title="Test Song",
            resolved_url=canonical_youtube_url("song1234567"),
            channel_name="Test Channel",
            duration=200,
        )
        mixin.current_song = {
            10: {
                "request_id": request_id,
                "youtube_id": "song1234567",
                "title": "Test Song",
                "url": str(file_path),
                "is_cached": True,
                "search_query_normalized": "my favorite song",
            }
        }

        mixin._play_next(10)
        self.assertEqual(self.db_manager.get_cached_query("my favorite song"), "song1234567")

        # Uncached streamed song completing must NOT write cached_queries.
        mixin.current_song = {
            10: {
                "youtube_id": "song1234567",
                "title": "Test Song",
                "url": "https://example.com/stream",
                "is_cached": False,
                "search_query_normalized": "streamed query",
            }
        }
        mixin._play_next(10)
        self.assertIsNone(self.db_manager.get_cached_query("streamed query"))

    async def test_late_skip_writes_cached_query_but_early_skip_and_remove_do_not(self):
        file_path = self._seed_cached_song("song1234567")

        class Harness(PlaybackMixin, VoiceCommandsMixin, QueueCommandsMixin):
            pass

        cog = Harness()
        cog.db_manager = self.db_manager
        cog.song_cache = self.song_cache
        cog.queues = {10: deque()}
        cog.last_activity = {}
        cog.pending_cache_evictions = {}
        cog.get_queue = lambda guild_id: cog.queues.setdefault(guild_id, deque())

        # 1. Early skip (< 60%) -> marked skipped, NOT written to cached_queries.
        early_req_id = self.db_manager.log_song_request(
            1, "Alice", 10, "early skip query", "Test Song",
            canonical_youtube_url("song1234567"), "Channel", 200
        )
        cog.voice_clients = {10: self._DummyVoiceClient()}
        cog.current_song = {
            10: {
                "request_id": early_req_id,
                "youtube_id": "song1234567",
                "title": "Test Song",
                "url": str(file_path),
                "duration": 200,
                "start_time": 100,
                "is_cached": True,
                "search_query_normalized": "early skip query",
            }
        }
        ctx = self._DummyContext()
        with patch("voice_commands_mixin.time.time", return_value=140):
            await VoiceCommandsMixin.skip.callback(cog, ctx)
        self.assertIsNone(self.db_manager.get_cached_query("early skip query"))

        # 2. Removed from queue before playing -> NOT written to cached_queries.
        removed_req_id = self.db_manager.log_song_request(
            1, "Alice", 10, "removed query", "Test Song",
            canonical_youtube_url("song1234567"), "Channel", 200
        )
        cog.queues[10].append({
            "request_id": removed_req_id,
            "youtube_id": "song1234567",
            "title": "Test Song",
            "url": str(file_path),
            "is_cached": True,
            "search_query_normalized": "removed query",
        })
        await QueueCommandsMixin.remove.callback(cog, ctx, 1)
        self.assertIsNone(self.db_manager.get_cached_query("removed query"))

        # 3. Late skip (>= 60%) -> marked completed, written to cached_queries.
        late_req_id = self.db_manager.log_song_request(
            1, "Alice", 10, "late skip query", "Test Song",
            canonical_youtube_url("song1234567"), "Channel", 200
        )
        cog.voice_clients = {10: self._DummyVoiceClient()}
        cog.current_song = {
            10: {
                "request_id": late_req_id,
                "youtube_id": "song1234567",
                "title": "Test Song",
                "url": str(file_path),
                "duration": 200,
                "start_time": 100,
                "is_cached": True,
                "search_query_normalized": "late skip query",
            }
        }
        with patch("voice_commands_mixin.time.time", return_value=230):
            await VoiceCommandsMixin.skip.callback(cog, ctx)
        self.assertEqual(self.db_manager.get_cached_query("late skip query"), "song1234567")

    async def test_query_cache_hit_invalidation_on_early_skip(self):
        file_path = self._seed_cached_song("song1234567")
        self.db_manager.upsert_cached_query("cached hit query", "song1234567")
        self.db_manager.upsert_cached_query("kept hit query", "song1234567")

        cog = VoiceCommandsMixin()
        cog.db_manager = self.db_manager
        cog.last_activity = {}

        # Skip within 10s deletes ONLY cached_queries row; cached_songs & file stay intact.
        cog.voice_clients = {10: self._DummyVoiceClient()}
        cog.current_song = {
            10: {
                "youtube_id": "song1234567",
                "title": "Test Song",
                "url": str(file_path),
                "duration": 200,
                "start_time": 100,
                "is_cached": True,
                "created_cache_entry": False,
                "matched_cached_query": "cached hit query",
                "search_query_normalized": "cached hit query",
            }
        }
        ctx = self._DummyContext()
        with patch("voice_commands_mixin.time.time", return_value=108):
            await VoiceCommandsMixin.skip.callback(cog, ctx)

        self.assertIsNone(self.db_manager.get_cached_query("cached hit query"))
        self.assertIsNotNone(self.db_manager.get_cached_song("song1234567"))
        self.assertTrue(file_path.exists())

        # Skip after 10s keeps cached_queries row.
        cog.voice_clients = {10: self._DummyVoiceClient()}
        cog.current_song = {
            10: {
                "youtube_id": "song1234567",
                "title": "Test Song",
                "url": str(file_path),
                "duration": 200,
                "start_time": 100,
                "is_cached": True,
                "created_cache_entry": False,
                "matched_cached_query": "kept hit query",
                "search_query_normalized": "kept hit query",
            }
        }
        with patch("voice_commands_mixin.time.time", return_value=111):
            await VoiceCommandsMixin.skip.callback(cog, ctx)

        self.assertEqual(self.db_manager.get_cached_query("kept hit query"), "song1234567")

    async def test_short_song_query_cache_hit_skipped_within_10s_still_invalidates(self):
        file_path = self._seed_cached_song("shortSong11")
        self.db_manager.upsert_cached_query("short meme", "shortSong11")

        cog = VoiceCommandsMixin()
        cog.db_manager = self.db_manager
        cog.last_activity = {}
        cog.voice_clients = {10: self._DummyVoiceClient()}
        cog.current_song = {
            10: {
                "youtube_id": "shortSong11",
                "title": "Short Meme",
                "url": str(file_path),
                "duration": 4,
                "start_time": 100,
                "is_cached": True,
                "created_cache_entry": False,
                "matched_cached_query": "short meme",
                "search_query_normalized": "short meme",
            }
        }
        ctx = self._DummyContext()
        # 3s elapsed is both >= 60% of 4s AND <= 10s; invalidation must win.
        with patch("voice_commands_mixin.time.time", return_value=103):
            await VoiceCommandsMixin.skip.callback(cog, ctx)

        self.assertIsNone(self.db_manager.get_cached_query("short meme"))

    async def test_play_preserves_search_query_normalized_across_download_step(self):
        vc = self._DummyVoiceClient(playing=False)
        ctx = self._DummyContext(voice_channel=vc.channel)

        cog = VoiceCommandsMixin()
        cog.db_manager = self.db_manager
        cog.song_cache = self.song_cache
        cog.cache_downloads_enabled = True
        cog.voice_clients = {10: vc}
        cog.current_song = {}
        cog.queues = {10: deque()}
        cog.last_activity = {}
        cog.get_queue = lambda guild_id: cog.queues.setdefault(guild_id, deque())
        cog._format_duration = lambda s: "03:00"

        extracted_calls = []

        async def fake_extract(query, download=False):
            extracted_calls.append((query, download))
            if not download:
                return {
                    "title": "Downloaded Song",
                    "url": "https://example.com/stream",
                    "webpage_url": canonical_youtube_url("dlVideo1234"),
                    "channel": "Channel",
                    "duration": 180,
                    "youtube_id": "dlVideo1234",
                    "is_cached": False,
                    "was_previously_cached": False,
                    "created_cache_entry": False,
                    "search_query_normalized": normalize_search_query(query),
                }
            # Download step receives the webpage_url, so its own normalized query is None.
            return {
                "title": "Downloaded Song",
                "url": str(self.cache_dir / "youtube-dlVideo1234.opus"),
                "webpage_url": canonical_youtube_url("dlVideo1234"),
                "channel": "Channel",
                "duration": 180,
                "youtube_id": "dlVideo1234",
                "is_cached": True,
                "was_previously_cached": False,
                "created_cache_entry": True,
                "search_query_normalized": None,
            }

        cog._extract_info = fake_extract

        with patch("voice_commands_mixin.discord.FFmpegOpusAudio.from_probe", new=AsyncMock(return_value=object())):
            await VoiceCommandsMixin.play.callback(cog, ctx, query="  My  Search  Terms ")

        self.assertEqual(len(extracted_calls), 2)
        self.assertEqual(cog.current_song[10]["search_query_normalized"], "my search terms")
        self.assertEqual(cog.current_song[10]["source_query_normalized"], "my search terms")


class StatsLongUrlGroupingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "music_log.db"
        self.manager = DatabaseManager(str(self.db_path))

    def tearDown(self):
        self.temp_dir.cleanup()

    def _insert(self, *, title, url, timestamp, user_id=1, guild_id=10, status="completed"):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO play_history (
                    request_timestamp, user_id, user_name, guild_id,
                    query, resolved_title, resolved_url, channel_name, duration, play_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    str(user_id),
                    f"User {user_id}",
                    str(guild_id),
                    title,
                    title,
                    url,
                    "Channel",
                    180,
                    status,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_top_5_requests_groups_by_url_with_latest_title_and_title_fallback(self):
        same_url = "https://www.youtube.com/watch?v=renamed1234"
        self._insert(title="Old Video Title", url=same_url, timestamp="2026-01-01T10:00:00+00:00")
        self._insert(title="Middle Video Title", url=same_url, timestamp="2026-01-02T10:00:00+00:00")
        self._insert(title="Latest Video Title", url=same_url, timestamp="2026-01-03T10:00:00+00:00")
        # Skipped play on same URL should not count.
        self._insert(
            title="Skipped Newer Title",
            url=same_url,
            timestamp="2026-01-04T10:00:00+00:00",
            status="skipped",
        )
        # Other user / other guild should not count.
        self._insert(title="Other User", url=same_url, timestamp="2026-01-05T10:00:00+00:00", user_id=2)
        self._insert(title="Other Guild", url=same_url, timestamp="2026-01-05T10:00:00+00:00", guild_id=20)

        # Fallback grouping by title when resolved_url IS NULL.
        self._insert(title="No URL Track", url=None, timestamp="2026-01-01T11:00:00+00:00")
        self._insert(title="No URL Track", url=None, timestamp="2026-01-02T11:00:00+00:00")

        # Another distinct URL.
        self._insert(
            title="Single Play",
            url="https://www.youtube.com/watch?v=single12345",
            timestamp="2026-01-01T12:00:00+00:00",
        )

        stats = self.manager.get_user_stats_long(user_id=1, guild_id=10)
        self.assertEqual(
            stats["top_5_requests"],
            [
                {"title": "Latest Video Title", "count": 3},
                {"title": "No URL Track", "count": 2},
                {"title": "Single Play", "count": 1},
            ],
        )


class ClearCacheRemovalTests(unittest.TestCase):
    def test_clearcache_command_is_not_registered(self):
        self.assertFalse(hasattr(CacheCommandsMixin, "clear_cache"))
        self.assertFalse(hasattr(MusicCog, "clear_cache"))
        cog_command_names = {cmd.name for cmd in MusicCog.__cog_commands__}
        self.assertNotIn("clearcache", cog_command_names)
        self.assertIn("cache", cog_command_names)
        self.assertIn("cacheleaderboard", cog_command_names)


if __name__ == "__main__":
    unittest.main()
