import asyncio
import concurrent.futures
from collections import deque
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src" / "music_bot"
sys.path.insert(0, str(SRC_DIR))

import audio_normalization
from database_manager import DatabaseManager
from playback_mixin import PlaybackMixin
from song_cache import SongCache
from voice_commands_mixin import VoiceCommandsMixin


class _TestBotMixin(PlaybackMixin, VoiceCommandsMixin):
    pass


class BackgroundCachingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.temp_dir.name) / "song_cache"
        self.db_path = Path(self.temp_dir.name) / "music_log.db"
        self.mixin = _TestBotMixin()
        self.mixin.song_cache = SongCache(self.cache_dir)
        self.mixin.db_manager = DatabaseManager(str(self.db_path))
        self.mixin.queues = {}
        self.mixin.current_song = {}
        self.mixin.voice_clients = {}
        self.mixin.last_activity = {}
        self.mixin.pending_cache_evictions = {}
        self.mixin.background_cache_tasks = {}
        self.mixin.cancelled_background_caches = set()
        self.mixin.is_shutting_down = False
        self.mixin.cache_downloads_enabled = True
        self.mixin.get_queue = lambda guild_id: self.mixin.queues.setdefault(guild_id, deque())
        self.mixin._format_duration = lambda s: "03:30"
        self.mixin.background_cache_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.mixin.thread_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    async def asyncTearDown(self):
        for task in list(self.mixin.background_cache_tasks.values()):
            if not task.done():
                task.cancel()
        self.mixin.background_cache_executor.shutdown(wait=True)
        self.mixin.thread_executor.shutdown(wait=True)
        self.temp_dir.cleanup()

    async def test_background_cache_downloads_at_low_priority_and_upgrades_queued_song(self):
        song_info = {
            "title": "Queued Song",
            "url": "https://example.com/stream-url",
            "webpage_url": "https://www.youtube.com/watch?v=abc12345678",
            "channel": "Test Artist",
            "duration": 200,
            "youtube_id": "abc12345678",
            "is_cached": False,
            "was_previously_cached": False,
            "created_cache_entry": False,
            "estimated_size_bytes": 1024,
            "search_query_normalized": "queued song",
        }
        self.mixin.queues[10] = deque([song_info])

        seen_kwargs = {}

        def fake_extractor(query, download=False, **kwargs):
            seen_kwargs.update(kwargs)
            self.assertTrue(download)
            dst = self.cache_dir / "youtube-abc12345678.opus"
            dst.write_bytes(b"normalized-opus-data")
            return {
                "id": "abc12345678",
                "title": "Queued Song",
                "ext": "opus",
                "extractor": "youtube",
            }

        with (
            patch("playback_mixin.SONG_CACHE_DIR", self.cache_dir),
            patch("playback_mixin.run_yt_dlp_extractor", side_effect=fake_extractor),
        ):
            task = self.mixin._schedule_background_cache(song_info, song_info["webpage_url"])
            self.assertIsNotNone(task)
            # Scheduling a duplicate while in-flight reuses the existing task.
            self.assertIs(
                self.mixin._schedule_background_cache(song_info, song_info["webpage_url"]),
                task,
            )
            await task

        self.assertEqual(seen_kwargs, {"low_priority": True, "threads": 1})
        self.assertTrue(song_info["created_cache_entry"])
        self.assertFalse(song_info["background_caching"])
        self.assertIsNotNone(self.mixin.db_manager.get_cached_song("abc12345678"))

        # Now when _play_next_async pops this queued song, it should upgrade to the cached .opus file.
        vc = MagicMock()
        vc.is_connected.return_value = True
        self.mixin.voice_clients[10] = vc
        fake_source = object()

        with (
            patch("playback_mixin.discord.FFmpegOpusAudio.from_probe", new=AsyncMock(return_value=fake_source)) as mock_probe,
            patch("playback_mixin.discord.FFmpegOpusAudio") as mock_direct,
        ):
            mock_direct.from_probe = mock_probe
            await self.mixin._play_next_async(10)

        mock_probe.assert_awaited_once()
        self.assertEqual(
            mock_probe.await_args.args[0],
            str(self.cache_dir / "youtube-abc12345678.opus"),
        )
        self.assertTrue(self.mixin.current_song[10]["is_cached"])
        vc.play.assert_called_once()

    async def test_cancelled_background_cache_discards_downloaded_file(self):
        song_info = {
            "title": "Skipped Song",
            "url": "https://example.com/stream-url",
            "webpage_url": "https://www.youtube.com/watch?v=skip1234567",
            "channel": "Test Artist",
            "duration": 180,
            "youtube_id": "skip1234567",
            "is_cached": False,
            "created_cache_entry": False,
            "estimated_size_bytes": 512,
        }

        def fake_extractor(query, download=False, **kwargs):
            dst = self.cache_dir / "youtube-skip1234567.opus"
            dst.write_bytes(b"temporary-opus")
            return {
                "id": "skip1234567",
                "title": "Skipped Song",
                "ext": "opus",
                "extractor": "youtube",
            }

        with (
            patch("playback_mixin.SONG_CACHE_DIR", self.cache_dir),
            patch("playback_mixin.run_yt_dlp_extractor", side_effect=fake_extractor),
        ):
            task = self.mixin._schedule_background_cache(song_info, song_info["webpage_url"])
            status = self.mixin._request_cache_eviction(song_info)
            self.assertEqual(status, "cancelled")
            await task

        self.assertIsNone(self.mixin.song_cache.get("skip1234567"))
        self.assertFalse((self.cache_dir / "youtube-skip1234567.opus").exists())

    async def test_background_cache_records_query_if_song_already_completed(self):
        song_info = {
            "title": "Short Song",
            "url": "https://example.com/stream-url",
            "webpage_url": "https://www.youtube.com/watch?v=done1234567",
            "channel": "Test Artist",
            "duration": 30,
            "youtube_id": "done1234567",
            "is_cached": False,
            "created_cache_entry": False,
            "completed_playback": True,
            "estimated_size_bytes": 512,
            "search_query_normalized": "short song",
        }

        def fake_extractor(query, download=False, **kwargs):
            dst = self.cache_dir / "youtube-done1234567.opus"
            dst.write_bytes(b"opus-bytes")
            return {
                "id": "done1234567",
                "title": "Short Song",
                "ext": "opus",
                "extractor": "youtube",
            }

        with (
            patch("playback_mixin.SONG_CACHE_DIR", self.cache_dir),
            patch("playback_mixin.run_yt_dlp_extractor", side_effect=fake_extractor),
        ):
            task = self.mixin._schedule_background_cache(song_info, song_info["webpage_url"])
            await task

        self.assertEqual(self.mixin.db_manager.get_cached_query("short song"), "done1234567")

    async def test_play_query_streams_immediately_and_schedules_background_cache(self):
        vc = MagicMock()
        vc.is_connected.return_value = True
        vc.is_playing.return_value = False
        vc.is_paused.return_value = False
        channel = MagicMock()
        vc.channel = channel
        self.mixin.voice_clients[10] = vc

        ctx = MagicMock()
        ctx.guild.id = 10
        ctx.guild.name = "Test Guild"
        ctx.guild.voice_client = vc
        ctx.author = MagicMock()
        ctx.author.id = 42
        ctx.author.voice.channel = channel
        ctx.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))

        stream_info = {
            "title": "Stream First Song",
            "url": "https://example.com/direct-stream",
            "webpage_url": "https://www.youtube.com/watch?v=strm1234567",
            "channel": "Artist",
            "duration": 210,
            "youtube_id": "strm1234567",
            "is_cached": False,
            "was_previously_cached": False,
            "created_cache_entry": False,
            "exceeds_cache_duration": False,
            "estimated_size_bytes": 1000,
        }

        fake_source = object()
        with (
            patch.object(self.mixin, "_extract_info", new=AsyncMock(return_value=stream_info)) as mock_extract,
            patch.object(self.mixin, "_schedule_background_cache") as mock_bg,
            patch("voice_commands_mixin.YtDlpPipeOpusAudio", return_value=fake_source) as mock_pipe,
        ):
            await self.mixin._play_query(ctx, "stream first song")

        # Must only call _extract_info once (download=False), never blocking on download=True.
        mock_extract.assert_awaited_once_with("stream first song", download=False)
        mock_bg.assert_called_once_with(stream_info, "https://www.youtube.com/watch?v=strm1234567")
        mock_pipe.assert_called_once()
        vc.play.assert_called_once_with(fake_source, after=unittest.mock.ANY)


