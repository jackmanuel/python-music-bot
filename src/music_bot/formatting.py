import logging

logger = logging.getLogger(__name__)


def format_duration(seconds: float) -> str:
    """Formats seconds into MM:SS or HH:MM:SS."""
    if seconds is None or not isinstance(seconds, (int, float)):
        return "??:??"
    try:
        seconds = int(seconds)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"
    except Exception:
        logger.warning(f"Could not format duration for seconds: {seconds}")
        return "??:??"
