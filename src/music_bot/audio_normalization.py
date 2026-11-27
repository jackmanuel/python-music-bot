import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from config import (
    CACHE_OPUS_BITRATE,
    FFMPEG_EXECUTABLE,
    LOUDNORM_LRA,
    LOUDNORM_TARGET_LUFS,
    LOUDNORM_TRUE_PEAK,
)

logger = logging.getLogger(__name__)

LOUDNORM_JSON_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)

_MEASURED_RANGES = {
    "input_i": (-99.0, 0.0),
    "input_tp": (-99.0, 0.0),
    "input_lra": (0.0, 99.0),
    "input_thresh": (-99.0, 0.0),
    "target_offset": (-99.0, 99.0),
}


def build_stream_loudnorm_filter(
    target_lufs: float = LOUDNORM_TARGET_LUFS,
    true_peak: float = LOUDNORM_TRUE_PEAK,
    lra: float = LOUDNORM_LRA,
) -> str:
    """Return the FFmpeg single-pass loudnorm filter string for live streams."""
    return f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={lra}"


def parse_loudnorm_stats(stderr_text: str) -> dict[str, str] | None:
    """Extract the JSON stats block emitted by FFmpeg's loudnorm filter."""
    matches = LOUDNORM_JSON_RE.findall(stderr_text)
    if not matches:
        return None
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError:
        return None


def _parse_finite_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def classify_loudnorm_stats(stats: dict[str, str] | None) -> str:
    """Classify pass-1 stats as 'linear', 'single_pass', or 'silent'."""
    if not stats:
        return "single_pass"

    parsed: dict[str, float] = {}
    for key in _MEASURED_RANGES:
        val = _parse_finite_float(stats.get(key))
        if val is None:
            return "silent"
        parsed[key] = val

    for key, (low, high) in _MEASURED_RANGES.items():
        if not (low <= parsed[key] <= high):
            return "single_pass"

    return "linear"


def _low_priority_preexec() -> None:
    """Lower subprocess CPU scheduling priority on POSIX systems."""
    try:
        os.nice(19)
    except OSError:
        pass


def normalize_audio_file(
    source: Path | str,
    dest: Path | str,
    *,
    ffmpeg_executable: str = FFMPEG_EXECUTABLE,
    target_lufs: float = LOUDNORM_TARGET_LUFS,
    true_peak: float = LOUDNORM_TRUE_PEAK,
    lra: float = LOUDNORM_LRA,
    bitrate: str = CACHE_OPUS_BITRATE,
    threads: int | None = None,
    low_priority: bool = False,
) -> tuple[bool, str]:
    """Normalize an audio file to 48 kHz stereo Opus using two-pass EBU R128 loudnorm.

    Writes atomically to a hidden temp file in ``dest.parent`` before replacing ``dest``.
    Returns ``(ok, detail)`` where ``detail`` describes the mode used or the error.
    """
    source_path = Path(source)
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    temp_dest = (
        dest_path.parent
        / f".{dest_path.stem}.tmp.{os.getpid()}.{threading.get_ident()}.opus"
    )
    effective_threads = 1 if (low_priority and threads is None) else threads
    thread_args = ["-threads", str(effective_threads)] if effective_threads is not None else []
    preexec_fn = _low_priority_preexec if (low_priority and os.name == "posix") else None

    try:
        pass1_filter = f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={lra}:print_format=json"
        pass1_cmd = [
            ffmpeg_executable,
            "-hide_banner",
            "-nostats",
            *thread_args,
            "-i",
            str(source_path),
            "-vn",
            "-af",
            pass1_filter,
            "-f",
            "null",
            "-",
        ]
        pass1 = subprocess.run(
            pass1_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            preexec_fn=preexec_fn,
        )
        if pass1.returncode != 0:
            return False, f"pass1 exit {pass1.returncode}: {pass1.stderr.strip()[-300:]}"

        stats = parse_loudnorm_stats(pass1.stderr)
        mode = classify_loudnorm_stats(stats)

        if mode == "silent":
            if source_path.suffix.lower() == ".opus":
                shutil.copy2(source_path, temp_dest)
            else:
                fallback_cmd = [
                    ffmpeg_executable,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    *thread_args,
                    "-i",
                    str(source_path),
                    "-vn",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-c:a",
                    "libopus",
                    "-b:a",
                    bitrate,
                    str(temp_dest),
                ]
                fb = subprocess.run(
                    fallback_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    preexec_fn=preexec_fn,
                )
                if fb.returncode != 0 or not temp_dest.exists() or temp_dest.stat().st_size == 0:
                    return False, f"silent fallback exit {fb.returncode}: {fb.stderr.strip()[-300:]}"
            os.replace(temp_dest, dest_path)
            return True, "copied_silent"

        if mode == "linear" and stats is not None:
            pass2_filter = (
                f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={lra}"
                f":measured_I={stats['input_i']}"
                f":measured_TP={stats['input_tp']}"
                f":measured_LRA={stats['input_lra']}"
                f":measured_thresh={stats['input_thresh']}"
                f":offset={stats['target_offset']}"
                f":linear=true"
            )
        else:
            pass2_filter = build_stream_loudnorm_filter(
                target_lufs=target_lufs,
                true_peak=true_peak,
                lra=lra,
            )

        pass2_cmd = [
            ffmpeg_executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *thread_args,
            "-i",
            str(source_path),
            "-vn",
            "-af",
            pass2_filter,
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "libopus",
            "-b:a",
            bitrate,
            str(temp_dest),
        ]
        pass2 = subprocess.run(
            pass2_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            preexec_fn=preexec_fn,
        )
        if pass2.returncode != 0 or not temp_dest.exists() or temp_dest.stat().st_size == 0:
            return False, f"pass2 ({mode}) exit {pass2.returncode}: {pass2.stderr.strip()[-300:]}"

        os.replace(temp_dest, dest_path)
        return True, mode
    except Exception as exc:
        return False, str(exc)
    finally:
        if temp_dest.exists():
            try:
                temp_dest.unlink()
            except OSError:
                pass
