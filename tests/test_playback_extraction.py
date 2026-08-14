import concurrent.futures
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src" / "music_bot"
sys.path.insert(0, str(SRC_DIR))

from playback_mixin import PlaybackMixin


class PlaybackExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mixin = PlaybackMixin()
        self.mixin.process_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    async def asyncTearDown(self):
        self.mixin.process_executor.shutdown(wait=True)

    @patch("playback_mixin.run_yt_dlp_search", return_value={"entries": []})
    async def test_empty_search_entries_are_reported_clearly(self, _mock_search):
        with self.assertLogs("playback_mixin", level="WARNING") as captured_logs:
            result = await self.mixin._extract_info("missing song")

        self.assertIsNone(result)
        self.assertIn(
            "yt-dlp returned no playable entries for 'missing song'",
            "\n".join(captured_logs.output),
        )


if __name__ == "__main__":
    unittest.main()
