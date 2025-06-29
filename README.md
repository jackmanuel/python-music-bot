
---

# Discord Music Bot

A straightforward Discord music bot built with `discord.py`. It plays audio from YouTube, manages a queue, and tracks user song request statistics.

## Features

*   Play music from YouTube via search or direct URL.
*   Full queue management: add, view (`!q`), skip, remove, and clear.
*   Show what's currently playing (`!np`).
*   User stats: see how many songs you (`!stats`) or others have requested.
*   Server leaderboard (`!lb`) for top requesters.
*   Automatically disconnects from empty or inactive voice channels.

## Setup for Windows

Follow these steps to get the bot running on a Windows machine.

### 1. Prerequisites

*   **Python:** Install [Python 3.8 or newer](https://www.python.org/downloads/windows/). **Important:** During installation, make sure to check the box that says **"Add Python to PATH"**.

*   **FFmpeg:** This is required for audio playback.
    1.  Download a `release-full` build of FFmpeg from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/).
    2.  Extract the downloaded `.zip` file into a permanent location, for example, `C:\ffmpeg`.
    3.  You now have two options:
        *   **(Recommended) Add to System PATH:** Add the `bin` folder from your FFmpeg directory (e.g., `C:\ffmpeg\bin`) to your Windows `Path` environment variable. This allows the bot to find it automatically.
        *   **(Alternative) Set in `.env` file:** If you don't want to modify your system PATH, you can specify the direct path to `ffmpeg.exe` in the configuration file later.

### 2. Project Installation

1.  **Download the Code:** Download or clone this repository to your computer.

2.  **Install Dependencies:** Open a Command Prompt or PowerShell in the project's folder (where `requirements.txt` is located) and run:
    ```bash
    pip install -r requirements.txt
    ```

### 3. Configuration

1.  **Create `.env` file:** In the same folder, create a new file named `.env`.

2.  **Add Bot Token:** Open the `.env` file and add your Discord bot token. You can get this from the [Discord Developer Portal](https://discord.com/developers/applications).
    ```dotenv
    DISCORD_BOT_TOKEN=YOUR_BOT_TOKEN_HERE
    ```

3.  **(Optional) Configure FFmpeg Path:** If you did not add FFmpeg to your system PATH, you must add the following line to your `.env` file. **Remember to use forward slashes (`/`) for the path.**
    ```dotenv
    # Example path, change it to match your own
    FFMPEG_EXECUTABLE_PATH=C:/ffmpeg/bin/ffmpeg.exe
    ```

### 4. Running the Bot

Once configured, run the bot from your terminal:

```bash
python music_bot.py
```

If everything is set up correctly, you'll see a confirmation message in your terminal, and the bot will appear online in your Discord server.

## Basic Commands

*   `!join`: Bot joins your voice channel.
*   `!play <song name or URL>`: Searches for and plays a song.
*   `!skip`: Skips the current song.
*   `!queue` or `!q`: Shows the current song queue.
*   `!np`: Shows the currently playing song and its progress.
*   `!stats [@user]`: Shows request stats for you or another user.
*   `!leaderboard` or `!lb`: Shows the top 5 song requesters.
*   `!leave`: Disconnects the bot from the voice channel.