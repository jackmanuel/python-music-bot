from collections import deque
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src" / "music_bot"
sys.path.insert(0, str(SRC_DIR))

from audio_normalization import (
    build_stream_loudnorm_filter,
    classify_loudnorm_stats,
    parse_loudnorm_stats,
)
from playback_mixin import PlaybackMixin
from youtube import FFMPEG_OPTIONS, LoudnormOpusPostProcessor


class AudioNormalizationUnitTests(unittest.TestCase):
    def test_parse_and_classify_normal_stats(self):
        stderr = """
        [Parsed_loudnorm_0 @ 0x1234]
        {
            "input_i" : "-12.34",
            "input_tp" : "-1.20",
            "input_lra" : "6.50",
            "input_thresh" : "-22.40",
            "target_offset" : "0.15"
        }
        """
        stats = parse_loudnorm_stats(stderr)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["input_i"], "-12.34")
        self.assertEqual(classify_loudnorm_stats(stats), "linear")

    def test_classify_out_of_range_loudness_falls_back_to_single_pass(self):
        stats = {
            "input_i": "0.25",
            "input_tp": "1.20",
            "input_lra": "3.00",
            "input_thresh": "-9.50",
            "target_offset": "0.00",
        }
        self.assertEqual(classify_loudnorm_stats(stats), "single_pass")

    def test_classify_silent_track(self):
        stats = {
            "input_i": "-inf",
            "input_tp": "-inf",
            "input_lra": "0.00",
            "input_thresh": "-inf",
            "target_offset": "inf",
        }
        self.assertEqual(classify_loudnorm_stats(stats), "silent")

    def test_stream_filter_string(self):
        self.assertEqual(
            build_stream_loudnorm_filter(target_lufs=-14.0, true_peak=-1.5, lra=11.0),
            "loudnorm=I=-14.0:TP=-1.5:LRA=11.0",
        )
        self.assertIn("-af loudnorm=", FFMPEG_OPTIONS["options"])

    def test_postprocessor_replaces_webm_with_normalized_opus(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "youtube-abc12345678.webm"
            src.write_bytes(b"raw-webm")

            def fake_normalize(source, dest, **_kwargs):
                Path(dest).write_bytes(b"normalized-opus")
                return True, "linear"

            pp = LoudnormOpusPostProcessor()
            info = {"filepath": str(src), "ext": "webm"}
            with patch("youtube.normalize_audio_file", side_effect=fake_normalize):
                _, updated = pp.run(info)

            expected_dst = Path(temp_dir) / "youtube-abc12345678.opus"
            self.assertEqual(updated["filepath"], str(expected_dst))
            self.assertEqual(updated["ext"], "opus")
            self.assertTrue(expected_dst.exists())
            self.assertFalse(src.exists())

    def test_postprocessor_keeps_original_file_on_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "youtube-abc12345678.webm"
            src.write_bytes(b"raw-webm")

            pp = LoudnormOpusPostProcessor()
            info = {"filepath": str(src), "ext": "webm"}
            with patch("youtube.normalize_audio_file", return_value=(False, "ffmpeg error")):
                _, updated = pp.run(info)

            self.assertEqual(updated["filepath"], str(src))
            self.assertEqual(updated["ext"], "webm")
            self.assertTrue(src.exists())


class PlaybackAudioSourceTests(unittest.IsolatedAsyncioTestCase):
    def _make_mixin(self):
        mixin = PlaybackMixin()
        mixin.queues = {}
        mixin.current_song = {}
        mixin.last_activity = {}
        mixin.pending_cache_evictions = {}
        mixin.get_queue = lambda guild_id: mixin.queues.setdefault(guild_id, deque())
        vc = MagicMock()
        vc.is_connected.return_value = True
        mixin.voice_clients = {10: vc}
        return mixin, vc

    async def test_cached_song_uses_from_probe_opus_passthrough(self):
        mixin, vc = self._make_mixin()
        mixin.queues[10] = deque([
            {"title": "Cached Song", "url": "/cache/youtube-abc.opus", "is_cached": True}
        ])
        fake_source = object()

        with (
            patch("playback_mixin.discord.FFmpegOpusAudio.from_probe", new=AsyncMock(return_value=fake_source)) as mock_probe,
            patch("playback_mixin.discord.FFmpegOpusAudio") as mock_direct,
        ):
            mock_direct.from_probe = mock_probe
            await mixin._play_next_async(10)

        mock_probe.assert_awaited_once()
        self.assertEqual(mock_probe.await_args.args[0], "/cache/youtube-abc.opus")
        self.assertNotIn("options", mock_probe.await_args.kwargs)
        vc.play.assert_called_once()
        self.assertIs(vc.play.call_args.args[0], fake_source)

    async def test_uncached_stream_uses_direct_ffmpeg_opus_audio_with_loudnorm(self):
        mixin, vc = self._make_mixin()
        mixin.queues[10] = deque([
            {"title": "Stream Song", "url": "https://example.com/stream", "is_cached": False}
        ])
        fake_source = object()

        with (
            patch("playback_mixin.LOUDNORM_ENABLED", True),
            patch("playback_mixin.discord.FFmpegOpusAudio", return_value=fake_source) as mock_cls,
        ):
            mock_cls.from_probe = AsyncMock()
            await mixin._play_next_async(10)

        mock_cls.from_probe.assert_not_awaited()
        mock_cls.assert_called_once_with("https://example.com/stream", **FFMPEG_OPTIONS)
        vc.play.assert_called_once()
        self.assertIs(vc.play.call_args.args[0], fake_source)


if __name__ == "__main__":
    unittest.main()
