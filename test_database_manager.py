import unittest
from unittest.mock import patch, MagicMock
import sqlite3
# No 'os' import needed if only using in-memory db and not testing file creation/deletion directly.

from database_manager import DatabaseManager 

class TestDatabaseManager(unittest.TestCase):

    def setUp(self):
        # Initialize DatabaseManager with an in-memory database for each test
        # This ensures tests are isolated and don't interfere with each other or a real db file.
        self.db_manager_memory = DatabaseManager(db_file=":memory:")
        # Helper to insert some data for multiple tests
        self._populate_test_data(self.db_manager_memory)

    def _populate_test_data(self, db_manager_instance):
        # Sample data population
        db_manager_instance.log_song_request(100, "User1", 1, "query1", "Title1", "url1")
        db_manager_instance.log_song_request(100, "User1", 1, "query2", "Title2", "url2") # User1, Guild1, 2nd request
        db_manager_instance.log_song_request(200, "User2", 1, "query3", "Title3", "url3") # User2, Guild1, 1st request
        db_manager_instance.log_song_request(300, "User3", 2, "query4", "Title4", "url4") # User3, Guild2, 1st request
        db_manager_instance.log_song_request(100, "User1", 2, "query5", "Title5", "url5") # User1, Guild2, 3rd request (overall for User1)

    # --- Tests for get_user_stats ---
    def test_get_user_stats_success_multiple_requests(self):
        # User 100 has 2 requests in guild 1 and 1 in guild 2, total 3
        count = self.db_manager_memory.get_user_stats(user_id=100)
        self.assertEqual(count, 3)

    def test_get_user_stats_single_request(self):
        count = self.db_manager_memory.get_user_stats(user_id=200)
        self.assertEqual(count, 1)

    def test_get_user_stats_no_requests_for_user(self):
        count = self.db_manager_memory.get_user_stats(user_id=999) # This user has no requests
        self.assertEqual(count, 0)

    @patch('database_manager.DatabaseManager._get_db_connection')
    def test_get_user_stats_db_connection_fails(self, mock_get_conn):
        mock_get_conn.return_value = None
        # Create a new manager instance that will use the patched _get_db_connection
        # This is important because self.db_manager_memory in setUp already has its _initialize_database called
        # with a real in-memory connection. For this test, we want to simulate failure from the start.
        patched_manager = DatabaseManager(db_file="dummy.db") # Filename doesn't matter, connection is mocked
        # We don't need to call _initialize_database on patched_manager as it would try to use the mocked conn
        
        count = patched_manager.get_user_stats(user_id=100)
        self.assertIsNone(count)
        mock_get_conn.assert_called_once() # Ensure _get_db_connection was indeed called

    @patch('database_manager.DatabaseManager._get_db_connection')
    def test_get_user_stats_sqlite_error_during_query(self, mock_get_conn):
        # Setup mock connection and cursor
        mock_sqlite_conn = MagicMock(spec=sqlite3.Connection)
        mock_cursor = MagicMock(spec=sqlite3.Cursor)
        
        # Configure cursor.execute to raise sqlite3.Error
        mock_cursor.execute.side_effect = sqlite3.Error("Simulated database query error")
        
        # Configure the connection mock to return the faulty cursor
        mock_sqlite_conn.cursor.return_value = mock_cursor
        
        # Configure _get_db_connection to return our mock connection
        mock_get_conn.return_value = mock_sqlite_conn

        # Create a manager instance that will use the patched connection logic
        patched_manager = DatabaseManager(db_file="dummy.db") 
        
        count = patched_manager.get_user_stats(user_id=100)
        self.assertIsNone(count)
        
        # Verifications
        mock_get_conn.assert_called_once()
        mock_sqlite_conn.cursor.assert_called_once()
        mock_cursor.execute.assert_called_once_with("SELECT COUNT(*) FROM play_history WHERE user_id = ?", (100,))
        mock_sqlite_conn.close.assert_called_once() # Crucial: ensure close is called even on error

    # --- Tests for get_leaderboard_stats ---
    def test_get_leaderboard_stats_success_guild1(self):
        # Guild 1: User1 (2 requests), User2 (1 request)
        leaderboard = self.db_manager_memory.get_leaderboard_stats(guild_id=1, limit=5)
        self.assertIsNotNone(leaderboard)
        self.assertEqual(len(leaderboard), 2)
        
        # User1 should be first
        self.assertEqual(leaderboard[0]['user_id'], 100)
        self.assertEqual(leaderboard[0]['user_name'], "User1")
        self.assertEqual(leaderboard[0]['request_count'], 2)
        self.assertListEqual(sorted(list(leaderboard[0].keys())), sorted(['user_id', 'user_name', 'request_count']))
        self.assertIsInstance(leaderboard[0]['user_id'], int)
        self.assertIsInstance(leaderboard[0]['user_name'], str)
        self.assertIsInstance(leaderboard[0]['request_count'], int)

        # User2 should be second
        self.assertEqual(leaderboard[1]['user_id'], 200)
        self.assertEqual(leaderboard[1]['user_name'], "User2")
        self.assertEqual(leaderboard[1]['request_count'], 1)

    def test_get_leaderboard_stats_success_guild2(self):
        # Guild 2: User3 (1 request), User1 (1 request in this guild)
        leaderboard = self.db_manager_memory.get_leaderboard_stats(guild_id=2, limit=5)
        self.assertIsNotNone(leaderboard)
        self.assertEqual(len(leaderboard), 2) # User3 and User1

        # Order can be User3 then User1, or User1 then User3, as both have 1 request in this guild.
        # We'll check if both are present and have correct counts.
        user_ids_in_leaderboard = {entry['user_id'] for entry in leaderboard}
        self.assertIn(300, user_ids_in_leaderboard)
        self.assertIn(100, user_ids_in_leaderboard)

        for entry in leaderboard:
            self.assertEqual(entry['request_count'], 1)
            if entry['user_id'] == 300:
                self.assertEqual(entry['user_name'], "User3")
            elif entry['user_id'] == 100:
                self.assertEqual(entry['user_name'], "User1") # Name from the first log for user 100

    def test_get_leaderboard_stats_empty_for_guild_with_no_requests(self):
        leaderboard = self.db_manager_memory.get_leaderboard_stats(guild_id=999) # This guild has no requests
        self.assertIsNotNone(leaderboard) # Should return empty list, not None
        self.assertEqual(len(leaderboard), 0)

    @patch('database_manager.DatabaseManager._get_db_connection')
    def test_get_leaderboard_stats_db_connection_fails(self, mock_get_conn):
        mock_get_conn.return_value = None
        patched_manager = DatabaseManager(db_file="dummy.db")
        
        leaderboard = patched_manager.get_leaderboard_stats(guild_id=1)
        self.assertIsNone(leaderboard)
        mock_get_conn.assert_called_once()

    @patch('database_manager.DatabaseManager._get_db_connection')
    def test_get_leaderboard_stats_sqlite_error_during_query(self, mock_get_conn):
        mock_sqlite_conn = MagicMock(spec=sqlite3.Connection)
        mock_cursor = MagicMock(spec=sqlite3.Cursor)
        mock_cursor.execute.side_effect = sqlite3.Error("Simulated DB error for leaderboard")
        mock_sqlite_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_sqlite_conn

        patched_manager = DatabaseManager(db_file="dummy.db")
        
        leaderboard = patched_manager.get_leaderboard_stats(guild_id=1, limit=3)
        self.assertIsNone(leaderboard)
        
        mock_get_conn.assert_called_once()
        mock_sqlite_conn.cursor.assert_called_once()
        # Verify the query and parameters. Note the SQL query from the original code.
        expected_query_part = "SELECT user_id, user_name, COUNT(request_id) as request_count FROM play_history WHERE guild_id = ? GROUP BY user_id ORDER BY request_count DESC LIMIT ?"
        # Whitespace differences can be tricky, so we check for the core part or normalize.
        # For simplicity, we assume the exact string from the code.
        actual_call_args = mock_cursor.execute.call_args[0][0] # First argument of the first call
        self.assertTrue(all(word in actual_call_args for word in ["SELECT", "user_id", "user_name", "COUNT(request_id)", "play_history", "WHERE guild_id = ?", "GROUP BY user_id", "ORDER BY request_count DESC", "LIMIT ?"]))
        self.assertEqual(mock_cursor.execute.call_args[0][1], (1, 3)) # Parameters
        mock_sqlite_conn.close.assert_called_once()

    def test_log_song_request_and_retrieval_integration(self):
        # This test acts as a mini-integration test for the logging and basic retrieval.
        # It ensures _initialize_database (called by __init__) works for the in-memory DB.
        fresh_manager = DatabaseManager(db_file=":memory:") # Use a completely fresh in-memory DB
        
        # Log a request
        fresh_manager.log_song_request(
            user_id=777, user_name="LuckyUser", guild_id=77, 
            query="lucky query", resolved_title="Lucky Title", resolved_url="http://lucky.url"
        )
        
        # Test get_user_stats
        count = fresh_manager.get_user_stats(user_id=777)
        self.assertEqual(count, 1)
        
        # Test get_leaderboard_stats
        leaderboard = fresh_manager.get_leaderboard_stats(guild_id=77)
        self.assertIsNotNone(leaderboard)
        self.assertEqual(len(leaderboard), 1)
        self.assertEqual(leaderboard[0]['user_id'], 777)
        self.assertEqual(leaderboard[0]['user_name'], "LuckyUser")
        self.assertEqual(leaderboard[0]['request_count'], 1)

    def test_data_type_consistency_in_leaderboard(self):
        # Specifically check data types from get_leaderboard_stats
        leaderboard = self.db_manager_memory.get_leaderboard_stats(guild_id=1, limit=1)
        self.assertIsNotNone(leaderboard)
        self.assertGreater(len(leaderboard), 0, "Leaderboard should not be empty for this test based on setUp")
        
        item = leaderboard[0]
        self.assertIsInstance(item['user_id'], int, "user_id should be an integer")
        self.assertIsInstance(item['user_name'], str, "user_name should be a string")
        self.assertIsInstance(item['request_count'], int, "request_count should be an integer")

if __name__ == '__main__':
    unittest.main(verbosity=2)
