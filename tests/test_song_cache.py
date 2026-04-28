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


if __name__ == "__main__":
    unittest.main()
