import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class SongCache:
    """Indexes and manages downloaded song files."""

    VALID_EXTENSIONS = ('.opus', '.m4a', '.webm', '.mp3', '.aac', '.wav')

    def __init__(
        self,
        cache_dir: Path,
        create_if_missing: bool = True,
        max_size_bytes: int | None = None,
        warning_threshold_bytes: int = 0,
    ):
        self.cache_dir = cache_dir
        self.create_if_missing = create_if_missing
        self.max_size_bytes = max_size_bytes
        self.warning_threshold_bytes = warning_threshold_bytes
        self._songs = {}
        self._capacity_state = None
        self.load()

    def __len__(self):
        return len(self._songs)

    def values(self):
        return self._songs.values()

    def get(self, youtube_id):
        return self._songs.get(youtube_id)

    @property
    def size_bytes(self):
        """Return the current size of all files in the cache directory."""
        if not self.cache_dir.exists():
            return 0

        total = 0
        try:
            for entry in os.scandir(self.cache_dir):
                if entry.is_file():
                    total += entry.stat().st_size
        except OSError as e:
            logger.warning(f"Could not calculate song cache size: {e}")
        return total

    def _check_capacity(self, additional_bytes=0):
        """Log capacity transitions and return whether another file can fit."""
        if self.max_size_bytes is None:
            return True

        current_size = self.size_bytes
        remaining_bytes = max(0, self.max_size_bytes - current_size)

        if current_size >= self.max_size_bytes:
            state = "full"
        elif remaining_bytes <= self.warning_threshold_bytes:
            state = "near"
        else:
            state = "normal"

        if state != self._capacity_state:
            if state == "full":
                logger.warning(
                    "Song cache is full (%0.2f GB of %0.2f GB); "
                    "uncached songs will be streamed.",
                    current_size / 1_000_000_000,
                    self.max_size_bytes / 1_000_000_000,
                )
            elif state == "near":
                logger.warning(
                    "Song cache is almost full: %0.2f GB remains of %0.2f GB.",
                    remaining_bytes / 1_000_000_000,
                    self.max_size_bytes / 1_000_000_000,
                )
            elif self._capacity_state in {"near", "full"}:
                logger.info("Song cache is no longer near its configured size limit.")
            self._capacity_state = state

        return (
            current_size < self.max_size_bytes
            and current_size + max(0, additional_bytes) <= self.max_size_bytes
        )

    def can_accept_download(self, estimated_size_bytes=0):
        """Return whether a new download can fit within the configured limit."""
        return self._check_capacity(estimated_size_bytes or 0)

    def add(self, youtube_id, file_path):
        file_size = os.path.getsize(file_path)
        self._check_capacity()
        if self.max_size_bytes is not None and self.size_bytes > self.max_size_bytes:
            logger.warning(
                "Downloaded song %s would exceed the cache size limit; rejecting %s-byte file.",
                youtube_id,
                file_size,
            )
            return False

        self._songs[youtube_id] = file_path
        logger.info(f"Added song {youtube_id} to cache")
        self._check_capacity()
        return True

    def remove(self, youtube_id, expected_file_path=None):
        """Remove one indexed cache file if it still matches the expected path."""
        file_path = self._songs.get(youtube_id)
        if not file_path:
            return False

        if expected_file_path is not None:
            indexed_path = os.path.normcase(os.path.abspath(file_path))
            expected_path = os.path.normcase(os.path.abspath(expected_file_path))
            if indexed_path != expected_path:
                logger.warning(
                    "Refusing to remove cache entry %s because its path changed from %s to %s.",
                    youtube_id,
                    expected_file_path,
                    file_path,
                )
                return False

        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError as e:
            logger.warning("Could not remove cached song %s at %s: %s", youtube_id, file_path, e)
            return False

        self._songs.pop(youtube_id, None)
        logger.info("Removed song %s from cache", youtube_id)
        self._check_capacity()
        return True

    def refresh_capacity(self):
        """Refresh capacity logs after files have been removed externally."""
        self._check_capacity()

    def clear(self):
        self._songs.clear()
        self._check_capacity()

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
        self._check_capacity()
