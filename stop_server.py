import os
import subprocess
import time

# --- Configuration ---
BOT_PID_FILE = "music_bot.pid"
SERVER_PID_FILE = "server.pid"
STOP_FLAG_FILE = "stop.flag"

def stop_process(pid_file, process_name):
    """Reads a PID and attempts a graceful shutdown, forcing it if necessary."""
    if not os.path.exists(pid_file):
        print(f"PID file not found for {process_name}. It might not be running.")
        return

    try:
        with open(pid_file, 'r') as f:
            pid = f.read().strip()
            if not pid.isdigit():
                print(f"Invalid PID found for {process_name}: {pid}")
                return
    except (IOError, ValueError) as e:
        print(f"Error reading PID for {process_name}: {e}")
        return

    # --- Stage 1: Attempt Graceful Shutdown ---
    print(f"Attempting graceful shutdown of {process_name} (PID: {pid})...")
    try:
        # For the server, create a stop flag file
        if process_name == "Log Server":
            with open(STOP_FLAG_FILE, 'w') as f:
                f.write("stop")
            print(f"Created stop flag file for {process_name}.")
        else:
            # For the bot, send a non-forceful taskkill command
            subprocess.run(["taskkill", "/PID", pid, "/T"], capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Error during graceful shutdown attempt for {process_name}: {e}")
        pass # Ignore errors here; if it fails, we'll check if it's running and force kill it.

    # --- Stage 2: Verify and Force if Necessary ---
    # Give the process a moment to shut down.
    time.sleep(5) # Increased wait time for graceful shutdown

    try:
        # Check if the process is still running. The "where" clause filters by PID.
        check_proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True
        )
        # If the PID appears in the output, the process is still alive.
        if pid in check_proc.stdout:
            print(f"{process_name} did not shut down gracefully. Forcing termination...")
            subprocess.run(["taskkill", "/F", "/PID", pid, "/T"], check=True, capture_output=True)
            print(f"{process_name} forcefully terminated.")
        else:
            print(f"{process_name} shut down gracefully.")
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"Could not verify process status, but shutdown was attempted. Error: {e}")
    finally:
        # Clean up the PID file
        if os.path.exists(pid_file):
            os.remove(pid_file)

if __name__ == "__main__":
    print("--- Stopping Music Bot and Server ---")
    stop_process(BOT_PID_FILE, "Music Bot")
    time.sleep(1)
    stop_process(SERVER_PID_FILE, "Log Server")
    print("--- Shutdown complete ---")

