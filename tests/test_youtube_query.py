import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src" / "music_bot"
sys.path.insert(0, str(SRC_DIR))

from youtube import (
    REMOTE_COMPONENTS,
    YDL_OPTIONS,
    age_restricted_error_result,
    is_age_restricted_yt_dlp_error,
    is_direct_url,
    is_livestream_info,
    prepare_yt_dlp_query,
    run_yt_dlp_search,
    select_first_video_entry,
    select_video_entries,
    video_url_from_entry,
)
import yt_dlp


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

    def test_age_restricted_error_ignores_unavailable_format_messages(self):
        error = (
            "\x1b[0;31mERROR:\x1b[0m [youtube] vXg8IVbOY_E: Requested format is not available. "
            "Use --list-formats for a list of available formats."
        )

        self.assertFalse(is_age_restricted_yt_dlp_error(error))

    def test_age_restricted_error_detects_explicit_age_gate(self):
        self.assertTrue(is_age_restricted_yt_dlp_error("Sign in to confirm your age"))
        self.assertFalse(is_age_restricted_yt_dlp_error("[soundcloud] Requested format is not available."))

    @patch("youtube.yt_dlp.YoutubeDL")
    def test_run_yt_dlp_search_returns_picklable_age_restricted_result(self, mock_youtube_dl):
        ydl = MagicMock()
        ydl.extract_info.side_effect = yt_dlp.utils.DownloadError("Sign in to confirm your age")
        mock_youtube_dl.return_value.__enter__.return_value = ydl

        self.assertEqual(run_yt_dlp_search("https://www.youtube.com/watch?v=abc123"), age_restricted_error_result())


if __name__ == "__main__":
    unittest.main()
