import discord
from discord.ext import commands, tasks
import yt_dlp
import asyncio
import logging
import time
from collections import deque
import os # For token loading
from dotenv import load_dotenv
import concurrent.futures
import subprocess # Added for FFmpeg check

from database_manager import DatabaseManager

# --- Load environment variables from .env file ---
load_dotenv() # <--- CALL THIS EARLY

# --- Configuration ---
# Load token securely from environment variable
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not DISCORD_TOKEN:
    # Handle error if token isn't found (important!)
    print("CRITICAL ERROR: DISCORD_BOT_TOKEN not found in environment variables.")
    print("Make sure you have a .env file with DISCORD_BOT_TOKEN=YOUR_TOKEN")
    exit() # Exit if the token is missing

# --- Load FFmpeg Path from environment variable ---
# Get the path from .env, defaulting to just "ffmpeg" if the variable is not set.
# This allows it to still work if FFmpeg is in the system PATH and the .env variable isn't defined.
FFMPEG_EXECUTABLE = os.getenv("FFMPEG_EXECUTABLE_PATH", "ffmpeg")

INACTIVITY_TIMEOUT_MINUTES = 30 # Minutes before leaving the voice channel due to inactivity

# --- Database Configuration ---
# Define the path here, or load from .env for more flexibility
DATABASE_FILE = os.getenv("DATABASE_FILE_PATH", "music_log.db")

# --- Basic Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('discord')

# Add a log message to confirm which FFmpeg path is being used
logger.info(f"Using FFmpeg executable located at: {FFMPEG_EXECUTABLE}")

# --- FFmpeg Check Function ---
def check_ffmpeg(ffmpeg_path: str) -> bool:
    """Checks if FFmpeg is accessible and working."""
    try:
        process = subprocess.run([ffmpeg_path, "-version"], capture_output=True, text=True, check=False) # check=False to handle non-zero exits manually
        if process.returncode == 0 and "ffmpeg version" in process.stdout.lower():
            # Extract the first line for a concise version log
            version_line = process.stdout.splitlines()[0]
            logger.info(f"FFmpeg check successful. Version: {version_line.strip()} (Path: {ffmpeg_path})")
            return True
        else:
            error_message = f"FFmpeg check failed (Path: {ffmpeg_path}). Return code: {process.returncode}\n"
            error_message += f"Stdout: {process.stdout.strip()}\n"
            error_message += f"Stderr: {process.stderr.strip()}"
            logger.critical(error_message)
            return False
    except FileNotFoundError:
        logger.critical(f"FFmpeg executable not found at path: {ffmpeg_path}. Please install FFmpeg and ensure it's in your system PATH or FFMPEG_EXECUTABLE_PATH is set correctly in .env.")
        return False
    except Exception as e:
        logger.critical(f"An unexpected error occurred while checking FFmpeg (Path: {ffmpeg_path}): {e}", exc_info=True)
        return False

# --- yt-dlp Options ---
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'opus',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
}

# --- FFmpeg Options ---
# -before_options: Handle reconnections before decoding starts
# -reconnect 1: Enable reconnection
# -reconnect_streamed 1: Enable reconnection on streamed URLs
# -reconnect_delay_max 5: Maximum delay before reconnect attempt
# -vn: Disable video processing
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
    'executable': FFMPEG_EXECUTABLE
}

def run_yt_dlp_extractor(query):
    """
    Runs yt-dlp extract_info in a way that's pickleable for multiprocessing.
    Needs YDL_OPTIONS to be globally accessible or passed explicitly if refactored.
    """
    try:
        # NOTE: Creates a new YoutubeDL instance each time in the new process.
        # This is generally fine for ProcessPoolExecutor.
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            data = ydl.extract_info(query, download=False)
        return data
    except Exception as e:
        # Log or handle errors occurring *within* the worker process if necessary
        # For simplicity, we'll let the main process catch it via the future
        logger.error(f"Error within run_yt_dlp_extractor for '{query}': {e}")
        # Re-raise or return an indicator if needed, but often letting the
        # executor raise it in the main thread is sufficient.
        raise # Re-raise the exception to be caught by the await call


# --- Bot Class ---
intents = discord.Intents.default()
intents.message_content = True  # Enable message content intent
intents.voice_states = True     # Enable voice state intent for joining/leaving tracking
intents.members = True # Needed for stats command

bot = commands.Bot(command_prefix="!", intents=intents)