class LowPriorityNormalizationTests(unittest.TestCase):
    def test_low_priority_sets_single_thread_and_preexec_fn(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "input.webm"
            dst = Path(temp_dir) / "output.opus"
            src.write_bytes(b"fake-audio")

            calls = []

            def fake_run(cmd, **kwargs):
                calls.append((cmd, kwargs))
                if len(calls) == 1:
                    res = MagicMock()
                    res.returncode = 0
                    res.stderr = (
                        '{"input_i":"-15.0","input_tp":"-2.0","input_lra":"6.0",'
                        '"input_thresh":"-25.0","target_offset":"0.0"}'
                    )
                    return res
                # Write temp output file expected by pass2
                Path(cmd[-1]).write_bytes(b"normalized-opus")
                res = MagicMock()
                res.returncode = 0
                res.stderr = ""
                return res

            with patch.object(audio_normalization.subprocess, "run", side_effect=fake_run):
                ok, mode = audio_normalization.normalize_audio_file(
                    src, dst, low_priority=True
                )

            self.assertTrue(ok)
            self.assertEqual(mode, "linear")
            self.assertEqual(len(calls), 2)
            for cmd, kwargs in calls:
                self.assertIn("-threads", cmd)
                idx = cmd.index("-threads")
                self.assertEqual(cmd[idx + 1], "1")
                if os.name == "posix":
                    self.assertIs(kwargs.get("preexec_fn"), audio_normalization._low_priority_preexec)


if __name__ == "__main__":
    unittest.main()
