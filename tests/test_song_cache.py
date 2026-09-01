import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src" / "music_bot"
sys.path.insert(0, str(SRC_DIR))

from song_cache import SongCache


class SongCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_no_cache_mode_does_not_create_missing_directory(self):
        cache_dir = self.temp_dir / "missing-cache"

        cache = SongCache(cache_dir, create_if_missing=False)

        self.assertEqual(len(cache), 0)
        self.assertFalse(cache_dir.exists())

    def test_no_cache_mode_loads_existing_cached_files(self):
        cache_dir = self.temp_dir / "song_cache"
        cache_dir.mkdir()
        cached_file = cache_dir / "youtube-abc123.opus"
        cached_file.touch()

        cache = SongCache(cache_dir, create_if_missing=False)

        self.assertEqual(cache.get("abc123"), str(cached_file))

    def test_rejects_file_that_exceeds_hard_limit(self):
        cache_dir = self.temp_dir / "song_cache"
        cache_dir.mkdir()
        cache = SongCache(cache_dir, max_size_bytes=5, warning_threshold_bytes=1)

        cached_file = cache_dir / "youtube-abc123.opus"
        cached_file.write_bytes(b"123456")

        self.assertFalse(cache.add("abc123", str(cached_file)))
        self.assertIsNone(cache.get("abc123"))

    def test_warns_when_cache_is_almost_full(self):
        cache_dir = self.temp_dir / "song_cache"
        cache_dir.mkdir()
        (cache_dir / "youtube-abc123.opus").write_bytes(b"12345678")

        with self.assertLogs("song_cache", level="WARNING") as captured_logs:
            SongCache(cache_dir, max_size_bytes=10, warning_threshold_bytes=3)

        self.assertIn("Song cache is almost full", "\n".join(captured_logs.output))

    def test_accepts_file_that_exactly_reaches_hard_limit(self):
        cache_dir = self.temp_dir / "song_cache"
        cache_dir.mkdir()
        cache = SongCache(cache_dir, max_size_bytes=5, warning_threshold_bytes=1)

        cached_file = cache_dir / "youtube-abc123.opus"
        cached_file.write_bytes(b"12345")

        self.assertTrue(cache.add("abc123", str(cached_file)))
        self.assertEqual(cache.get("abc123"), str(cached_file))
        self.assertFalse(cache.can_accept_download())

    def test_removes_one_indexed_file(self):
        cache_dir = self.temp_dir / "song_cache"
        cache_dir.mkdir()
        cache = SongCache(cache_dir)
        cached_file = cache_dir / "youtube-abc123.opus"
        cached_file.write_bytes(b"song")
        cache.add("abc123", str(cached_file))

        self.assertTrue(cache.remove("abc123", expected_file_path=str(cached_file)))
        self.assertFalse(cached_file.exists())
        self.assertIsNone(cache.get("abc123"))

    def test_refuses_to_remove_when_the_indexed_path_changed(self):
        cache_dir = self.temp_dir / "song_cache"
        cache_dir.mkdir()
        cache = SongCache(cache_dir)
        cached_file = cache_dir / "youtube-abc123.opus"
        cached_file.write_bytes(b"song")
        cache.add("abc123", str(cached_file))

        self.assertFalse(cache.remove("abc123", expected_file_path=str(cache_dir / "other.opus")))
        self.assertTrue(cached_file.exists())
        self.assertEqual(cache.get("abc123"), str(cached_file))


if __name__ == "__main__":
    unittest.main()
