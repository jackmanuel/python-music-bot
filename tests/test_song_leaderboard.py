import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src" / "music_bot"
sys.path.insert(0, str(SRC_DIR))

from database_manager import DatabaseManager
from stats_commands_mixin import StatsCommandsMixin


class SongLeaderboardDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "music_log.db"
        self.manager = DatabaseManager(str(self.db_path))

    def tearDown(self):
        self.temp_dir.cleanup()

    def _log(self, title, url, user_id=1, guild_id=10, status="completed"):
        request_id = self.manager.log_song_request(
            user_id=user_id,
            user_name=f"User {user_id}",
            guild_id=guild_id,
            query=title,
            resolved_title=title,
            resolved_url=url,
            channel_name="Test Channel",
            duration=180
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE play_history SET play_status = ? WHERE request_id = ?",
                (status, request_id)
            )
            conn.commit()
        finally:
            conn.close()

    def test_ranks_completed_plays(self):
        for _ in range(3):
            self._log("First Song", "https://example.com/first", user_id=1)
        for _ in range(2):
            self._log("Second Song", "https://example.com/second", user_id=2)
        self._log("First Song", "https://example.com/first", user_id=2)
        self._log("Skipped Song", "https://example.com/skipped", status="skipped")
        self._log("Queued Song", "https://example.com/queued", status="queued")
        self._log("Other Server", "https://example.com/other", guild_id=20)

        server_songs = self.manager.get_song_leaderboard(guild_id=10, limit=20)

        self.assertEqual(
            [(song['title'], song['play_count']) for song in server_songs],
            [("First Song", 4), ("Second Song", 2)]
        )

    def test_applies_limit(self):
        for index in range(25):
            self._log(f"Song {index}", f"https://example.com/{index}")

        self.assertEqual(len(self.manager.get_song_leaderboard(guild_id=10, limit=10)), 10)


class SongLeaderboardCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_requests_and_displays_top_ten(self):
        class Database:
            def __init__(self):
                self.arguments = None

            def get_song_leaderboard(self, **kwargs):
                self.arguments = kwargs
                return [
                    {
                        'title': "Favourite Song",
                        'url': "https://example.com",
                        'play_count': 3
                    },
                    {
                        'title': "Another Song",
                        'url': "https://example.org",
                        'play_count': 1
                    }
                ]

        class Context:
            def __init__(self):
                self.author = "Requester"
                self.guild = type(
                    "Guild",
                    (),
                    {
                        "id": 10,
                        "get_member": lambda self, user_id: (
                            type("Member", (), {"display_name": "Alice"})()
                            if user_id == 42 else None
                        )
                    }
                )()
                self.embeds = []

            async def send(self, message=None, *, embed=None):
                self.embeds.append(embed)

        database = Database()
        mixin = StatsCommandsMixin()
        mixin.db_manager = database
        ctx = Context()

        await StatsCommandsMixin.song_leaderboard.callback(mixin, ctx)

        self.assertEqual(
            database.arguments,
            {'guild_id': 10, 'limit': 10}
        )
        embed = ctx.embeds[0]
        self.assertEqual(embed.title, "🎵 Top 10 Songs")
        self.assertIn("Favourite Song", embed.description)
        self.assertIn("**3** plays", embed.description)
        self.assertIn("Another Song", embed.description)
        self.assertIn("**1** play", embed.description)
        self.assertEqual(embed.footer.text, "Counts completed plays only.")


if __name__ == "__main__":
    unittest.main()
