"""Interactive command-line review for finding low-use song cache files."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_DIR = PROJECT_ROOT / "song_cache"
DEFAULT_DATABASE = PROJECT_ROOT / "database" / "music_log.db"
YOUTUBE_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com"}


@dataclass(frozen=True)
class CacheCandidate:
    path: Path
    youtube_id: str
    size_bytes: int
    database_entries: int
    completed_plays: int
    title: str | None = None


@dataclass(frozen=True)
class UnmatchedCacheFile:
    path: Path
    size_bytes: int
    reason: str
    source_url: str | None = None
    title: str | None = None


def extract_youtube_id(url: str | None) -> str | None:
    """Extract a video ID from the YouTube URL forms stored by the bot."""
    if not url:
        return None

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if host == "youtu.be":
        return parsed.path.strip("/").split("/", 1)[0] or None

    if host not in YOUTUBE_HOSTS:
        return None

    path_parts = [part for part in parsed.path.split("/") if part]
    if parsed.path.rstrip("/") == "/watch":
        return parse_qs(parsed.query).get("v", [None])[0]
    if len(path_parts) >= 2 and path_parts[0] in {"embed", "live", "shorts"}:
        return path_parts[1]
    return None


def load_youtube_usage(database_path: Path) -> dict[str, tuple[int, int, str | None]]:
    """Return database counts and a stored title per YouTube ID."""
    database_uri = database_path.resolve().as_uri() + "?mode=ro"
    usage: dict[str, list] = {}

    with closing(sqlite3.connect(database_uri, uri=True)) as connection:
        rows = connection.execute(
            "SELECT resolved_url, play_status, resolved_title FROM play_history "
            "WHERE resolved_url IS NOT NULL"
        )
        for resolved_url, play_status, resolved_title in rows:
            youtube_id = extract_youtube_id(resolved_url)
            if not youtube_id:
                continue
            counts = usage.setdefault(youtube_id, [0, 0, None])
            counts[0] += 1
            if play_status == "completed":
                counts[1] += 1
            if not counts[2] and resolved_title:
                counts[2] = str(resolved_title)

    return {
        youtube_id: (counts[0], counts[1], counts[2])
        for youtube_id, counts in usage.items()
    }


def investigate_cache(
    cache_dir: Path,
    database_path: Path,
    max_plays: int = 1,
    largest_first: bool = False,
) -> tuple[list[CacheCandidate], list[UnmatchedCacheFile]]:
    """Find ranked YouTube files and files that cannot be matched safely."""
    usage = load_youtube_usage(database_path)
    candidates: list[CacheCandidate] = []
    unmatched: list[UnmatchedCacheFile] = []

    for path in cache_dir.iterdir():
        if not path.is_file():
            continue

        try:
            size_bytes = path.stat().st_size
        except OSError as error:
            unmatched.append(UnmatchedCacheFile(path, 0, f"could not read size: {error}"))
            continue

        filename_parts = path.name.split("-", 1)
        if len(filename_parts) != 2:
            unmatched.append(UnmatchedCacheFile(path, size_bytes, "unrecognized filename"))
            continue

        extractor, identifier_with_extension = filename_parts
        if extractor.lower() != "youtube":
            unmatched.append(
                UnmatchedCacheFile(path, size_bytes, f"{extractor} ID is not stored in the database")
            )
            continue

        if path.suffix.lower() == ".part":
            youtube_id = identifier_with_extension.split(".", 1)[0]
            unmatched.append(
                UnmatchedCacheFile(
                    path,
                    size_bytes,
                    "partial download",
                    f"https://www.youtube.com/watch?v={youtube_id}",
                )
            )
            continue

        youtube_id = identifier_with_extension.rsplit(".", 1)[0]
        database_entries, completed_plays, title = usage.get(youtube_id, (0, 0, None))
        if largest_first or completed_plays <= max_plays:
            candidates.append(
                CacheCandidate(
                    path=path,
                    youtube_id=youtube_id,
                    size_bytes=size_bytes,
                    database_entries=database_entries,
                    completed_plays=completed_plays,
                    title=title,
                )
            )

    if largest_first:
        candidates.sort(key=lambda item: (-item.size_bytes, item.path.name.lower()))
    else:
        candidates.sort(
            key=lambda item: (item.database_entries, -item.size_bytes, item.path.name.lower())
        )
    unmatched.sort(key=lambda item: (-item.size_bytes, item.path.name.lower()))
    return candidates, unmatched


def format_size(size_bytes: int) -> str:
    """Format a byte count using binary units."""
    size = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def truncate_text(value: str, width: int) -> str:
    """Truncate text to a fixed terminal column width."""
    if len(value) <= width:
        return value
    return value[: width - 1] + "…"


def youtube_terminal_link(filename: str, youtube_id: str, enabled: bool = True) -> str:
    """Return a clickable filename using the terminal hyperlink escape sequence."""
    return terminal_link(
        filename,
        f"https://www.youtube.com/watch?v={youtube_id}",
        enabled,
    )


def terminal_link(label: str, url: str | None, enabled: bool = True) -> str:
    """Return a terminal hyperlink while preserving the supplied visible label."""
    if not enabled or not url:
        return label
    return f"\033]8;;{url}\033\\{label}\033]8;;\033\\"


def fetch_youtube_title(url: str) -> str | None:
    """Fetch one title through yt-dlp without downloading media."""
    try:
        import yt_dlp

        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
        if isinstance(info, dict) and info.get("title"):
            return str(info["title"])
    except Exception:
        return None
    return None


def resolve_missing_titles(
    candidates: list[CacheCandidate],
    unmatched: list[UnmatchedCacheFile],
    title_fetcher=fetch_youtube_title,
    max_workers: int = 4,
) -> tuple[list[CacheCandidate], list[UnmatchedCacheFile]]:
    """Fill titles absent from the database using metadata-only lookups."""
    candidate_urls = {
        f"https://www.youtube.com/watch?v={item.youtube_id}"
        for item in candidates
        if not item.title
    }
    unmatched_urls = {
        item.source_url
        for item in unmatched
        if item.source_url and not item.title
    }
    urls = sorted(candidate_urls | unmatched_urls)
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(urls) or 1))) as executor:
        titles = dict(zip(urls, executor.map(title_fetcher, urls)))

    resolved_candidates = []
    for item in candidates:
        if item.title:
            resolved_candidates.append(item)
            continue
        url = f"https://www.youtube.com/watch?v={item.youtube_id}"
        resolved_candidates.append(replace(item, title=titles.get(url)))

    resolved_unmatched = []
    for item in unmatched:
        if item.title or not item.source_url:
            resolved_unmatched.append(item)
            continue
        resolved_unmatched.append(replace(item, title=titles.get(item.source_url)))

    return resolved_candidates, resolved_unmatched


def print_report(
    candidates: list[CacheCandidate],
    unmatched: list[UnmatchedCacheFile],
    limit: int,
    show_unmatched: bool,
    use_links: bool = True,
    largest_first: bool = False,
) -> None:
    """Print a compact report suitable for a terminal."""
    shown_candidates = candidates[:limit]
    print("Largest cache files" if largest_first else "Low-use cache candidates")
    print("DB entries  Plays  Size       Title                                                        Filename")
    print("----------  -----  ---------  -----------------------------------------------------------  --------")
    if not shown_candidates:
        print("No matching files found.")
    for item in shown_candidates:
        title = truncate_text(item.title or "[title unavailable]", 59)
        filename = youtube_terminal_link(item.path.name, item.youtube_id, use_links)
        print(
            f"{item.database_entries:>10}  {item.completed_plays:>5}  "
            f"{format_size(item.size_bytes):>9}  {title:<59}  {filename}"
        )

    candidate_bytes = sum(item.size_bytes for item in candidates)
    print(
        f"\nShowing {len(shown_candidates)} of {len(candidates)} candidates "
        f"({format_size(candidate_bytes)} total)."
    )

    if show_unmatched and unmatched:
        shown_unmatched = unmatched[:limit]
        print("\nUnmatched files requiring separate review")
        print("Size       Title                                                        Filename  Reason")
        print("---------  -----------------------------------------------------------  --------  ------")
        for item in shown_unmatched:
            title = truncate_text(item.title or "[title unavailable]", 59)
            filename = terminal_link(item.path.name, item.source_url, use_links)
            print(
                f"{format_size(item.size_bytes):>9}  {title:<59}  "
                f"{filename}  {item.reason}"
            )
        print(f"\nShowing {len(shown_unmatched)} of {len(unmatched)} unmatched files.")


def review_deletions(
    candidates: list[CacheCandidate],
    unmatched: list[UnmatchedCacheFile],
    input_func=None,
) -> tuple[int, int]:
    """Prompt for every displayed file and delete only explicit yes responses."""
    if input_func is None:
        input_func = input

    review_items = [*candidates, *unmatched]
    deleted_count = 0
    deleted_bytes = 0
    if not review_items:
        return deleted_count, deleted_bytes

    print("\nDeletion review (default: no; enter q to stop)")
    for position, item in enumerate(review_items, start=1):
        title = item.title or "title unavailable"
        while True:
            try:
                answer = input_func(
                    f"[{position}/{len(review_items)}] Delete {item.path.name} "
                    f"({format_size(item.size_bytes)}, {title})? [y/N/q] "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nReview stopped.")
                return deleted_count, deleted_bytes

            if answer in {"", "n", "no"}:
                break
            if answer in {"q", "quit"}:
                print("Review stopped.")
                return deleted_count, deleted_bytes
            if answer in {"y", "yes"}:
                try:
                    item.path.unlink()
                except OSError as error:
                    print(f"Could not delete {item.path.name}: {error}")
                else:
                    deleted_count += 1
                    deleted_bytes += item.size_bytes
                    print(f"Deleted {item.path.name}.")
                break
            print("Please enter y, n, or q.")

    print(f"\nDeleted {deleted_count} file(s), freeing {format_size(deleted_bytes)}.")
    return deleted_count, deleted_bytes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Review large cached files with few database entries. Missing YouTube "
            "titles are fetched without downloading media, and deletion requires "
            "an explicit yes for each file."
        )
    )
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--limit", type=int, default=25, help="Maximum rows per section (default: 25)")
    parser.add_argument(
        "--max-plays",
        type=int,
        default=1,
        help="Include files with at most this many completed plays (default: 1)",
    )
    parser.add_argument(
        "--largest",
        action="store_true",
        help="Include all matched files and sort strictly by size, ignoring --max-plays",
    )
    parser.add_argument(
        "--hide-unmatched",
        action="store_true",
        help="Do not show partial, unsupported, or oddly named cache files",
    )
    parser.add_argument(
        "--no-links",
        action="store_true",
        help="Print matched filenames without terminal hyperlinks",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Show the report without prompting to delete files",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit < 1:
        print("error: --limit must be at least 1", file=sys.stderr)
        return 2
    if args.max_plays < 0:
        print("error: --max-plays cannot be negative", file=sys.stderr)
        return 2
    if not args.cache_dir.is_dir():
        print(f"error: cache directory not found: {args.cache_dir}", file=sys.stderr)
        return 1
    if not args.database.is_file():
        print(f"error: database not found: {args.database}", file=sys.stderr)
        return 1

    try:
        candidates, unmatched = investigate_cache(
            args.cache_dir,
            args.database,
            args.max_plays,
            largest_first=args.largest,
        )
    except sqlite3.Error as error:
        print(f"error: could not read database: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"error: could not inspect cache: {error}", file=sys.stderr)
        return 1

    shown_candidates = candidates[:args.limit]
    shown_unmatched = [] if args.hide_unmatched else unmatched[:args.limit]
    missing_title_count = sum(not item.title for item in shown_candidates) + sum(
        bool(item.source_url and not item.title) for item in shown_unmatched
    )
    if missing_title_count:
        print(f"Fetching {missing_title_count} missing YouTube title(s) without downloading media...")
    resolved_candidates, resolved_unmatched = resolve_missing_titles(
        shown_candidates,
        shown_unmatched,
    )
    candidates[:args.limit] = resolved_candidates
    if shown_unmatched:
        unmatched[:args.limit] = resolved_unmatched

    print_report(
        candidates,
        unmatched,
        args.limit,
        not args.hide_unmatched,
        use_links=not args.no_links,
        largest_first=args.largest,
    )
    if not args.report_only:
        if not sys.stdin.isatty():
            print("\nDeletion review skipped because input is not an interactive terminal.")
        else:
            review_deletions(resolved_candidates, resolved_unmatched)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
