import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src" / "music_bot"
sys.path.insert(0, str(SRC_DIR))

from cache_investigator import (
    CacheCandidate,
    extract_youtube_id,
    investigate_cache,
    resolve_missing_titles,
    review_deletions,
    print_report,
    youtube_terminal_link,
)


class CacheInvestigatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.cache_dir = self.root / "song_cache"
        self.cache_dir.mkdir()
        self.database = self.root / "music_log.db"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "CREATE TABLE play_history (resolved_url TEXT, play_status TEXT NOT NULL, "
                "resolved_title TEXT)"
            )
            connection.commit()

    def tearDown(self):
        self.temp_dir.cleanup()

    def add_history(self, youtube_id, *statuses, title="Stored title"):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executemany(
                "INSERT INTO play_history VALUES (?, ?, ?)",
                [
                    (f"https://www.youtube.com/watch?v={youtube_id}", status, title)
                    for status in statuses
                ],
            )
            connection.commit()

    def test_extracts_common_youtube_url_forms(self):
        self.assertEqual(extract_youtube_id("https://youtu.be/abcdefghijk"), "abcdefghijk")
        self.assertEqual(
            extract_youtube_id("https://www.youtube.com/shorts/abcdefghijk"),
            "abcdefghijk",
        )
        self.assertIsNone(extract_youtube_id("https://soundcloud.com/user/song"))

    def test_terminal_link_keeps_filename_as_visible_text(self):
        link = youtube_terminal_link("youtube-abcdefghijk.opus", "abcdefghijk")

        self.assertIn("https://www.youtube.com/watch?v=abcdefghijk", link)
        self.assertIn("youtube-abcdefghijk.opus", link)
        self.assertEqual(
            youtube_terminal_link("youtube-abcdefghijk.opus", "abcdefghijk", enabled=False),
            "youtube-abcdefghijk.opus",
        )

    def test_prioritizes_total_entries_before_file_size(self):
        (self.cache_dir / "youtube-fewentries1.opus").write_bytes(b"small")
        (self.cache_dir / "youtube-moreentries.opus").write_bytes(b"much-larger-file")
        self.add_history("fewentries1", "completed")
        self.add_history("moreentries", "completed", "skipped")

        candidates, _ = investigate_cache(self.cache_dir, self.database)

        self.assertEqual(
            [candidate.path.name for candidate in candidates],
            ["youtube-fewentries1.opus", "youtube-moreentries.opus"],
        )
        self.assertEqual(candidates[1].database_entries, 2)
        self.assertEqual(candidates[1].completed_plays, 1)
        self.assertEqual(candidates[1].title, "Stored title")

    def test_sorts_equal_entry_counts_by_largest_file(self):
        (self.cache_dir / "youtube-abcdefghijk.opus").write_bytes(b"small")
        (self.cache_dir / "youtube-lmnopqrst.opus").write_bytes(b"much-larger-file")

        candidates, _ = investigate_cache(self.cache_dir, self.database)

        self.assertEqual(candidates[0].path.name, "youtube-lmnopqrst.opus")

    def test_excludes_files_over_completed_play_limit(self):
        (self.cache_dir / "youtube-abcdefghijk.opus").write_bytes(b"song")
        self.add_history("abcdefghijk", "completed", "completed", "skipped")

        candidates, _ = investigate_cache(self.cache_dir, self.database, max_plays=1)

        self.assertEqual(candidates, [])

    def test_largest_mode_includes_all_play_counts_and_sorts_by_size(self):
        small_path = self.cache_dir / "youtube-smallfile01.opus"
        large_path = self.cache_dir / "youtube-largefile01.opus"
        small_path.write_bytes(b"small")
        large_path.write_bytes(b"a much larger cached file")
        self.add_history("largefile01", "completed", "completed", "completed")

        candidates, _ = investigate_cache(
            self.cache_dir,
            self.database,
            max_plays=0,
            largest_first=True,
        )

        self.assertEqual(
            [candidate.path.name for candidate in candidates],
            ["youtube-largefile01.opus", "youtube-smallfile01.opus"],
        )
        self.assertEqual(candidates[0].completed_plays, 3)

    def test_reports_partial_and_soundcloud_files_as_unmatched(self):
        (self.cache_dir / "youtube-abcdefghijk.mp4.part").write_bytes(b"partial")
        (self.cache_dir / "soundcloud-123456.opus").write_bytes(b"song")

        _, unmatched = investigate_cache(self.cache_dir, self.database)

        self.assertEqual(len(unmatched), 2)
        self.assertEqual(unmatched[0].path.name, "youtube-abcdefghijk.mp4.part")
        self.assertEqual(
            unmatched[0].source_url,
            "https://www.youtube.com/watch?v=abcdefghijk",
        )

    def test_fetches_only_titles_missing_from_database(self):
        stored = CacheCandidate(
            self.cache_dir / "youtube-storedtitle.opus",
            "storedtitle",
            4,
            1,
            1,
            "Database title",
        )
        missing = CacheCandidate(
            self.cache_dir / "youtube-missingttl.opus",
            "missingttl",
            4,
            0,
            0,
        )
        fetched_urls = []

        resolved, _ = resolve_missing_titles(
            [stored, missing],
            [],
            title_fetcher=lambda url: fetched_urls.append(url) or "Fetched title",
        )

        self.assertEqual(resolved[0].title, "Database title")
        self.assertEqual(resolved[1].title, "Fetched title")
        self.assertEqual(len(fetched_urls), 1)

    def test_review_deletes_only_explicit_yes_choices(self):
        delete_path = self.cache_dir / "youtube-deletefile1.opus"
        keep_path = self.cache_dir / "youtube-keepthisone.opus"
        delete_path.write_bytes(b"delete")
        keep_path.write_bytes(b"keep")
        items = [
            CacheCandidate(delete_path, "deletefile1", 6, 0, 0, "Delete me"),
            CacheCandidate(keep_path, "keepthisone", 4, 0, 0, "Keep me"),
        ]
        answers = iter(["yes", "no"])

        deleted_count, deleted_bytes = review_deletions(
            items,
            [],
            input_func=lambda _prompt: next(answers),
        )

        self.assertEqual((deleted_count, deleted_bytes), (1, 6))
        self.assertFalse(delete_path.exists())
        self.assertTrue(keep_path.exists())

    def test_report_has_separate_title_and_filename_columns(self):
        candidate = CacheCandidate(
            self.cache_dir / "youtube-abcdefghijk.opus",
            "abcdefghijk",
            1024,
            1,
            1,
            "Example title",
        )
        output = StringIO()

        with redirect_stdout(output):
            print_report([candidate], [], limit=1, show_unmatched=False, use_links=False)

        lines = output.getvalue().splitlines()
        self.assertIn("Title", lines[1])
        self.assertIn("Filename", lines[1])
        self.assertIn("Example title", lines[3])
        self.assertIn("youtube-abcdefghijk.opus", lines[3])

    def test_report_labels_largest_mode(self):
        output = StringIO()

        with redirect_stdout(output):
            print_report([], [], 1, False, use_links=False, largest_first=True)

        self.assertEqual(output.getvalue().splitlines()[0], "Largest cache files")


if __name__ == "__main__":
    unittest.main()
