import logging
import logging.handlers
import os

def setup_logging(log_file_path: str = "logs/music_bot.log", level: int = logging.INFO):
    """
    Sets up the root logger for the application.

    Args:
        log_file_path (str): The path to the log file.
        level (int): The logging level to set for the root logger.
    """
    # Ensure the directory for the log file exists
    log_dir = os.path.dirname(log_file_path)
    if log_dir and not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir)
        except OSError as e:
            # Use a basic print since logging might not be configured yet
            print(f"Error creating log directory {log_dir}: {e}")
            return

    # Get the root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Create a formatter
    formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s')

    # --- File Handler ---
    # Use a rotating file handler
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=log_file_path,
        when='midnight',
        backupCount=7,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # --- Stream Handler (for console output) ---
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    # --- Set Specific Log Levels for Noisy Libraries ---
    # This prevents the console from being spammed by web server access logs
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)

    # Initial log to confirm setup
    logging.info(f"Logging configured. Log level: {logging.getLevelName(level)}. Log file: {os.path.abspath(log_file_path)}")
