# database_manager.py
import sqlite3
import logging
from datetime import datetime
import os # To potentially get db file path from env
from typing import Union, Optional, List, Dict, Any

# Configure logging for this module
logger = logging.getLogger(__name__) # Use the module's name for the logger

class DatabaseManager:
    """Handles all database operations for the music bot."""

    def __init__(self, db_file="music_log.db"):
        """
        Initializes the DatabaseManager.

        Args:
            db_file (str): The path to the SQLite database file.
        """
        self.db_file = db_file
        logger.info(f"DatabaseManager initialized with file: {self.db_file}")
        # Ensure the directory exists if the path includes folders
        db_dir = os.path.dirname(self.db_file)
        if db_dir and not os.path.exists(db_dir):
            try:
                os.makedirs(db_dir)
                logger.info(f"Created database directory: {db_dir}")
            except OSError as e:
                logger.error(f"Failed to create database directory {db_dir}: {e}")
        # Initialize the database table structure
        self._initialize_database()

    def _get_db_connection(self):
        """Gets a connection to the SQLite database."""
        try:
            conn = sqlite3.connect(self.db_file, timeout=10)
            conn.row_factory = sqlite3.Row # Access columns by name
            # Enable WAL mode for potentially better concurrency, though less critical for simple bots
            # conn.execute("PRAGMA journal_mode=WAL;")
            return conn
        except sqlite3.Error as e:
            logger.error(f"Database connection error to {self.db_file}: {e}", exc_info=True)
            return None

    def _initialize_database(self):
        """Creates the database table and indices if they don't exist."""
        logger.debug(f"Initializing database table structure in {self.db_file}...")
        conn = self._get_db_connection()
        if not conn:
            logger.error("Failed to get database connection for initialization.")
            return
        try:
            with conn:
                cursor = conn.cursor()
                # Create the main table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS play_history (
                        request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        user_id INTEGER NOT NULL,
                        user_name TEXT NOT NULL,
                        guild_id INTEGER NOT NULL,
                        query TEXT NOT NULL,
                        resolved_title TEXT,
                        resolved_url TEXT
                    )
                """)
                # Add index for faster user lookups
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_play_history_user_id ON play_history (user_id);
                """)
                logger.info(f"Database table 'play_history' initialized successfully in {self.db_file}.")
        except sqlite3.Error as e:
            logger.error(f"Database initialization error in {self.db_file}: {e}", exc_info=True)
        finally:
            if conn:
                conn.close()

    def log_song_request(self, user_id: int, user_name: str, guild_id: int, query: str, resolved_title: str, resolved_url: Optional[str]):
        """
        Logs a successfully processed song request to the database.

        Args:
            user_id: Discord ID of the user making the request.
            user_name: Discord username#discriminator of the user.
            guild_id: Discord Guild ID where the request was made.
            query: The original search query or URL provided by the user.
            resolved_title: The title of the song/video found.
            resolved_url: The webpage URL of the song/video found (e.g., YouTube link). Can be None.
        """
        resolved_title = resolved_title or "N/A"
        conn = self._get_db_connection()
        if not conn:
            logger.warning("Failed to get DB connection for logging song request.")
            return # Don't crash if logging fails

        timestamp = datetime.utcnow().isoformat()

        try:
            with conn:
                 cursor = conn.cursor()
                 cursor.execute("""
                    INSERT INTO play_history (timestamp, user_id, user_name, guild_id, query, resolved_title, resolved_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                 """, (timestamp, user_id, user_name, guild_id, query, resolved_title, resolved_url))
                 logger.debug(f"Logged request to DB: User={user_name}({user_id}), Query='{query}', Title='{resolved_title}'")
        except sqlite3.Error as e:
             logger.error(f"Failed to log song request to database {self.db_file}: {e}", exc_info=True)
        finally:
             if conn:
                 conn.close()

    def get_user_stats(self, user_id: int) -> Optional[int]:
        """
        Gets the total request count for a given user ID.

        Args:
            user_id: The Discord user ID.

        Returns:
            The total number of requests made by the user, 0 if the user has no
            requests, or None if a database error occurs.
        """
        conn = self._get_db_connection()
        if not conn:
            logger.warning(f"Failed to get DB connection for fetching stats for user {user_id}. Returning None.")
            return None

        count = 0
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM play_history WHERE user_id = ?", (user_id,))
                result = cursor.fetchone()
                if result:
                    count = result[0]
                logger.debug(f"Fetched stats for user {user_id}: Count={count}")
        except sqlite3.Error as e:
            logger.error(f"Failed to query stats for user {user_id} from {self.db_file}: {e}. Returning None.", exc_info=True)
            return None
        finally:
            if conn:
                conn.close()
        return count

    def get_leaderboard_stats(self, guild_id: int, limit: int = 5) -> Optional[list[dict[str, Any]]]:
        """
        Gets the top users based on song request count.

        Args:
            limit (int): The maximum number of users to return.
            guild_id (int): The Discord Guild ID to filter the leaderboard by.

        Returns:
            A list of dictionaries, each containing 'user_id', 'user_name',
            and 'request_count', ordered by request_count descending.
            Returns an empty list [] if there is no data for the leaderboard.
            Returns None if a database connection error or query error occurs.
        """
        # Initialize the result list with the precise target type *before* any operations
        leaderboard_data: List[Dict[str, Any]] = []
        conn: Optional[sqlite3.Connection] = None  # Explicitly type conn as potentially None

        try:
            conn = self._get_db_connection()
            if conn is None:  # Use 'is None' for explicit None check
                logger.warning("Failed to get DB connection for fetching leaderboard stats. Returning None.")
                return None

            # Proceed only if conn is not None
            with conn:
                cursor = conn.cursor()
                cursor.execute("""
                            SELECT
                                user_id,
                                user_name,
                                COUNT(request_id) as request_count
                            FROM play_history
                            WHERE guild_id = ?  -- Filter by the specific guild_id
                            GROUP BY user_id    -- Group within that guild
                            ORDER BY request_count DESC
                            LIMIT ?
                        """, (guild_id, limit)) # Pass guild_id first, then limit
                results: List[sqlite3.Row] = cursor.fetchall()  # Hint fetchall result type

                for row in results:
                    # Ensure keys match column names/aliases from the query
                    # The types returned by sqlite (int, str) are compatible with Any
                    user_data: Dict[str, Any] = {
                        'user_id': row['user_id'],
                        'user_name': row['user_name'],
                        'request_count': row['request_count']
                    }
                    leaderboard_data.append(user_data)

                logger.debug(f"Fetched leaderboard stats: Found {len(leaderboard_data)} users.")
        except sqlite3.Error as e:
            logger.error(f"Failed to query leaderboard stats from {self.db_file}: {e}. Returning None.", exc_info=True)
            return None
        finally:
            if conn:
                conn.close()
        return leaderboard_data