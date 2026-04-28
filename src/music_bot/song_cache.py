import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class SongCache:
    """Indexes and manages downloaded song files."""

    VALID_EXTENSIONS = ('.opus', '.m4a', '.webm', '.mp3', '.aac', '.wav')

    def __init__(self, cache_dir: Path, create_if_missing: bool = True):
        self.cache_dir = cache_dir
        self.create_if_missing = create_if_missing
        self._songs = {}
        self.load()

    def __len__(self):
        return len(self._songs)

    def values(self):
        return self._songs.values()

    def get(self, youtube_id):
        return self._songs.get(youtube_id)

    def add(self, youtube_id, file_path):
        self._songs[youtube_id] = file_path
        logger.info(f"Added song {youtube_id} to cache")

    def clear(self):
        self._songs.clear()

    def load(self):
        """Load existing song cache from the cache directory."""
        if not os.path.exists(self.cache_dir):
            if self.create_if_missing:
                os.makedirs(self.cache_dir)
            else:
                logger.info("Song cache directory does not exist; starting without a cache index.")
            return

        logger.info("Loading existing song cache...")
        for filename in os.listdir(self.cache_dir):
            if filename.endswith(self.VALID_EXTENSIONS):
                try:
                    name_part = os.path.splitext(filename)[0]
                    parts = name_part.split('-')
                    if len(parts) >= 2:
                        youtube_id = "-".join(parts[1:])
                        file_path = str(self.cache_dir / filename)
                        self._songs[youtube_id] = file_path
                except Exception as e:
                    logger.error(f"Error loading cache file {filename}: {e}")

        logger.info(f"Loaded {len(self._songs)} songs from cache")
