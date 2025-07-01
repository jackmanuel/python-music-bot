import subprocess
import http.server
import socketserver
import threading
import time
import os
import signal

# --- Configuration ---
LOG_FILE = "music_bot.log"
BOT_SCRIPT = "music_bot.py"
SERVER_PORT = 8000
BOT_PID_FILE = "music_bot.pid"
SERVER_PID_FILE = "server.pid"

# --- Correctly locate the Python executable in the virtual environment ---
# This is crucial for ensuring the bot runs with the correct dependencies.
VENV_PYTHON = os.path.abspath(os.path.join(".venv", "Scripts", "python.exe"))
BOT_SCRIPT_PATH = os.path.abspath(BOT_SCRIPT)

def run_bot():
    """
    Starts the music bot as a subprocess using the venv Python interpreter,
    logs its output, and saves its PID.
    """
    print(f"Starting {BOT_SCRIPT} with interpreter: {VENV_PYTHON}")
    if not os.path.exists(VENV_PYTHON):
        error_message = f"ERROR: Virtual environment Python not found at '{VENV_PYTHON}'. Please ensure the venv exists and is correctly structured.\n"
        print(error_message)
        with open(LOG_FILE, 'ab') as log_file:
            log_file.write(error_message.encode('utf-8'))
        return

    with open(LOG_FILE, 'ab') as log_file:
        try:
            # Use STARTUPINFO to hide the console window, while still creating a new process group
            # that can correctly receive the CTRL+C signal for graceful shutdown.
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE # Hide the window

            process = subprocess.Popen(
                [VENV_PYTHON, "-u", BOT_SCRIPT_PATH],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            )
            print(f"{BOT_SCRIPT} started with PID: {process.pid}")
            with open(BOT_PID_FILE, "w") as f:
                f.write(str(process.pid))
            
            process.wait()
        except FileNotFoundError:
            error_message = f"ERROR: Could not find '{BOT_SCRIPT_PATH}'. Make sure the script exists.\n"
            print(error_message)
            log_file.write(error_message.encode('utf-8'))
        except Exception as e:
            error_message = f"An unexpected error occurred while trying to run the bot: {e}\n"
            print(error_message)
            log_file.write(error_message.encode('utf-8'))
        finally:
            if os.path.exists(BOT_PID_FILE):
                os.remove(BOT_PID_FILE)
            print("Bot process has terminated.")

def run_server():
    """
    Runs a simple HTTP server to display the log file.
    """
    class LogFileHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/favicon.ico':
                self.send_response(204)  # No Content
                self.end_headers()
                return
            
            if self.path == '/':
                self.path = '/' + LOG_FILE
            
            self.send_response(200)
            self.send_header('Content-type', 'text/plain; charset=utf-8')
            self.send_header('Refresh', '5')
            self.end_headers()
            try:
                with open(LOG_FILE, 'rb') as f:
                    self.wfile.write(f.read())
            except FileNotFoundError:
                self.wfile.write(b"Log file not found.")
            except Exception as e:
                self.wfile.write(f"Error reading log file: {e}".encode('utf-8'))

        def log_message(self, format, *args):
            # Silently ignore log messages to keep the console clean
            return

    # --- Server and Shutdown Handler ---
    httpd = socketserver.TCPServer(("", SERVER_PORT), LogFileHandler)

    def shutdown_checker():
        """Checks for a stop file and shuts down the server if it exists."""
        stop_file = "stop.flag"
        while True:
            if os.path.exists(stop_file):
                print("Stop file detected. Shutting down server...")
                # Clean up the flag file
                os.remove(stop_file)
                # Shutdown the server in a separate thread to prevent deadlock
                threading.Thread(target=httpd.shutdown).start()
                break # Exit the checker thread
            time.sleep(1) # Check every second

    # Start the shutdown checker in a daemon thread
    checker_thread = threading.Thread(target=shutdown_checker, daemon=True)
    checker_thread.start()

    print(f"Log server started at http://localhost:{SERVER_PORT}")
    httpd.serve_forever() # This will block until shutdown() is called
    print("Server has been shut down.")

if __name__ == "__main__":
    with open(SERVER_PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, 'w').close()

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    time.sleep(2)

    try:
        run_server()
    finally:
        if os.path.exists(SERVER_PID_FILE):
            os.remove(SERVER_PID_FILE)
        print("Server has shut down.")

