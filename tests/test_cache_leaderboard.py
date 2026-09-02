import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src" / "music_bot"
sys.path.insert(0, str(SRC_DIR))

from cache_commands_mixin import CacheCommandsMixin, youtube_id_from_cache_path
from database_manager import DatabaseManager


class CacheLeaderboardDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "music_log.db"
        self.manager = DatabaseManager(str(self.db_path))

    def tearDown(self):
        self.temp_dir.cleanup()

    def _log(self, youtube_id, user_id, user_name, guild_id=10):
        return self.manager.log_song_request(
            user_id=user_id,
            user_name=user_name,
            guild_id=guild_id,
            query=youtube_id,
            resolved_title="Test Song",
            resolved_url=f"https://www.youtube.com/watch?v={youtube_id}",
            channel_name="Test Channel",
            duration=180
        )

    def test_returns_the_earliest_requester_for_each_cached_id(self):
        self._log("dQw4w9WgXcQ", 1, "Alice")
        self._log("dQw4w9WgXcQ", 2, "Bob")
        self._log("abcdefghijk", 2, "Bob")
        self._log("otherguild1", 3, "Carol", guild_id=20)

        requesters = self.manager.get_first_youtube_requesters(
            {"dQw4w9WgXcQ", "abcdefghijk", "otherguild1", "notindb0000"},
            guild_id=10
        )

        self.assertEqual(requesters["dQw4w9WgXcQ"]['user_name'], "Alice")
        self.assertEqual(requesters["abcdefghijk"]['user_name'], "Bob")
        self.assertNotIn("otherguild1", requesters)
        self.assertNotIn("notindb0000", requesters)


class CacheLeaderboardCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_sums_matched_cache_file_sizes_by_requester(self):
        class Cache:
            def __init__(self, paths):
                self.paths = paths

            def values(self):
                return self.paths

        class Database:
            def get_first_youtube_requesters(self, youtube_ids, guild_id):
                self.youtube_ids = youtube_ids
                self.guild_id = guild_id
                return {
                    "dQw4w9WgXcQ": {'user_id': 1, 'user_name': "Alice"},
                    "abcdefghijk": {'user_id': 1, 'user_name': "Alice"}
                }

        class Context:
            def __init__(self):
                self.author = "Requester"
                self.guild = type(
                    "Guild",
                    (),
                    {"id": 10, "get_member": lambda self, user_id: None}
                )()
                self.embeds = []

            async def send(self, message=None, *, embed=None):
                self.embeds.append(embed)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "youtube-dQw4w9WgXcQ.opus"
            second = root / "youtube-abcdefghijk.webm"
            unmatched = root / "youtube-notindb0000.opus"
            other = root / "soundcloud-track.opus"
            first.write_bytes(b"a" * 1024 * 1024)
            second.write_bytes(b"b" * 2 * 1024 * 1024)
            unmatched.write_bytes(b"c" * 4 * 1024 * 1024)
            other.write_bytes(b"d" * 8 * 1024 * 1024)

            database = Database()
            mixin = CacheCommandsMixin()
            mixin.song_cache = Cache([str(first), str(second), str(unmatched), str(other)])
            mixin.db_manager = database
            mixin.last_activity = {}
            ctx = Context()

            await CacheCommandsMixin.cache_leaderboard.callback(mixin, ctx)

        self.assertEqual(
            database.youtube_ids,
            {"dQw4w9WgXcQ", "abcdefghijk", "notindb0000"}
        )
        self.assertEqual(database.guild_id, 10)
        embed = ctx.embeds[0]
        self.assertEqual(embed.title, "📁 Cache Leaderboard")
        self.assertIn("Alice", embed.description)
        self.assertIn("**3.00 MB** (2 files)", embed.description)
        self.assertNotIn("12.00 MB", embed.description)
        self.assertIn("in this server", embed.footer.text)

    def test_extracts_only_youtube_cache_filenames(self):
        self.assertEqual(
            youtube_id_from_cache_path("cache/youtube-dQw4w9WgXcQ.opus"),
            "dQw4w9WgXcQ"
        )
        self.assertIsNone(youtube_id_from_cache_path("cache/soundcloud-track.opus"))
        self.assertIsNone(youtube_id_from_cache_path("cache/youtube-dQw4w9WgXcQ.part"))


if __name__ == "__main__":
    unittest.main()
