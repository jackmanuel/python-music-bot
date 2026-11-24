#!/usr/bin/env python3
"""Multithreaded two-pass EBU R128 loudness normalization for song_cache files.

Reads audio files from an input directory, measures integrated loudness (pass 1),
applies linear loudness normalization and encodes to 48 kHz stereo Opus (pass 2),
and writes atomically into an output directory.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

VALID_EXTENSIONS = (".opus", ".webm", ".m4a", ".mp3", ".aac", ".wav")
EXT_PRIORITY = {ext: idx for idx, ext in enumerate(VALID_EXTENSIONS)}
LOUDNORM_JSON_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)


@dataclass
class FileResult:
    source: Path
    dest: Path
    status: str  # "normalized", "copied_silent", "skipped", "failed"
    input_i: float | None = None
    error: str | None = None


def collect_input_files(input_dir: Path) -> tuple[list[Path], list[tuple[Path, Path]]]:
    """Collect valid audio files, deduplicating by stem (e.g. .opus vs .webm)."""
    chosen: dict[str, Path] = {}
    duplicates: list[tuple[Path, Path]] = []

    for entry in sorted(input_dir.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_file():
            continue
        ext = entry.suffix.lower()
        if ext not in EXT_PRIORITY:
            continue

        stem = entry.stem
        if stem not in chosen:
            chosen[stem] = entry
            continue

        existing = chosen[stem]
        existing_prio = EXT_PRIORITY[existing.suffix.lower()]
        new_prio = EXT_PRIORITY[ext]
        if new_prio < existing_prio or (
            new_prio == existing_prio and entry.stat().st_size > existing.stat().st_size
        ):
            duplicates.append((existing, entry))
            chosen[stem] = entry
        else:
            duplicates.append((entry, existing))

    return list(chosen.values()), duplicates


def parse_loudnorm_stats(stderr_text: str) -> dict[str, str] | None:
    """Extract the JSON stats block emitted by ffmpeg's loudnorm filter."""
    matches = LOUDNORM_JSON_RE.findall(stderr_text)
    if not matches:
        return None
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError:
        return None


