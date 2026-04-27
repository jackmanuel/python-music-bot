import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
SONG_CACHE_DIR = BASE_DIR / "song_cache"
LOG_VIEWER_TEMPLATE = BASE_DIR / "web" / "templates" / "log_viewer.html"


def project_path_from_env(env_var: str, default: Path) -> Path:
    """Resolve project-relative paths from environment variables."""
    configured_path = os.getenv(env_var)
    path = Path(configured_path) if configured_path else default
    return path if path.is_absolute() else BASE_DIR / path


load_dotenv(BASE_DIR / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
FFMPEG_EXECUTABLE = os.getenv("FFMPEG_EXECUTABLE_PATH", "ffmpeg")
FFMPEG_VOLUME = "0.5"

INACTIVITY_TIMEOUT_MINUTES = 20
MAX_SONG_DURATION_SECONDS = int(os.getenv("MAX_SONG_DURATION_SECONDS", "1800"))

DATABASE_FILE = project_path_from_env("DATABASE_FILE_PATH", BASE_DIR / "database" / "music_log.db")
LOG_FILE = project_path_from_env("LOG_FILE_PATH", BASE_DIR / "logs" / "music_bot.log")

SERVER_HOST = "localhost"
SERVER_PORT = 8000
