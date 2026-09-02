import re
from urllib.parse import parse_qs, urlparse


YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


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