def _is_finite_number(value: str | None) -> bool:
    if value is None:
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def normalize_one(
    source: Path,
    output_dir: Path,
    ffmpeg_bin: str,
    target_lufs: float,
    true_peak: float,
    lra: float,
    bitrate: str,
    skip_existing: bool,
) -> FileResult:
    dest = output_dir / f"{source.stem}.opus"
    if skip_existing and dest.exists() and dest.stat().st_size > 0:
        return FileResult(source=source, dest=dest, status="skipped")

    temp_dest = output_dir / f".{source.stem}.tmp.{os.getpid()}.opus"
    try:
        # Pass 1: Measure loudness across the entire track.
        pass1_filter = (
            f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={lra}:print_format=json"
        )
        pass1_cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-nostats",
            "-threads",
            "1",
            "-i",
            str(source),
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
        )
        if pass1.returncode != 0:
            return FileResult(
                source=source,
                dest=dest,
                status="failed",
                error=f"pass1 exit {pass1.returncode}: {pass1.stderr.strip()[-300:]}",
            )

        stats = parse_loudnorm_stats(pass1.stderr)
        if not stats:
            return FileResult(
                source=source,
                dest=dest,
                status="failed",
                error="pass1 did not emit loudnorm JSON stats",
            )

        required_ranges = {
            "input_i": (-99.0, 0.0),
            "input_tp": (-99.0, 0.0),
            "input_lra": (0.0, 99.0),
            "input_thresh": (-99.0, 0.0),
            "target_offset": (-99.0, 99.0),
        }
        if not all(_is_finite_number(stats.get(k)) for k in required_ranges):
            # Track is silent or has non-finite loudness; copy if already opus, else transcode without loudnorm.
            if source.suffix.lower() == ".opus":
                shutil.copy2(source, temp_dest)
            else:
                fallback_cmd = [
                    ffmpeg_bin,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-threads",
                    "1",
                    "-i",
                    str(source),
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
                )
                if fb.returncode != 0:
                    return FileResult(
                        source=source,
                        dest=dest,
                        status="failed",
                        error=f"silent fallback exit {fb.returncode}: {fb.stderr.strip()[-300:]}",
                    )
            os.replace(temp_dest, dest)
            return FileResult(source=source, dest=dest, status="copied_silent")

        in_range = all(
            low <= float(stats[k]) <= high
            for k, (low, high) in required_ranges.items()
        )

        # Pass 2: Apply linear loudnorm when stats are in FFmpeg's valid range, else single-pass loudnorm.
        if in_range:
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
            pass2_filter = f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={lra}"
        pass2_cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-threads",
            "1",
            "-i",
            str(source),
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
        )
        if pass2.returncode != 0 or not temp_dest.exists() or temp_dest.stat().st_size == 0:
            return FileResult(
                source=source,
                dest=dest,
                status="failed",
                error=f"pass2 exit {pass2.returncode}: {pass2.stderr.strip()[-300:]}",
            )

        os.replace(temp_dest, dest)
        return FileResult(
            source=source,
            dest=dest,
            status="normalized",
            input_i=float(stats["input_i"]),
        )
    except Exception as exc:
        return FileResult(source=source, dest=dest, status="failed", error=str(exc))
    finally:
        if temp_dest.exists():
            try:
                temp_dest.unlink()
            except OSError:
                pass


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Multithreaded two-pass EBU R128 loudness normalization for song_cache."
    )
    parser.add_argument("input_dir", type=Path, help="Directory containing source cache files.")
    parser.add_argument("output_dir", type=Path, help="Directory to write normalized .opus files.")
    parser.add_argument("--target-lufs", type=float, default=-14.0, help="Target integrated loudness in LUFS (default: -14.0).")
    parser.add_argument("--true-peak", type=float, default=-1.5, help="Max true peak in dBTP (default: -1.5).")
    parser.add_argument("--lra", type=float, default=11.0, help="Target loudness range in LU (default: 11.0).")
    parser.add_argument("--bitrate", default="128k", help="Output Opus bitrate (default: 128k).")
    parser.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 4,
        help="Number of parallel FFmpeg workers (default: logical CPU count).",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg", help="Path to ffmpeg executable (default: ffmpeg).")
    parser.add_argument("--no-skip-existing", action="store_true", help="Re-process files even if they already exist in output_dir.")
    args = parser.parse_args()

    if not args.input_dir.is_dir():
        print(f"Error: input_dir does not exist: {args.input_dir}", file=sys.stderr)
        return 1

    ffmpeg_bin = shutil.which(args.ffmpeg) or args.ffmpeg
    if not shutil.which(ffmpeg_bin) and not Path(ffmpeg_bin).is_file():
        print(f"Error: ffmpeg executable not found: {args.ffmpeg}", file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)

    files, duplicates = collect_input_files(args.input_dir)
    total = len(files)
    print(
        f"Found {total} unique audio files in {args.input_dir} "
        f"({len(duplicates)} duplicate-stem file(s) skipped).",
        flush=True,
    )
    for skipped_dup, kept in duplicates:
        print(f"  Duplicate stem: skipped {skipped_dup.name}, kept {kept.name}", flush=True)

    print(
        f"Normalizing to {args.target_lufs} LUFS (TP={args.true_peak}, LRA={args.lra}, "
        f"Opus {args.bitrate}) with {args.workers} workers -> {args.output_dir}",
        flush=True,
    )

    counts = {"normalized": 0, "copied_silent": 0, "skipped": 0, "failed": 0}
    failures: list[FileResult] = []
    start = time.monotonic()
    last_log = start

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(
                normalize_one,
                src,
                args.output_dir,
                ffmpeg_bin,
                args.target_lufs,
                args.true_peak,
                args.lra,
                args.bitrate,
                not args.no_skip_existing,
            )
            for src in files
        ]

        done_count = 0
        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            done_count += 1
            counts[res.status] += 1
            if res.status == "failed":
                failures.append(res)
                print(f"[FAIL] {res.source.name}: {res.error}", file=sys.stderr, flush=True)

            now = time.monotonic()
            if done_count == total or done_count % 100 == 0 or (now - last_log) >= 15.0:
                elapsed = now - start
                processed = done_count - counts["skipped"]
                rate = done_count / elapsed if elapsed > 0 else 0.0
                eta = (total - done_count) / rate if rate > 0 else 0.0
                print(
                    f"[{done_count}/{total}] normalized={counts['normalized']} "
                    f"silent={counts['copied_silent']} skipped={counts['skipped']} "
                    f"failed={counts['failed']} | {rate:.2f} files/s | "
                    f"elapsed={format_duration(elapsed)} | eta={format_duration(eta)}",
                    flush=True,
                )
                last_log = now

    elapsed = time.monotonic() - start
    print(
        f"\nDone in {format_duration(elapsed)}: "
        f"normalized={counts['normalized']}, silent={counts['copied_silent']}, "
        f"skipped={counts['skipped']}, failed={counts['failed']}.",
        flush=True,
    )

    if failures:
        failure_log = args.output_dir / "normalize_failures.log"
        with failure_log.open("w", encoding="utf-8") as fh:
            for item in failures:
                fh.write(f"{item.source.name}\t{item.error}\n")
        print(f"Failure details written to {failure_log}", flush=True)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
