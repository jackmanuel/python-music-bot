import sqlite3
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

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


class CacheCleanupCommandTests(unittest.IsolatedAsyncioTestCase):
    class Context:
        def __init__(self):
            self.author = "Test User"
            self.guild = type("Guild", (), {"id": 10, "name": "Test Server"})()
            self.messages = []

        async def send(self, message):
            self.messages.append(message)

    class RecordingDatabase:
        def __init__(self):
            self.status_updates = []

        def update_play_status(self, request_id, status):
            self.status_updates.append((request_id, status))

    async def test_remove_requests_eviction_for_a_new_download(self):
        mixin = QueueCommandsMixin()
        removed_song = {
            "request_id": 101,
            "title": "Wrong Song",
            "created_cache_entry": True,
        }
        waiting = deque([removed_song])
        eviction_requests = []
        mixin.last_activity = {}
        mixin.db_manager = self.RecordingDatabase()
        mixin.get_queue = lambda guild_id: waiting
        mixin._request_cache_eviction = lambda song: eviction_requests.append(song) or "deleted"
        mixin._process_pending_cache_evictions = lambda: None
        ctx = self.Context()

        await QueueCommandsMixin.remove.callback(mixin, ctx, 1)

        self.assertEqual(eviction_requests, [removed_song])
        self.assertEqual(len(waiting), 0)
        self.assertIn("also removed from cache", ctx.messages[0])

    async def test_skip_within_thirty_seconds_requests_eviction(self):
        class VoiceClient:
            def __init__(self):
                self.stopped = False

            def is_connected(self):
                return True

            def is_playing(self):
                return True

            def is_paused(self):
                return False

            def stop(self):
                self.stopped = True

        mixin = VoiceCommandsMixin()
        current = {
            "request_id": 101,
            "title": "Wrong Song",
            "duration": 180,
            "start_time": 100,
            "created_cache_entry": True,
        }
        voice_client = VoiceClient()
        eviction_requests = []
        mixin.last_activity = {}
        mixin.voice_clients = {10: voice_client}
        mixin.current_song = {10: current}
        mixin.db_manager = self.RecordingDatabase()
        mixin._request_cache_eviction = lambda song: eviction_requests.append(song) or "deferred"
        ctx = self.Context()

        with patch("voice_commands_mixin.time.time", return_value=125):
            await VoiceCommandsMixin.skip.callback(mixin, ctx)

        self.assertEqual(eviction_requests, [current])
        self.assertTrue(voice_client.stopped)
        self.assertTrue(current["was_skipped"])
        self.assertIn("skipped early", ctx.messages[0])

    async def test_skip_after_thirty_seconds_keeps_the_cache_entry(self):
        class VoiceClient:
            def is_connected(self):
                return True

            def is_playing(self):
                return True

            def is_paused(self):
                return False

            def stop(self):
                pass

        mixin = VoiceCommandsMixin()
        current = {
            "request_id": 101,
            "title": "Correct Song",
            "duration": 180,
            "start_time": 100,
            "created_cache_entry": True,
        }
        eviction_requests = []
        mixin.last_activity = {}
        mixin.voice_clients = {10: VoiceClient()}
        mixin.current_song = {10: current}
        mixin.db_manager = self.RecordingDatabase()
        mixin._request_cache_eviction = lambda song: eviction_requests.append(song) or "deferred"
        ctx = self.Context()

        with patch("voice_commands_mixin.time.time", return_value=131):
            await VoiceCommandsMixin.skip.callback(mixin, ctx)

        self.assertEqual(eviction_requests, [])
        self.assertNotIn("skipped early", ctx.messages[0])


if __name__ == "__main__":
    unittest.main()
