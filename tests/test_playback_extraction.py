import concurrent.futures
from collections import deque
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src" / "music_bot"
sys.path.insert(0, str(SRC_DIR))

from playback_mixin import PlaybackMixin
from song_cache import SongCache


class PlaybackExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mixin = PlaybackMixin()
        self.mixin.process_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.mixin.thread_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    async def asyncTearDown(self):
        self.mixin.process_executor.shutdown(wait=True)
        self.mixin.thread_executor.shutdown(wait=True)

    @patch("playback_mixin.run_yt_dlp_search", return_value={"entries": []})
    async def test_empty_search_entries_are_reported_clearly(self, _mock_search):
        with self.assertLogs("playback_mixin", level="WARNING") as captured_logs:
            result = await self.mixin._extract_info("missing song")

        self.assertIsNone(result)
        self.assertIn(
            "yt-dlp returned no playable entries for 'missing song'",
            "\n".join(captured_logs.output),
        )

    async def test_over_limit_download_is_removed_and_streamed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            self.mixin.song_cache = SongCache(
                cache_dir,
                max_size_bytes=5,
                warning_threshold_bytes=1,
            )

            search_data = {
                "id": "abc123",
                "title": "Test Song",
                "url": "https://example.com/audio-stream",
                "webpage_url": "https://example.com/watch/abc123",
                "duration": 120,
            }

            def download_to_cache(_query, _download):
                (cache_dir / "youtube-abc123.opus").write_bytes(b"123456")
                return {
                    "id": "abc123",
                    "title": "Test Song",
                    "ext": "opus",
                    "extractor": "youtube",
                }

            with (
                patch("playback_mixin.SONG_CACHE_DIR", cache_dir),
                patch("playback_mixin.run_yt_dlp_search", return_value=search_data),
                patch("playback_mixin.run_yt_dlp_extractor", side_effect=download_to_cache),
            ):
                result = await self.mixin._extract_info(
                    "https://example.com/watch/abc123",
                    download=True,
                )

            self.assertFalse(result["is_cached"])
            self.assertEqual(result["url"], "https://example.com/audio-stream")
            self.assertFalse((cache_dir / "youtube-abc123.opus").exists())

    async def test_successful_download_marks_the_request_as_cache_creator(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            self.mixin.song_cache = SongCache(cache_dir)
            search_data = {
                "id": "abc123",
                "title": "Test Song",
                "url": "https://example.com/audio-stream",
                "webpage_url": "https://example.com/watch/abc123",
                "duration": 120,
            }

            def download_to_cache(_query, _download):
                (cache_dir / "youtube-abc123.opus").write_bytes(b"song")
                return {
                    "id": "abc123",
                    "title": "Test Song",
                    "ext": "opus",
                    "extractor": "youtube",
                }

            with (
                patch("playback_mixin.SONG_CACHE_DIR", cache_dir),
                patch("playback_mixin.run_yt_dlp_search", return_value=search_data),
                patch("playback_mixin.run_yt_dlp_extractor", side_effect=download_to_cache),
            ):
                result = await self.mixin._extract_info(
                    "https://example.com/watch/abc123",
                    download=True,
                )

            self.assertTrue(result["created_cache_entry"])


class CacheEvictionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.temp_dir.name)
        self.mixin = PlaybackMixin()
        self.mixin.song_cache = SongCache(self.cache_dir)
        self.mixin.current_song = {}
        self.mixin.queues = {}
        self.mixin.pending_cache_evictions = {}

    def tearDown(self):
        self.temp_dir.cleanup()

    def _new_download(self):
        cached_file = self.cache_dir / "youtube-abc123.opus"
        cached_file.write_bytes(b"song")
        self.mixin.song_cache.add("abc123", str(cached_file))
        return {
            "title": "Test Song",
            "youtube_id": "abc123",
            "url": str(cached_file),
            "is_cached": True,
            "created_cache_entry": True,
        }

    def test_rejected_new_download_is_deleted_when_unreferenced(self):
        song_info = self._new_download()

        status = self.mixin._request_cache_eviction(song_info)

        self.assertEqual(status, "deleted")
        self.assertFalse(Path(song_info["url"]).exists())
        self.assertEqual(self.mixin.pending_cache_evictions, {})

    def test_eviction_waits_until_other_queue_reference_is_gone(self):
        song_info = self._new_download()
        self.mixin.queues[20] = deque([{**song_info, "created_cache_entry": False}])

        status = self.mixin._request_cache_eviction(song_info)

        self.assertEqual(status, "deferred")
        self.assertTrue(Path(song_info["url"]).exists())

        self.mixin.queues[20].clear()
        results = self.mixin._process_pending_cache_evictions()

        self.assertEqual(results["abc123"], "deleted")
        self.assertFalse(Path(song_info["url"]).exists())

    def test_previously_cached_request_is_not_evicted(self):
        song_info = self._new_download()
        song_info["created_cache_entry"] = False

        status = self.mixin._request_cache_eviction(song_info)

        self.assertIsNone(status)
        self.assertTrue(Path(song_info["url"]).exists())


if __name__ == "__main__":
    unittest.main()
