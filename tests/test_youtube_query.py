import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src" / "music_bot"
sys.path.insert(0, str(SRC_DIR))

from youtube import (
    REMOTE_COMPONENTS,
    YDL_OPTIONS,
    is_direct_url,
    is_livestream_info,
    prepare_yt_dlp_query,
    select_first_video_entry,
    select_video_entries,
    video_url_from_entry,
)


class YoutubeQueryTests(unittest.TestCase):
    def test_special_character_query_is_forced_to_search(self):
        self.assertEqual(prepare_yt_dlp_query("$$$"), "ytsearch1:$$$")

    def test_plain_search_query_is_forced_to_search(self):
        self.assertEqual(
            prepare_yt_dlp_query("artist - song name"),
            "ytsearch1:artist - song name",
        )

    def test_direct_http_urls_are_left_unchanged(self):
        url = "https://www.youtube.com/watch?v=abc123"
        self.assertTrue(is_direct_url(url))
        self.assertEqual(prepare_yt_dlp_query(url), url)

    def test_existing_search_prefix_is_left_unchanged(self):
        query = "ytsearch1:$$$"
        self.assertEqual(prepare_yt_dlp_query(query), query)

    def test_remote_components_are_passed_as_a_list(self):
        self.assertEqual(REMOTE_COMPONENTS, ["ejs:github"])
        self.assertEqual(YDL_OPTIONS["remote_components"], ["ejs:github"])

    def test_search_selection_skips_channels_and_uses_first_video(self):
        selected = select_first_video_entry(
            [
                {
                    "id": "UCeSL5leXCREGvpDpoWcSb8g",
                    "title": "Matt Ox",
                    "url": "https://www.youtube.com/channel/UCeSL5leXCREGvpDpoWcSb8g",
                    "ie_key": "YoutubeTab",
                    "_type": "url",
                },
                {
                    "id": "0cZ8-RgtrP0",
                    "title": "MATT OX - Overwhelming",
                    "url": "https://www.youtube.com/watch?v=0cZ8-RgtrP0",
                    "ie_key": "Youtube",
                    "_type": "url",
                },
                {
                    "id": "pAm1PDrfi4o",
                    "title": "XXXTENTACION & MATT OX - $$$",
                    "url": "https://www.youtube.com/watch?v=pAm1PDrfi4o",
                    "ie_key": "Youtube",
                    "_type": "url",
                },
            ],
        )

        assert selected is not None
        self.assertEqual(video_url_from_entry(selected), "https://www.youtube.com/watch?v=0cZ8-RgtrP0")

    def test_search_results_selection_returns_top_playable_videos(self):
        results = select_video_entries(
            [
                {
                    "id": "UCeSL5leXCREGvpDpoWcSb8g",
                    "title": "Matt Ox",
                    "url": "https://www.youtube.com/channel/UCeSL5leXCREGvpDpoWcSb8g",
                    "ie_key": "YoutubeTab",
                    "_type": "url",
                },
                {
                    "id": "0cZ8-RgtrP0",
                    "title": "MATT OX - Overwhelming",
                    "url": "https://www.youtube.com/watch?v=0cZ8-RgtrP0",
                    "ie_key": "Youtube",
                    "_type": "url",
                },
                {
                    "id": "pAm1PDrfi4o",
                    "title": "XXXTENTACION & MATT OX - $$$",
                    "url": "https://www.youtube.com/watch?v=pAm1PDrfi4o",
                    "ie_key": "Youtube",
                    "_type": "url",
                },
                {
                    "id": "skipped",
                    "title": "Skipped by limit",
                    "url": "https://www.youtube.com/watch?v=skipped",
                    "ie_key": "Youtube",
                    "_type": "url",
                },
            ],
            limit=2,
        )

        self.assertEqual([entry["id"] for entry in results], ["0cZ8-RgtrP0", "pAm1PDrfi4o"])

    def test_livestream_info_detects_active_livestreams(self):
        self.assertTrue(is_livestream_info({"is_live": True}))
        self.assertTrue(is_livestream_info({"live_status": "is_live"}))
        self.assertFalse(is_livestream_info({"is_live": False, "live_status": "was_live"}))


if __name__ == "__main__":
    unittest.main()
