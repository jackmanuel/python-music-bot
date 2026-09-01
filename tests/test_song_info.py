import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src" / "music_bot"
sys.path.insert(0, str(SRC_DIR))

from database_manager import DatabaseManager
from stats_commands_mixin import StatsCommandsMixin


class SongInfoDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "music_log.db"
        self.manager = DatabaseManager(str(self.db_path))
        self.url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _log(self, user_id, user_name, guild_id=10):
        return self.manager.log_song_request(
            user_id=user_id,
            user_name=user_name,
            guild_id=guild_id,
            query=self.url,
            resolved_title="Test Song",
            resolved_url=self.url,
            channel_name="Test Channel",
            duration=213
        )

    def test_song_info_has_server_counts_and_top_five_queue_leaderboard(self):
        request_statuses = []
        for _ in range(3):
            request_statuses.append((self._log(1, "Alice"), "completed"))
        request_statuses.append((self._log(1, "Alice"), "skipped"))
        for _ in range(3):
            request_statuses.append((self._log(2, "Bob"), "completed"))
        for user_id, name in [(3, "Carol"), (4, "Dan"), (5, "Eve"), (6, "Frank")]:
            request_statuses.append((self._log(user_id, name), "queued"))
        self._log(7, "Other Server", guild_id=20)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                "UPDATE play_history SET play_status = ? WHERE request_id = ?",
                [(status, request_id) for request_id, status in request_statuses]
            )
            conn.commit()
        finally:
            conn.close()

        info = self.manager.get_song_info(self.url, guild_id=10)

        self.assertEqual(info['title'], "Test Song")
        self.assertEqual(info['duration'], 213)
        self.assertEqual(info['completed_count'], 6)
        self.assertEqual(info['skipped_count'], 1)
        self.assertEqual(len(info['queue_leaderboard']), 5)
        self.assertEqual(info['queue_leaderboard'][0]['user_name'], "Alice")
        self.assertEqual(info['queue_leaderboard'][0]['queue_count'], 4)
        self.assertEqual(info['queue_leaderboard'][1]['user_name'], "Bob")
        self.assertEqual(info['queue_leaderboard'][1]['queue_count'], 3)

    def test_song_info_returns_none_without_server_history(self):
        self._log(1, "Other Server", guild_id=20)

        self.assertIsNone(self.manager.get_song_info(self.url, guild_id=10))


class SongInfoCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_uses_local_data_and_reports_cache_hit(self):
        class Database:
            def get_song_info(self, resolved_url, guild_id):
                return {
                    'title': "Test Song",
                    'duration': 213,
                    'first_queued_at': "2024-03-01T12:00:00+00:00",
                    'first_queued_by': "Alice",
                    'completed_count': 6,
                    'skipped_count': 1,
                    'queue_leaderboard': [
                        {'user_id': '1', 'user_name': 'Alice', 'queue_count': 4}
                    ]
                }

        class Cache:
            def __init__(self, path):
                self.path = path

            def get(self, youtube_id):
                return str(self.path) if youtube_id == "dQw4w9WgXcQ" else None

        class Context:
            def __init__(self):
                self.guild = type(
                    "Guild",
                    (),
                    {"id": 10, "get_member": lambda self, user_id: None}
                )()
                self.embeds = []

            async def send(self, message=None, *, embed=None):
                self.embeds.append(embed)

        with tempfile.TemporaryDirectory() as temp_dir:
            cached_file = Path(temp_dir) / "youtube-dQw4w9WgXcQ.opus"
            cached_file.touch()
            mixin = StatsCommandsMixin()
            mixin.db_manager = Database()
            mixin.song_cache = Cache(cached_file)
            mixin._format_duration = lambda duration: "03:33"
            ctx = Context()

            with patch("yt_dlp.YoutubeDL.YoutubeDL.extract_info") as extract_info:
                await StatsCommandsMixin.songinfo.callback(
                    mixin,
                    ctx,
                    "https://youtu.be/dQw4w9WgXcQ"
                )

            extract_info.assert_not_called()

        embed = ctx.embeds[0]
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(embed.title, "Test Song")
        self.assertEqual(fields['Length'], "03:33")
        self.assertEqual(fields['Cached'], "Yes")
        self.assertEqual(fields['Completed plays'], "6")
        self.assertEqual(fields['Skipped'], "1")
        self.assertIn("Alice", fields['Queue leaderboard'])


if __name__ == "__main__":
    unittest.main()
