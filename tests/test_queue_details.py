import sqlite3
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src" / "music_bot"
sys.path.insert(0, str(SRC_DIR))

from database_manager import DatabaseManager
from queue_commands_mixin import QueueCommandsMixin
from voice_commands_mixin import VoiceCommandsMixin


class QueueWaitEstimateTests(unittest.TestCase):
    def test_estimate_includes_current_remainder_and_songs_ahead(self):
        current = {"duration": 300, "start_time": 100}
        songs_ahead = [{"duration": 120}, {"duration": 60}]

        estimate = VoiceCommandsMixin._estimate_queue_wait_seconds(
            current, songs_ahead, now=160
        )

        self.assertEqual(estimate, 420)

    def test_estimate_is_unavailable_when_a_duration_is_unknown(self):
        estimate = VoiceCommandsMixin._estimate_queue_wait_seconds(
            {"duration": 300, "start_time": 100},
            [{"duration": None}],
            now=160
        )

        self.assertIsNone(estimate)


class UrlPlayStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "music_log.db"
        self.manager = DatabaseManager(str(self.db_path))

    def tearDown(self):
        self.temp_dir.cleanup()

    def _log_request(self, user_name, resolved_url, guild_id=10):
        return self.manager.log_song_request(
            user_id=1,
            user_name=user_name,
            guild_id=guild_id,
            query=resolved_url,
            resolved_title="Test Song",
            resolved_url=resolved_url,
            channel_name="Test Channel",
            duration=180
        )

    def test_stats_are_server_scoped_and_count_only_completed_plays(self):
        url = "https://www.youtube.com/watch?v=abc123"
        first_id = self._log_request("First User", url)
        skipped_id = self._log_request("Second User", url)
        self._log_request("Still Queued User", url)
        other_guild_id = self._log_request("Other Server User", url, guild_id=20)
        self._log_request("Other Song User", "https://www.youtube.com/watch?v=xyz789")

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE play_history SET request_timestamp = ? WHERE request_id = ?",
                ("2024-03-01T12:00:00+00:00", first_id)
            )
            conn.execute(
                "UPDATE play_history SET play_status = 'completed' WHERE request_id = ?",
                (first_id,)
            )
            conn.execute(
                "UPDATE play_history SET play_status = 'skipped' WHERE request_id = ?",
                (skipped_id,)
            )
            conn.execute(
                "UPDATE play_history SET request_timestamp = ? WHERE request_id = ?",
                ("2020-01-01T12:00:00+00:00", other_guild_id)
            )
            conn.commit()
        finally:
            conn.close()

        stats = self.manager.get_url_play_stats(url, guild_id=10)

        self.assertEqual(stats['first_queued_at'], "2024-03-01T12:00:00+00:00")
        self.assertEqual(stats['first_queued_by'], "First User")
        self.assertEqual(stats['play_count'], 1)

    def test_missing_url_has_no_stats(self):
        self.assertIsNone(self.manager.get_url_play_stats(None, guild_id=10))


class QueueClearTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_marks_waiting_songs_skipped_without_touching_current_song(self):
        class RecordingDatabase:
            def __init__(self):
                self.status_updates = []

            def update_play_status(self, request_id, status):
                self.status_updates.append((request_id, status))

        class Context:
            def __init__(self):
                self.author = "Test User"
                self.guild = type("Guild", (), {"id": 10, "name": "Test Server"})()
                self.messages = []

            async def send(self, message):
                self.messages.append(message)

        mixin = QueueCommandsMixin()
        waiting = deque([
            {"request_id": 101, "title": "First"},
            {"request_id": 102, "title": "Second"}
        ])
        current = {"request_id": 100, "title": "Current"}
        mixin.last_activity = {}
        mixin.current_song = {10: current}
        mixin.db_manager = RecordingDatabase()
        mixin.get_queue = lambda guild_id: waiting
        ctx = Context()

        await QueueCommandsMixin.clear.callback(mixin, ctx)

        self.assertEqual(
            mixin.db_manager.status_updates,
            [(101, 'skipped'), (102, 'skipped')]
        )
        self.assertEqual(len(waiting), 0)
        self.assertIs(mixin.current_song[10], current)
        self.assertEqual(ctx.messages, ["Song queue cleared!"])


if __name__ == "__main__":
    unittest.main()
