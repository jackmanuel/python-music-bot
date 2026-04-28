import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src" / "music_bot"
sys.path.insert(0, str(SRC_DIR))

from voice_commands_mixin import VoiceCommandsMixin


class CacheDownloadPolicyTests(unittest.TestCase):
    def test_uncached_song_downloads_when_cache_downloads_are_enabled(self):
        mixin = VoiceCommandsMixin()
        mixin.cache_downloads_enabled = True

        self.assertTrue(mixin._should_download_to_cache({"is_cached": False}))

    def test_cached_song_does_not_download_again(self):
        mixin = VoiceCommandsMixin()
        mixin.cache_downloads_enabled = True

        self.assertFalse(mixin._should_download_to_cache({"is_cached": True}))

    def test_uncached_song_streams_when_cache_downloads_are_disabled(self):
        mixin = VoiceCommandsMixin()
        mixin.cache_downloads_enabled = False

        self.assertFalse(mixin._should_download_to_cache({"is_cached": False}))

    def test_over_limit_song_streams_even_when_cache_downloads_are_enabled(self):
        mixin = VoiceCommandsMixin()
        mixin.cache_downloads_enabled = True

        self.assertFalse(
            mixin._should_download_to_cache({
                "is_cached": False,
                "exceeds_cache_duration": True,
            })
        )


if __name__ == "__main__":
    unittest.main()