# --- Music Cog ---
class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queues = {}  # Dictionary to hold queues for each guild {guild_id: deque()}
        self.current_song = {} # Dictionary to hold current song info {guild_id: song_info}
        self.voice_clients = {} # Dictionary to hold voice clients {guild_id: voice_client}
        self.last_activity = {} # Dictionary to track last activity time {guild_id: timestamp}
        # Use 1 quarter the cores, minimum 1
        cpu_cores = os.cpu_count() or 1
        max_workers = max(1, cpu_cores // 4)
        logger.info(f"Initializing ProcessPoolExecutor with max_workers={max_workers}")
        self.process_executor = concurrent.futures.ProcessPoolExecutor(max_workers=max_workers)

        self.db_manager = DatabaseManager(DATABASE_FILE) # Pass the configured DB file path

        self.inactivity_check.start()

    def cog_unload(self):
        logger.info("Shutting down ProcessPoolExecutor...")
        # Shutdown executor - True waits, False returns immediately
        self.process_executor.shutdown(wait=True)
        logger.info("Cancelling inactivity check task.")
        self.inactivity_check.cancel()

    def get_queue(self, guild_id):
        """Gets the queue for a guild, creating it if it doesn't exist."""
        if guild_id not in self.queues:
            self.queues[guild_id] = deque()
        return self.queues[guild_id]

    def _format_duration(self, seconds: float) -> str:
        """Formats seconds into MM:SS or HH:MM:SS."""
        if seconds is None or not isinstance(seconds, (int, float)):
            return "??:??"
        try:
            seconds = int(seconds)
            minutes, seconds = divmod(seconds, 60)
            hours, minutes = divmod(minutes, 60)
            if hours > 0:
                return f"{hours:d}:{minutes:02d}:{seconds:02d}"
            else:
                return f"{minutes:02d}:{seconds:02d}"
        except Exception:
             logger.warning(f"Could not format duration for seconds: {seconds}")
             return "??:??"


    async def _extract_info(self, query):
        """Extracts info using yt-dlp in an executor to avoid blocking."""
        loop = asyncio.get_event_loop()
        try:
            logger.debug(f"Submitting yt-dlp extraction for '{query}' to process pool.")
            # Call the top-level function, passing the query as an argument
            data = await loop.run_in_executor(
                self.process_executor,
                run_yt_dlp_extractor,  # Pass the function itself
                query  # Pass the argument(s) for the function
            )
            logger.debug(f"Successfully retrieved data for '{query}' from process pool.")

            if not data:
                logger.warning(f"Extraction returned no data for '{query}'.")
                return None

            if 'entries' in data:
                logger.info(f"Found multiple entries for '{query}', using first result.")
                data = data['entries'][0]

            if 'url' not in data:
                logger.warning(f"Could not extract stream URL for '{query}'. Missing 'url' key. Data: {data}")
                return None

            # Prepare song info dictionary
            song_info = {
                'title': data.get('title', 'Unknown Title'),
                'url': data['url'], # The crucial stream URL
                'thumbnail': data.get('thumbnail'),
                'duration': data.get('duration'), # In seconds
                'webpage_url': data.get('webpage_url', query), # Link back to YT page
                'start_time': None # Will be set when playback actually starts
            }
            return song_info

        # This catches errors from within run_yt_dlp_extractor or pickling issues
        except Exception as e:
            # Check if it's the specific pickle error, though the fix should prevent it
            if "Can't pickle" in str(e):
                 logger.critical(f"Pickling error encountered despite fix attempt for '{query}': {e}", exc_info=True)
            # Catch errors from the yt-dlp process itself
            elif isinstance(e, yt_dlp.utils.DownloadError):
                 logger.error(f"yt-dlp DownloadError extracting info for '{query}': {e}")
            # Handle pool shutdown errors
            elif isinstance(e, concurrent.futures.process.BrokenProcessPool):
                 logger.error(f"Process Pool Broken during info extraction for '{query}'. It might be shutting down or crashed: {e}")
            else:
                # Log other unexpected exceptions from run_in_executor or within the target function
                logger.exception(f"Unexpected error during info extraction process for '{query}': {e}")
            return None # Return None on any extraction error


    def _play_next(self, guild_id, error=None):
        """Callback function executed after a song finishes or errors."""
        if error:
            logger.error(f'Player error in guild {guild_id}: {error}')
            # Potentially notify the channel about the error

        queue = self.get_queue(guild_id)
        if not queue:
            logger.info(f"Queue empty for guild {guild_id}.")
            self.current_song.pop(guild_id, None)
            # Start inactivity timer logic here by updating last_activity
            self.last_activity[guild_id] = time.time()
            # Don't disconnect immediately, let the loop handle it
            return

        # Get next song info from the queue
        next_song_info = queue.popleft()
        # Set start time *just before* playback
        next_song_info['start_time'] = time.time()
        next_song_info['accumulated_duration'] = 0
        next_song_info['is_paused'] = False
        self.current_song[guild_id] = next_song_info
        logger.info(f"Playing next song in guild {guild_id}: {next_song_info['title']}")
        logger.debug(f"Attempting to play next URL: {next_song_info['url']}")

        if guild_id not in self.voice_clients or not self.voice_clients[guild_id].is_connected():
            logger.warning(f"Voice client not available or disconnected in guild {guild_id} when trying to play next.")
            self.current_song.pop(guild_id, None) # Clear current song as we can't play
            return

        vc = self.voice_clients[guild_id]
        try:
            source = discord.FFmpegPCMAudio(next_song_info['url'], **FFMPEG_OPTIONS)
            # Wrap the source with PCMVolumeTransformer if you want volume control later
            # source = discord.PCMVolumeTransformer(source, volume=0.5)
            vc.play(source, after=lambda e: self._play_next(guild_id, e))
            self.last_activity[guild_id] = time.time() # Update activity time when song starts
        except discord.ClientException as e:
             logger.error(f"Discord ClientException while trying to play next in {guild_id}: {e}")
             # Try playing the next one in the queue if available
             self.current_song.pop(guild_id, None) # Clear failed song
             self._play_next(guild_id) # Recursive call to try next song
        except Exception as e:
            logger.exception(f"Unexpected error during playback setup in {guild_id}: {e}")
            self.current_song.pop(guild_id, None) # Clear failed song
            self._play_next(guild_id) # Try next song on unexpected error


    @commands.command(name='join', help='Joins the voice channel you are currently in.')
    async def join(self, ctx: commands.Context):
        """Joins the invoker's voice channel."""
        if ctx.author.voice is None:
            await ctx.send("You are not connected to a voice channel.")
            return

        channel = ctx.author.voice.channel
        guild_id = ctx.guild.id
        self.last_activity[guild_id] = time.time() # Update activity on join attempt

        if guild_id in self.voice_clients and self.voice_clients[guild_id].is_connected():
            if self.voice_clients[guild_id].channel == channel:
                await ctx.send("I am already in your voice channel.")
            else:
                await self.voice_clients[guild_id].move_to(channel)
                await ctx.send(f"Moved to {channel.mention}.")
        else:
            try:
                logger.info(f"Joining channel {channel.name} in guild {guild_id}")
                vc = await channel.connect()
                self.voice_clients[guild_id] = vc
                await ctx.send(f"Joined {channel.mention}!")
            except asyncio.TimeoutError:
                await ctx.send("Connecting to the voice channel timed out.")
                logger.error(f"Timeout connecting to {channel.name} in {guild_id}")
            except discord.ClientException as e:
                 await ctx.send(f"Error connecting to voice channel: {e}")
                 logger.error(f"ClientException connecting to {channel.name} in {guild_id}: {e}")
            except Exception as e:
                 await ctx.send("An unexpected error occurred while joining the channel.")
                 logger.exception(f"Unexpected error joining {channel.name} in {guild_id}: {e}")


    @commands.command(name='leave', help='Leaves the current voice channel.')
    async def leave(self, ctx: commands.Context):
        """Disconnects the bot from the voice channel."""
        guild_id = ctx.guild.id
        if guild_id in self.voice_clients and self.voice_clients[guild_id].is_connected():
            vc = self.voice_clients[guild_id]
            logger.info(f"Leaving channel {vc.channel.name} in guild {guild_id} by command.")
            await vc.disconnect()
            # Clean up state for this guild
            self.voice_clients.pop(guild_id, None)
            self.queues.pop(guild_id, None)
            self.current_song.pop(guild_id, None)
            self.last_activity.pop(guild_id, None)
            await ctx.send("Disconnected from the voice channel.")
        else:
            await ctx.send("I am not currently in a voice channel.")

    @commands.command(name='play', help='Plays a song from YouTube (URL or search query).')
    async def play(self, ctx: commands.Context, *, query: str):
        """Plays audio from a YouTube URL or search query."""
        guild_id = ctx.guild.id
        self.last_activity[guild_id] = time.time() # Update activity

        # Ensure user is in a voice channel
        if ctx.author.voice is None:
            await ctx.send("You need to be in a voice channel to play music.")
            return

        # Ensure bot is in a voice channel (or join the user's)
        user_channel = ctx.author.voice.channel
        if guild_id not in self.voice_clients or not self.voice_clients[guild_id].is_connected():
            logger.info(f"Play command used, joining {user_channel.name} in {guild_id}.")
            try:
                 self.voice_clients[guild_id] = await user_channel.connect()
            except Exception as e:
                 await ctx.send(f"Failed to join your voice channel: {e}")
                 logger.exception(f"Failed to join {user_channel.name} for play command.")
                 return
        elif self.voice_clients[guild_id].channel != user_channel:
             await ctx.send("You need to be in the same voice channel as the bot.")
             return

        vc = self.voice_clients[guild_id]
        queue = self.get_queue(guild_id)

        # Check if the query looks like a URL (starts with http)
        query_stripped = query.strip()  # Use strip() to handle leading/trailing spaces
        is_url = query_stripped.startswith("http://") or query_stripped.startswith("https://")

        # Extract song info - Send status message conditionally
        processing_message = None  # Keep track of the message if we send one
        if is_url:
            processing_message = await ctx.send(f"Processing URL...")
            logger.info(f"Processing direct URL: {query_stripped}")
            pass  # No searching message needed
        else:
            processing_message = await ctx.send(f"Searching for `{query_stripped}`...")

        # Pass the stripped query to the extractor
        song_info = await self._extract_info(query_stripped)

        if not song_info:
            await ctx.send(f"Could not find or process `{query}`. Please check the URL or search terms.")
            return

        try:
            # Call the method on the db_manager instance
            self.db_manager.log_song_request(
                user_id=ctx.author.id,
                user_name=str(ctx.author),  # Get username
                guild_id=ctx.guild.id,
                query=query_stripped,  # Use the original query
                resolved_title=song_info.get('title', 'N/A'),  # Get title from extracted info
                resolved_url=song_info.get('webpage_url')  # Get webpage_url
            )
        except Exception as e:
            # Log if the logging itself fails, but don't stop playback
            logger.error(f"Error occurred during song request logging via DB Manager: {e}", exc_info=True)

        # Check if currently playing, paused, OR if current_song is set (might be transitioning)
        is_active = vc.is_playing() or vc.is_paused() or (guild_id in self.current_song and self.current_song[guild_id] is not None)
        if is_active:
             queue.append(song_info)
             embed = discord.Embed(title="Added to Queue", description=f"[{song_info['title']}]({song_info['webpage_url']})", color=discord.Color.blue())
             if song_info.get('thumbnail'):
                 embed.set_thumbnail(url=song_info['thumbnail'])
             embed.add_field(name="Position in queue", value=len(queue))
             await ctx.send(embed=embed)
        else:
            # Set start time *just before* playback
            song_info['start_time'] = time.time()
            song_info['accumulated_duration'] = 0
            song_info['is_paused'] = False
            self.current_song[guild_id] = song_info
            logger.info(f"Playing immediately in guild {guild_id}: {song_info['title']}")
            logger.debug(f"Attempting to play URL: {song_info['url']}")
            try:
                source = discord.FFmpegPCMAudio(song_info['url'], **FFMPEG_OPTIONS)
                vc.play(source, after=lambda e: self._play_next(guild_id, e))

                embed = discord.Embed(title="Now Playing", description=f"[{song_info['title']}]({song_info['webpage_url']})", color=discord.Color.green())
                if song_info.get('thumbnail'):
                    embed.set_thumbnail(url=song_info['thumbnail'])
                if song_info.get('duration'):
                    embed.add_field(name="Duration", value=self._format_duration(song_info['duration']))
                await ctx.send(embed=embed)

            except discord.ClientException as e:
                await ctx.send(f"Error starting playback: {e}")
                logger.error(f"ClientException during initial play in {guild_id}: {e}")
                self.current_song.pop(guild_id, None) # Clear current song if playback failed
            except Exception as e:
                await ctx.send("An unexpected error occurred while trying to play.")
                logger.exception(f"Unexpected error during initial play in {guild_id}: {e}")
                self.current_song.pop(guild_id, None) # Clear current song

    @commands.command(name='pause', help='Pauses the currently playing song.')
    async def pause(self, ctx: commands.Context):
        """Pauses the currently playing song."""
        guild_id = ctx.guild.id
        self.last_activity[guild_id] = time.time() # Update activity

        if guild_id not in self.voice_clients or not self.voice_clients[guild_id].is_connected():
            await ctx.send("I'm not connected to a voice channel.")
            return

        vc = self.voice_clients[guild_id]
        current = self.current_song.get(guild_id)

        if not current:
            await ctx.send("I am not playing anything right now.")
            return

        if not vc.is_playing():
            # Check if it's already paused
            if current.get('is_paused'):
                 await ctx.send("The song is already paused.")
            else:
                 await ctx.send("I am not playing anything that can be paused (or it's already paused).")
            return

        if current.get('is_paused'): # Redundant check given vc.is_playing(), but good for internal state consistency
            await ctx.send("The song is already paused (internal state).")
            return

        # Update accumulated duration before pausing
        current['accumulated_duration'] += (time.time() - current['start_time'])
        vc.pause()
        current['is_paused'] = True
        self.current_song[guild_id] = current # Re-assign to ensure update if it was a copy

        logger.info(f"Paused song in guild {guild_id}: {current['title']}")
        await ctx.send(f"Paused: {current['title']}")

    @commands.command(name='resume', help='Resumes the currently paused song.')
    async def resume(self, ctx: commands.Context):
        """Resumes the currently paused song."""
        guild_id = ctx.guild.id
        self.last_activity[guild_id] = time.time() # Update activity

        if guild_id not in self.voice_clients or not self.voice_clients[guild_id].is_connected():
            await ctx.send("I'm not connected to a voice channel.")
            return

        vc = self.voice_clients[guild_id]
        current = self.current_song.get(guild_id)

        if not current:
            await ctx.send("Nothing is currently loaded to resume.")
            return

        if not current.get('is_paused'):
            if vc.is_playing():
                await ctx.send("The song is already playing.")
            else: # Not paused and not playing - might be stopped or in a weird state
                await ctx.send("The song is not paused (it might be stopped or finished).")
            return

        # At this point, current['is_paused'] should be True.
        # We also rely on vc.is_paused() to be true, which discord.py should ensure if we used vc.pause()
        if not vc.is_paused():
            logger.warning(f"Resume command in guild {guild_id}: Internal state 'is_paused' is True, but vc.is_paused() is False. Proceeding with resume logic.")
            # This case might indicate a desync, but we try to recover.

        current['start_time'] = time.time() # Reset start time for the new segment
        vc.resume()
        current['is_paused'] = False
        self.current_song[guild_id] = current # Re-assign

        logger.info(f"Resumed song in guild {guild_id}: {current['title']}")
        await ctx.send(f"Resumed: {current['title']}")


    @commands.command(name='skip', help='Skips the currently playing song.')
    async def skip(self, ctx: commands.Context):
        """Skips the current song."""
        guild_id = ctx.guild.id
        self.last_activity[guild_id] = time.time() # Update activity

        if guild_id not in self.voice_clients or not self.voice_clients[guild_id].is_connected():
            await ctx.send("I'm not connected to a voice channel.")
            return

        vc = self.voice_clients[guild_id]
        # Check actual playback state first
        if not vc.is_playing() and not vc.is_paused():
            await ctx.send("I am not playing anything right now.")
            return

        # Then check our internal state as a fallback/confirmation
        current = self.current_song.get(guild_id)
        if not current:
             await ctx.send("There's no song currently marked as playing, but attempting to stop.")
             # Try stopping anyway, might be in a weird state
             vc.stop() # Will trigger _play_next if successful
             return

        logger.info(f"Skipping song in guild {guild_id} by command: {current['title']}")
        await ctx.send(f"Skipping: {current['title']}")
        vc.stop()  # This will trigger the _play_next callback

    @commands.command(name='queue', aliases=['q'], help='Shows the current song queue.')
    async def queue(self, ctx: commands.Context):
        """Displays the song queue."""
        guild_id = ctx.guild.id
        self.last_activity[guild_id] = time.time() # Update activity

        queue = self.get_queue(guild_id)
        current = self.current_song.get(guild_id)

        if not current and not queue:
            await ctx.send("The queue is empty and nothing is playing.")
            return

        embed = discord.Embed(title="Music Queue", color=discord.Color.purple())

        if current:
             duration_str = self._format_duration(current.get('duration'))
             embed.add_field(name="Now Playing", value=f"[{current['title']}]({current['webpage_url']}) `[{duration_str}]`", inline=False)
        else:
             embed.add_field(name="Now Playing", value="Nothing currently playing.", inline=False)


        if queue:
            queue_list = ""
            # Limit display to avoid huge messages
            for i, song in enumerate(list(queue)[:10]): # Show first 10 songs
                duration_str = self._format_duration(song.get('duration'))
                queue_list += f"{i + 1}. [{song['title']}]({song['webpage_url']}) `[{duration_str}]`\n"
            if len(queue) > 10:
                 queue_list += f"\n... and {len(queue) - 10} more."

            embed.add_field(name="Up Next", value=queue_list if queue_list else "Queue is empty.", inline=False)
        else:
             embed.add_field(name="Up Next", value="Queue is empty.", inline=False)

        await ctx.send(embed=embed)

    @commands.command(name='nowplaying', aliases=['np'], help='Shows the currently playing song and its progress.')
    async def nowplaying(self, ctx: commands.Context):
        """Displays the current song and playback progress."""
        guild_id = ctx.guild.id
        self.last_activity[guild_id] = time.time() # Update activity

        vc = self.voice_clients.get(guild_id)
        current = self.current_song.get(guild_id)

        # Check if connected and if something is marked as current
        if not vc or not vc.is_connected() or not current:
            await ctx.send("I am not playing anything right now.")
            return

        # Check if actually playing or paused (more accurate state)
        if not vc.is_playing() and not vc.is_paused():
             await ctx.send("I am not playing anything right now (playback state inactive).")
             # Clear potentially stale current song info if state mismatch
             if guild_id in self.current_song:
                 logger.warning(f"Clearing stale current_song entry for guild {guild_id} due to inactive playback state.")
                 self.current_song.pop(guild_id, None)
             return

        # current is already current_song_info from self.current_song.get(guild_id)
        total_duration = current.get('duration')
        title = current.get('title', 'Unknown Title')
        webpage_url = current.get('webpage_url', '')
        thumbnail = current.get('thumbnail')

        progress_str = ""
        elapsed_seconds = 0
        state = "Playing" # Default state

        if current.get('is_paused'):
            elapsed_seconds = current.get('accumulated_duration', 0)
            state = "Paused"
        elif current.get('start_time'): # Playing
            elapsed_seconds = current.get('accumulated_duration', 0) + (time.time() - current['start_time'])
            state = "Playing"
        else: # Should not happen if current is valid and song is loaded
            progress_str = "Progress unavailable (state error)"


        if total_duration: # Ensure total_duration is available for progress calculation
            elapsed_seconds = max(0, min(elapsed_seconds, total_duration)) # Clamp
            formatted_elapsed = self._format_duration(elapsed_seconds)
            formatted_total = self._format_duration(total_duration)
            progress_str = f"{formatted_elapsed} / {formatted_total}"

            bar_length = 20  # characters
            progress_ratio = elapsed_seconds / total_duration if total_duration > 0 else 0
            filled_length = int(bar_length * progress_ratio)
            bar = '█' * filled_length + '░' * (bar_length - filled_length)
            progress_str += f"\n`[{bar}]`"
        elif progress_str == "": # If not set by state error and no total_duration
            progress_str = "Duration info missing"


        embed = discord.Embed(title=f"{state}: {title}", description=f"[{title}]({webpage_url})", color=discord.Color.green() if state == "Playing" else discord.Color.orange())
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)

        embed.add_field(name="Progress", value=progress_str, inline=False)

        await ctx.send(embed=embed)

    @commands.command(name='remove', help='Removes a song from the queue by its number (use !queue to see numbers).')
    async def remove(self, ctx: commands.Context, position: int):
        """Removes a song from the queue specified by its 1-based position."""
        guild_id = ctx.guild.id
        self.last_activity[guild_id] = time.time() # Update activity

        queue = self.get_queue(guild_id)

        if not queue:
            await ctx.send("The queue is currently empty.")
            return

        # Adjust position to be 0-based index for the deque
        index_to_remove = position - 1

        if 0 <= index_to_remove < len(queue):
            try:
                # Convert deque to list temporarily for indexed removal if needed,
                # though deques support `del queue[index]`
                removed_song_info = queue[index_to_remove] # Access item by index
                del queue[index_to_remove] # Deques support deletion by index

                logger.info(f"Removed song at position {position} in guild {guild_id}: {removed_song_info['title']}")
                await ctx.send(f"Removed song #{position}: **{removed_song_info['title']}**")

            except IndexError:
                 await ctx.send("An error occurred trying to remove that song. The queue might have changed.")
                 logger.warning(f"IndexError during remove command for position {position} in guild {guild_id}.")
            except Exception as e:
                 await ctx.send("An unexpected error occurred while trying to remove the song.")
                 logger.exception(f"Unexpected error in remove command for guild {guild_id}: {e}")
        else:
            await ctx.send(f"Invalid song number. Please provide a number between 1 and {len(queue)}.")

    @remove.error
    async def remove_error(self, ctx: commands.Context, error: commands.CommandError):
        """Handles errors specifically for the !remove command."""
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("You need to specify the number of the song to remove. Use `!queue` to see the numbers.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("Invalid input. Please provide a valid number for the song position.")
        else:
            logger.error(f"An unexpected error occurred in the remove command: {error}")
            # Optional: send a generic error message
            # await ctx.send("An unexpected error occurred processing the remove command.")

    @commands.command(name='clear', help='Clears the song queue.')
    async def clear(self, ctx: commands.Context):
        """Clears all songs from the queue."""
        guild_id = ctx.guild.id
        self.last_activity[guild_id] = time.time() # Update activity

        queue = self.get_queue(guild_id)
        if not queue:
            await ctx.send("The queue is already empty.")
            return

        queue.clear()
        await ctx.send("Song queue cleared!")
        logger.info(f"Queue cleared for guild {guild_id} by command.")

    @commands.command(name='stats', help='Shows song request stats for a user (or yourself).')
    async def stats(self, ctx: commands.Context, *, member: discord.Member = None):
        """Shows the total number of songs requested by the specified user or yourself."""
        target_user = member or ctx.author

        logger.info(f"Stats command invoked by {ctx.author} for user {target_user}")

        # --- Fetch stats using the DatabaseManager ---
        try:
            # Call the method on the db_manager instance
            request_count = self.db_manager.get_user_stats(target_user.id)
        except Exception as e:
             logger.error(f"Error getting stats via DB Manager for user {target_user.id}: {e}", exc_info=True)
             await ctx.send("An error occurred while fetching stats.")
             return

        # Send the result
        await ctx.send(f"📊 **{target_user.display_name}** has requested **{request_count}** track(s).")

    @stats.error
    async def stats_error(self, ctx: commands.Context, error: commands.CommandError):
        """Handles errors for the !stats command."""
        if isinstance(error, commands.MemberNotFound):
            # 'argument' contains the raw string input that failed conversion
            user_input = error.argument
            await ctx.send(
                f"Could not find a member matching '{user_input}' in this server. Please use their @mention, username#discriminator, or user ID.")
            # You might want to add self.logger.warning here too
            logger.warning(f"MemberNotFound error in stats command: Input='{user_input}', Guild='{ctx.guild.id}'")
        elif isinstance(error, commands.CommandInvokeError):
            # This catches errors *inside* the stats command logic (e.g., database errors that weren't caught)
            logger.error(f"Error during stats command execution: {error.original}", exc_info=True)
            await ctx.send("An unexpected error occurred while processing the stats command.")
            # Mark the error as handled if you have a generic cog error handler
            # error.original.handled = True # Add this if your generic handler shouldn't also report this
        else:
            # Handle other potential errors specific to this command if needed
            logger.error(f"Unhandled error in stats command: {error}", exc_info=True)
            await ctx.send("An error occurred processing the stats command.")

    @commands.command(name='leaderboard', aliases=['lb'], help='Shows the top 5 song requesters in this server.')
    async def leaderboard(self, ctx: commands.Context):
        """Displays the top 5 users by song request count for this server."""
        logger.info(f"Leaderboard command invoked by {ctx.author} in guild {ctx.guild.id}")

        try:
            top_users_data = self.db_manager.get_leaderboard_stats(guild_id=ctx.guild.id, limit=5)
        except Exception as e:
            logger.error(f"Error fetching leaderboard data via DB Manager: {e}", exc_info=True)
            await ctx.send("An error occurred while fetching the leaderboard.")
            return

        if not top_users_data:
            await ctx.send("No song request data available yet for this server to generate a leaderboard.")
            return

        embed = discord.Embed(
            title="🏆 Top Song Requesters 🏆",
            color=discord.Color.gold()
        )

        description_lines = []
        rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}

        for i, user_data in enumerate(top_users_data):
            rank = i + 1
            user_id = user_data['user_id']
            db_user_name = user_data['user_name']  # Fallback name from DB
            request_count = user_data['request_count']

            # Try to find the member in the current guild for up-to-date name
            member = ctx.guild.get_member(user_id)
            display_name = member.display_name if member else db_user_name
            # Add "(Not Found)" if member left server but is on leaderboard
            not_found_tag = "" if member else " *(user not in server)*"

            rank_display = rank_emojis.get(rank, f"{rank}.")  # Use emoji or just rank number
            line = f"{rank_display} **{discord.utils.escape_markdown(display_name)}**{not_found_tag}: **{request_count}** requests"
            description_lines.append(line)

        embed.description = "\n".join(description_lines)
        embed.set_footer(text="Based on total songs requested via the bot on this server.")

        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState):
        """Checks if the bot should disconnect when a voice channel becomes empty."""
        if member.id == self.bot.user.id:
            return

        guild_id = member.guild.id
        vc = self.voice_clients.get(guild_id)
        if not vc or not vc.is_connected():
            return

        # Check if the event happened in the bot's current channel
        if before.channel == vc.channel and after.channel != vc.channel: # User left the bot's channel
             # Check who is left in the channel
            human_members = [m for m in vc.channel.members if not m.bot]

            if not human_members:
                logger.info(f"Voice channel {vc.channel.name} in guild {guild_id} is empty. Scheduling disconnect.")
                # Introduce a small delay before disconnecting
                # This helps prevent race conditions if someone quickly rejoins
                await asyncio.sleep(10) # Wait 10 seconds

                # Re-check after the delay if the bot is still connected and channel still empty
                vc = self.voice_clients.get(guild_id) # Get potentially updated vc state
                if vc and vc.is_connected() and before.channel == vc.channel: # Ensure we're still in the *same* channel
                     current_human_members = [m for m in vc.channel.members if not m.bot]
                     if not current_human_members:
                         logger.info(f"Disconnecting from empty channel {vc.channel.name} in guild {guild_id} after delay.")
                         if vc.is_playing() or vc.is_paused():
                             vc.stop()
                         await vc.disconnect()
                         self.voice_clients.pop(guild_id, None)
                         self.queues.pop(guild_id, None)
                         self.current_song.pop(guild_id, None)
                         self.last_activity.pop(guild_id, None)
                     else:
                          logger.info(f"Disconnect cancelled for guild {guild_id}, user rejoined.")
                else:
                     logger.info(f"Disconnect cancelled for guild {guild_id}, state changed during delay.")


    # --- Inactivity Check Task ---
    @tasks.loop(minutes=1.0) # Check every minute
    async def inactivity_check(self):
        """Periodically checks for inactive voice clients and disconnects them."""
        now = time.time()
        inactive_threshold = INACTIVITY_TIMEOUT_MINUTES * 60

        # Iterate over a copy of keys to allow modification during iteration
        for guild_id in list(self.voice_clients.keys()):
            vc = self.voice_clients.get(guild_id)
            last_act = self.last_activity.get(guild_id)

            # Check if VC exists, is connected, is not playing/paused, and has activity tracked
            if vc and vc.is_connected() and not vc.is_playing() and not vc.is_paused() and last_act:
                if (now - last_act) > inactive_threshold:
                    logger.info(f"Disconnecting from guild {guild_id} due to inactivity.")
                    await vc.disconnect()
                    # Clean up state for this guild
                    self.voice_clients.pop(guild_id, None)
                    self.queues.pop(guild_id, None)
                    self.current_song.pop(guild_id, None)
                    self.last_activity.pop(guild_id, None)
                    # Consider sending a message to a default channel if possible
            elif vc and vc.is_connected() and (vc.is_playing() or vc.is_paused()):
                # If playing or paused, reset the inactivity timer
                self.last_activity[guild_id] = now


    @inactivity_check.before_loop
    async def before_inactivity_check(self):
        """Ensures the bot is ready before the loop starts."""
        await self.bot.wait_until_ready()
        logger.info("Inactivity check loop ready.")

    # --- Cog Error Handling ---
    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Handles errors specific to this cog."""
        # Ignore specific errors handled locally
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)) and ctx.command.name == 'remove':
             return # Already handled by remove_error

        logger.error(f"Error in command '{ctx.command.qualified_name if ctx.command else 'Unknown'}': {error}")

        if isinstance(error, commands.CommandNotFound):
            await ctx.send("Invalid command. Use `!help` to see available commands.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing argument: `{error.param.name}`. Use `!help {ctx.command.qualified_name}` for usage.")
        elif isinstance(error, commands.BadArgument):
             await ctx.send(f"Invalid argument provided. Use `!help {ctx.command.qualified_name}` for usage.")
        elif isinstance(error, commands.CheckFailure):
            await ctx.send("You don't have the necessary permissions to use this command.")
        elif isinstance(error, commands.CommandInvokeError):
            # More serious errors during command execution
             await ctx.send(f"An error occurred while executing the command. Please check the logs or contact the admin. Error: {error.original}")
             logger.exception(f"CommandInvokeError in {ctx.command.qualified_name}: {error.original}")
        else:
            # Generic error message for other cases
            await ctx.send(f"An unexpected error occurred: {error}")


# --- Bot Event Handlers ---
@bot.event
async def on_ready():
    """Called when the bot is ready and connected to Discord."""
    logger.info(f'Logged in as {bot.user.name} ({bot.user.id})')
    logger.info('Bot is ready and online.')
    # Store the cog instance to potentially unload it later
    music_cog_instance = MusicCog(bot)
    await bot.add_cog(music_cog_instance)
    print(f'Bot {bot.user.name} is ready.')
    await bot.change_presence(activity=discord.Game(name="Music | !help")) # Set status


# --- Run the Bot ---
if __name__ == "__main__":
    if not DISCORD_TOKEN or DISCORD_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("CRITICAL ERROR: DISCORD_BOT_TOKEN not found or not set correctly in environment variables/.env file.")
        exit()
    
    # --- Check for FFmpeg before starting the bot ---
    if not check_ffmpeg(FFMPEG_EXECUTABLE):
        print(f"CRITICAL ERROR: FFmpeg not found or not working (tried path: {FFMPEG_EXECUTABLE}).")
        print("Music playback will fail. Please install FFmpeg and ensure it's in your system's PATH,")
        print("or configure the FFMPEG_EXECUTABLE_PATH in your .env file if it's installed elsewhere.")
        exit() # Exit if FFmpeg check fails

    else:
        try:
            logger.info("Starting bot...")
            bot.run(DISCORD_TOKEN, log_handler=None) # Use our basicConfig
        except discord.LoginFailure:
             logger.error("Login failed: Invalid Discord token provided.")
             print("CRITICAL ERROR: Invalid Discord Token. Please check your .env file or environment variable.")
        except KeyboardInterrupt:
             logger.info("Bot shutdown requested via KeyboardInterrupt.")
             # Note: bot.close() should ideally be called for clean async shutdown,
             # but bot.run handles Ctrl+C fairly well by itself, triggering cleanup.
        except Exception as e:
             logger.exception(f"Fatal error during bot execution: {e}")
             print(f"FATAL ERROR: {e}")
        finally:
            logger.info("Bot process finished.")