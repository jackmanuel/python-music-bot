import os
import re
from urllib.parse import parse_qs, urlparse


YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
SUPPORTED_URL_SCHEMES = {"http", "https"}
SEARCH_PREFIXES = ("ytsearch", "ytsearchdate", "scsearch")
VALID_CACHE_EXTENSIONS = (".opus", ".m4a", ".webm", ".mp3", ".aac", ".wav")


def is_direct_url(query: str) -> bool:
    """Return True when yt-dlp should treat the query as a direct URL."""
    try:
        parsed = urlparse(query.strip())
    except (AttributeError, ValueError):
        return False
    return parsed.scheme in SUPPORTED_URL_SCHEMES and bool(parsed.netloc)


def normalize_search_query(query: str | None) -> str | None:
    """
    Normalize a plain text search query for exact cache lookup.

    Returns None for empty inputs, direct URLs, or yt-dlp search prefixes.
    """
    if not isinstance(query, str):
        return None
    stripped = query.strip()
    if not stripped:
        return None
    if is_direct_url(stripped):
        return None
    lowered = stripped.lower()
    if lowered.startswith(SEARCH_PREFIXES):
        return None
    return " ".join(lowered.split())


def canonical_youtube_url(youtube_id: str) -> str:
    """Return the canonical watch URL for a YouTube video ID."""
    return f"https://www.youtube.com/watch?v={youtube_id}"


def default_youtube_thumbnail_url(youtube_id: str) -> str:
    """Return the fallback thumbnail URL for a YouTube video ID."""
    return f"https://i.ytimg.com/vi/{youtube_id}/hqdefault.jpg"


def youtube_id_from_cache_path(file_path: str) -> str | None:
    """Extract a YouTube video ID from a yt-dlp cache filename."""
    filename = os.path.basename(file_path)
    stem, extension = os.path.splitext(filename)
    if extension.lower() not in VALID_CACHE_EXTENSIONS:
        return None
    if not stem.startswith("youtube-"):
        return None
    youtube_id = stem[len("youtube-"):]
    return youtube_id or None


def extract_youtube_video_id(url: str) -> str | None:
    """Extract a video ID from common YouTube URL formats without network access."""
    try:
        parsed = urlparse(url.strip())
    except (AttributeError, ValueError):
        return None

    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    video_id = None
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        path_parts = [part for part in parsed.path.split("/") if part]
        if parsed.path.rstrip("/") == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        elif len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
            video_id = path_parts[1]

    return video_id if video_id and YOUTUBE_VIDEO_ID_RE.fullmatch(video_id) else None
