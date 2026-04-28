import logging
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from config import FFMPEG_EXECUTABLE, SONG_CACHE_DIR

logger = logging.getLogger(__name__)

SUPPORTED_URL_SCHEMES = {"http", "https"}
SEARCH_PREFIXES = ("ytsearch", "ytsearchdate", "scsearch")
SEARCH_RESULT_COUNT = 5


def first_existing_path(*candidates) -> str | None:
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def detect_js_runtimes() -> dict:
    runtimes = {}

    deno_path = first_existing_path(
        os.getenv("DENO_EXECUTABLE_PATH"),
        shutil.which("deno"),
        Path.home() / ".deno" / "bin" / "deno.exe",
    )
    if deno_path:
        runtimes["deno"] = {"path": deno_path}

    node_path = first_existing_path(
        os.getenv("NODE_EXECUTABLE_PATH"),
        shutil.which("node"),
        Path("C:/Program Files/nodejs/node.exe"),
    )
    if node_path:
        runtimes["node"] = {"path": node_path}

    return runtimes or {"deno": {}}


JS_RUNTIMES = detect_js_runtimes()
REMOTE_COMPONENTS = ["ejs:github"]

YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'outtmpl': str(SONG_CACHE_DIR / '%(extractor)s-%(id)s.%(ext)s'),
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch1:',
    'source_address': '0.0.0.0',
    'subtitles': False,
    'writethumbnail': False,
    'js_runtimes': JS_RUNTIMES,
    'remote_components': REMOTE_COMPONENTS,
    'extractor_args': {
        'youtube': {
            'player_client': ['default', '-android_sdkless']
        }
    },
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'opus',
        'preferredquality': '192',
    }],
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
    'executable': FFMPEG_EXECUTABLE
}


def is_direct_url(query: str) -> bool:
    """Return True when yt-dlp should treat the query as a direct URL."""
    parsed = urlparse(query.strip())
    return parsed.scheme in SUPPORTED_URL_SCHEMES and bool(parsed.netloc)


def prepare_yt_dlp_query(query: str, search_count: int = 1) -> str:
    """
    Normalise user input before passing it to yt-dlp.

    Plain search terms are prefixed explicitly so special characters such as
    "$$$" are searched literally instead of being interpreted as URL-like input.
    """
    query = query.strip()
    if is_direct_url(query) or query.lower().startswith(SEARCH_PREFIXES):
        return query
    return f"ytsearch{search_count}:{query}"


def is_video_entry(entry: dict) -> bool:
    """Return True when a search result points at a playable YouTube video."""
    ie_key = (entry.get("ie_key") or entry.get("extractor_key") or "").lower()
    entry_url = entry.get("webpage_url") or entry.get("url") or ""
    return ie_key == "youtube" or "youtube.com/watch" in entry_url or "youtu.be/" in entry_url


def is_livestream_info(info: dict) -> bool:
    """Return True when yt-dlp metadata identifies a live YouTube stream."""
    live_status = (info.get("live_status") or "").lower()
    return info.get("is_live") is True or live_status == "is_live"


def video_url_from_entry(entry: dict) -> str | None:
    entry_url = entry.get("webpage_url") or entry.get("url")
    if entry_url and ("youtube.com/watch" in entry_url or "youtu.be/" in entry_url):
        return entry_url

    video_id = entry.get("id")
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"

    return None


def select_first_video_entry(entries: list[dict]) -> dict | None:
    return next((entry for entry in entries if entry and is_video_entry(entry)), None)


def get_yt_dlp_logger() -> logging.Logger:
    """Keep yt-dlp output quiet enough that bad inputs cannot balloon logs."""
    ydl_logger = logging.getLogger('yt-dlp')
    ydl_logger.setLevel(logging.WARNING)
    return ydl_logger


def run_yt_dlp_extractor(query, download=False):
    """Runs yt-dlp extract_info in a way that's pickleable for multiprocessing."""
    try:
        ydl_options = YDL_OPTIONS.copy()
        ydl_options['logger'] = get_yt_dlp_logger()
        prepared_query = prepare_yt_dlp_query(query)

        with yt_dlp.YoutubeDL(ydl_options) as ydl:
            data = ydl.extract_info(prepared_query, download=download)
        return data
    except Exception as e:
        logger.error("Error within run_yt_dlp_extractor for query %r: %s", query, e)
        raise


def run_yt_dlp_search(query):
    """Runs yt-dlp to search for a video without downloading."""
    try:
        search_options = {
            'format': 'bestaudio/best',
            'restrictfilenames': True,
            'noplaylist': True,
            'nocheckcertificate': True,
            'ignoreerrors': False,
            'logtostderr': False,
            'quiet': True,
            'no_warnings': True,
            'default_search': 'ytsearch1:',
            'source_address': '0.0.0.0',
            'logger': get_yt_dlp_logger(),
            'js_runtimes': JS_RUNTIMES,
            'remote_components': REMOTE_COMPONENTS,
        }
        prepared_query = prepare_yt_dlp_query(query, search_count=SEARCH_RESULT_COUNT)

        with yt_dlp.YoutubeDL(search_options) as ydl:
            if not is_direct_url(query):
                flat_options = search_options.copy()
                flat_options['extract_flat'] = 'in_playlist'
                flat_options['playlist_items'] = f"1-{SEARCH_RESULT_COUNT}"
                with yt_dlp.YoutubeDL(flat_options) as flat_ydl:
                    flat_data = flat_ydl.extract_info(prepared_query, download=False)

                selected_entry = select_first_video_entry(flat_data.get('entries') or [])
                selected_url = video_url_from_entry(selected_entry) if selected_entry else None
                if selected_url:
                    logger.info(
                        "Selected search result for %r: %s (%s)",
                        query,
                        selected_entry.get("title", "Unknown Title"),
                        selected_url,
                    )
                    return ydl.extract_info(selected_url, download=False)

            data = ydl.extract_info(prepared_query, download=False)
        return data
    except Exception as e:
        logger.error("Error within run_yt_dlp_search for query %r: %s", query, e)
        raise
