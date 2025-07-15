# database_manager.py
import sqlite3
import logging
from datetime import datetime
from datetime import timedelta
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
        """Creates or updates the database table and indices."""
        logger.debug(f"Initializing database table structure in {self.db_file}...")
        conn = self._get_db_connection()
        if not conn:
            logger.error("Failed to get database connection for initialization.")
            return
        try:
            with conn:
                cursor = conn.cursor()
                # Create the main table if it doesn't exist
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS play_history (
                        request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request_timestamp TEXT NOT NULL,
                        play_start_timestamp TEXT,
                        duration INTEGER,
                        user_id INTEGER NOT NULL,
                        user_name TEXT NOT NULL,
                        guild_id INTEGER NOT NULL,
                        query TEXT NOT NULL,
                        resolved_title TEXT,
                        resolved_url TEXT,
                        channel_name TEXT,
                        status TEXT NOT NULL DEFAULT 'new'
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

    def log_song_request(self, user_id: int, user_name: str, guild_id: int, query: str, resolved_title: str, resolved_url: Optional[str], channel_name: Optional[str], duration: int) -> Optional[int]:
        """
        Logs a successfully processed song request to the database.

        Args:
            user_id: Discord ID of the user making the request.
            user_name: Discord username#discriminator of the user.
            guild_id: Discord Guild ID where the request was made.
            query: The original search query or URL provided by the user.
            resolved_title: The title of the song/video found.
            resolved_url: The webpage URL of the song/video found (e.g., YouTube link). Can be None.
            channel_name: The name of the YouTube channel.
            duration: The duration of the track in seconds.

        Returns:
            The request_id of the newly inserted row, or None if an error occurred.
        """
        resolved_title = resolved_title or "N/A"
        conn = self._get_db_connection()
        if not conn:
            logger.warning("Failed to get DB connection for logging song request.")
            return None

        request_timestamp = datetime.utcnow().isoformat()
        last_row_id = None

        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO play_history (request_timestamp, user_id, user_name, guild_id, query, resolved_title, resolved_url, channel_name, duration)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (request_timestamp, user_id, user_name, guild_id, query, resolved_title, resolved_url, channel_name, duration))
                last_row_id = cursor.lastrowid
                logger.debug(f"Logged request to DB: User={user_name}({user_id}), Query='{query}', Title='{resolved_title}', Duration={duration}s, RequestID={last_row_id}")
        except sqlite3.Error as e:
            logger.error(f"Failed to log song request to database {self.db_file}: {e}", exc_info=True)
        finally:
            if conn:
                conn.close()
        return last_row_id

    def update_play_start_timestamp(self, request_id: int):
        """
        Updates the play_start_timestamp for a given request_id to the current time.

        Args:
            request_id: The ID of the request to update.
        """
        conn = self._get_db_connection()
        if not conn:
            logger.warning(f"Failed to get DB connection for updating play_start_timestamp for request_id {request_id}.")
            return

        play_start_timestamp = datetime.utcnow().isoformat()

        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE play_history
                    SET play_start_timestamp = ?
                    WHERE request_id = ?
                """, (play_start_timestamp, request_id))
                logger.debug(f"Updated play_start_timestamp for request_id {request_id}")
        except sqlite3.Error as e:
            logger.error(f"Failed to update play_start_timestamp for request_id {request_id}: {e}", exc_info=True)
        finally:
            if conn:
                conn.close()

    def get_user_stats(self, user_id: int, guild_id: int) -> Union[Optional[int], Any]:
        """
        Gets the total request count for a given user ID within a specific guild.

        Args:
            user_id: The Discord user ID.
            guild_id: The Discord Guild ID.

        Returns:
            The total number of requests made by the user in that guild, or 0 if an error occurs.
        """
        conn = self._get_db_connection()
        if not conn:
            logger.warning(f"Failed to get DB connection for fetching stats for user {user_id} in guild {guild_id}.")
            return 0

        count = 0
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM play_history WHERE user_id = ? AND guild_id = ?", (user_id, guild_id,))
                result = cursor.fetchone()
                if result:
                    count = result[0]
                logger.debug(f"Fetched stats for user {user_id} in guild {guild_id}: Count={count}")
        except sqlite3.Error as e:
            logger.error(f"Failed to query stats for user {user_id} in guild {guild_id} from {self.db_file}: {e}", exc_info=True)
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
            Returns an empty list on error or if no data exists.
        """
        # Initialize the result list with the precise target type *before* any operations
        leaderboard_data: List[Dict[str, Any]] = []
        conn: Optional[sqlite3.Connection] = None  # Explicitly type conn as potentially None

        try:
            conn = self._get_db_connection()
            if conn is None:  # Use 'is None' for explicit None check
                logger.warning("Failed to get DB connection for fetching leaderboard stats (returned None).")
                # Return the pre-initialized empty list matching the type hint
                return leaderboard_data

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
            logger.error(f"Failed to query leaderboard stats from {self.db_file}: {e}", exc_info=True)
            return []  # Return empty list on error
        finally:
            if conn:
                conn.close()
        return leaderboard_data

    def get_user_stats_long(self, user_id: int, guild_id: int) -> Dict[str, Any]:
        """
        Gets detailed statistics for a given user ID within a specific guild.
        """
        stats: Dict[str, Any] = {
            'today': 0,
            'this_week': 0,
            'this_month': 0,
            'this_year': 0,
            'all_time': 0,
            'top_5_requests': [],
            'longest_streak': 0
        }
        conn = self._get_db_connection()
        if not conn:
            return stats

        try:
            with conn:
                cursor = conn.cursor()
                now = datetime.utcnow()
                today_start = now.strftime('%Y-%m-%d')
                week_start = (now - timedelta(days=now.weekday())).strftime('%Y-%m-%d')
                month_start = now.strftime('%Y-%m-01')
                year_start = now.strftime('%Y-01-01')

                # Time-based counts
                cursor.execute("SELECT COUNT(*) FROM play_history WHERE user_id = ? AND guild_id = ? AND date(request_timestamp) = ?", (user_id, guild_id, today_start))
                stats['today'] = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM play_history WHERE user_id = ? AND guild_id = ? AND date(request_timestamp) >= ?", (user_id, guild_id, week_start))
                stats['this_week'] = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM play_history WHERE user_id = ? AND guild_id = ? AND date(request_timestamp) >= ?", (user_id, guild_id, month_start))
                stats['this_month'] = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM play_history WHERE user_id = ? AND guild_id = ? AND date(request_timestamp) >= ?", (user_id, guild_id, year_start))
                stats['this_year'] = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM play_history WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
                stats['all_time'] = cursor.fetchone()[0]

                # Top 5 most frequent requests
                cursor.execute("""
                    SELECT resolved_title, COUNT(*) as request_count
                    FROM play_history
                    WHERE user_id = ? AND guild_id = ?
                    GROUP BY resolved_title
                    ORDER BY request_count DESC
                    LIMIT 5
                """, (user_id, guild_id))
                stats['top_5_requests'] = [{'title': row['resolved_title'], 'count': row['request_count']} for row in cursor.fetchall()]

                # Longest request streak
                cursor.execute("SELECT DISTINCT date(request_timestamp) FROM play_history WHERE user_id = ? AND guild_id = ? ORDER BY date(request_timestamp)", (user_id, guild_id))
                request_dates = [datetime.strptime(row[0], '%Y-%m-%d').date() for row in cursor.fetchall()]
                if request_dates:
                    longest_streak = 0
                    current_streak = 1
                    for i in range(1, len(request_dates)):
                        if (request_dates[i] - request_dates[i-1]).days == 1:
                            current_streak += 1
                        else:
                            longest_streak = max(longest_streak, current_streak)
                            current_streak = 1
                    stats['longest_streak'] = max(longest_streak, current_streak)

        except sqlite3.Error as e:
            logger.error(f"Failed to query long stats for user {user_id} in guild {guild_id} from {self.db_file}: {e}", exc_info=True)
        finally:
            if conn:
                conn.close()
        return stats
