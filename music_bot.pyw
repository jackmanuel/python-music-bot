import discord
from discord.ext import commands, tasks
import yt_dlp
import nacl
import asyncio
import logging
import time
from collections import deque
import os
from dotenv import load_dotenv
import concurrent.futures
from aiohttp import web

from database_manager import DatabaseManager
from logging_config import setup_logging

# --- Load environment variables from .env file ---
load_dotenv()

# --- Configuration ---
# Load token securely from environment variable
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not DISCORD_TOKEN:
    print("CRITICAL ERROR: DISCORD_BOT_TOKEN not found in environment variables.")
    print("Make sure you have a .env file with DISCORD_BOT_TOKEN=YOUR_TOKEN")
    exit()

# --- Load FFmpeg Path from environment variable ---
# Get the path from .env, defaulting to just "ffmpeg" if the variable is not set.
# This allows it to still work if FFmpeg is in the system PATH and the .env variable isn't defined.
FFMPEG_EXECUTABLE = os.getenv("FFMPEG_EXECUTABLE_PATH", "ffmpeg")
FFMPEG_VOLUME = "0.5"

INACTIVITY_TIMEOUT_MINUTES = 20 # Minutes before leaving the voice channel due to inactivity

# --- Song Duration Limit ---
# Maximum song duration in seconds (default: 30 minutes = 1800 seconds)
# Songs longer than this will be rejected to save performance and disk space
MAX_SONG_DURATION_SECONDS = int(os.getenv("MAX_SONG_DURATION_SECONDS", "1800"))

# --- Database Configuration ---
DATABASE_FILE = os.getenv("DATABASE_FILE_PATH", "database/music_log.db")
LOG_FILE = os.getenv("LOG_FILE_PATH", "logs/music_bot.log")

SERVER_HOST = "localhost"
SERVER_PORT = 8000

# --- Basic Logging ---
setup_logging(log_file_path=LOG_FILE)
logger = logging.getLogger(__name__)

# Add a log message to confirm which FFmpeg path is being used
logger.info(f"Using FFmpeg executable located at: {FFMPEG_EXECUTABLE}")
logger.info(f"Database file located at: {os.path.abspath(DATABASE_FILE)}")
logger.info(f"Log file located at: {os.path.abspath(LOG_FILE)}")

# Format the duration for logging
def format_duration_log(seconds: float) -> str:
    """Formats seconds into MM:SS or HH:MM:SS for logging."""
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
         return "??:??"

logger.info(f"Maximum song duration limit: {MAX_SONG_DURATION_SECONDS} seconds ({format_duration_log(MAX_SONG_DURATION_SECONDS)})")

# --- yt-dlp Options ---
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'outtmpl': 'song_cache/%(extractor)s-%(id)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': False,
    'no_warnings': False,
    'default_search': 'ytsearch1:',
    'source_address': '0.0.0.0',
    'subtitles': False,
    'writethumbnail': False,
    'remote_components': 'ejs:github',
    'extractor_args': {
        'youtube': {
            'player_client': ['default', '-android_sdkless']
        }
    }
}

# --- FFmpeg Options ---
# -before_options: Handle reconnections before decoding starts
# -reconnect 1: Enable reconnection
# -reconnect_streamed 1: Enable reconnection on streamed URLs
# -reconnect_delay_max 5: Maximum delay before reconnect attempt
# -vn: Disable video processing
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -f s16le -ar 48000 -ac 2',
    'executable': FFMPEG_EXECUTABLE
}


def run_yt_dlp_extractor(query, download=False):
    """
    Runs yt-dlp extract_info in a way that's pickleable for multiprocessing.
    Needs YDL_OPTIONS to be globally accessible or passed explicitly if refactored.
    """
    try:
        # Create a custom logger for yt-dlp to capture its output
        ydl_logger = logging.getLogger('yt-dlp')
        ydl_logger.setLevel(logging.DEBUG)
        
        # Create a copy of YDL_OPTIONS to avoid modifying the global one
        ydl_options = YDL_OPTIONS.copy()
        ydl_options['logger'] = ydl_logger
        
        # NOTE: Creates a new YoutubeDL instance each time in the new process.
        # This is generally fine for ProcessPoolExecutor.
        with yt_dlp.YoutubeDL(ydl_options) as ydl:
            data = ydl.extract_info(query, download=download)
        return data
    except Exception as e:
        # Log or handle errors occurring *within* the worker process if necessary
        # For simplicity, we'll let the main process catch it via the future
        logger.error(f"Error within run_yt_dlp_extractor for '{query}': {e}")
        # Re-raise or return an indicator if needed, but often letting the
        # executor raise it in the main thread is sufficient.
        raise # Re-raise the exception to be caught by the await call

def run_yt_dlp_search(query):
    """
    Runs yt-dlp to search for a video without downloading.
    This is optimized for just getting video information.
    """
    try:
        # Create a custom logger for yt-dlp to capture its output
        ydl_logger = logging.getLogger('yt-dlp')
        ydl_logger.setLevel(logging.DEBUG)
        
        # Create search-specific options - no download, no post-processing
        search_options = {
            'format': 'bestaudio/best',
            'restrictfilenames': True,
            'noplaylist': True,
            'nocheckcertificate': True,
            'ignoreerrors': False,
            'logtostderr': False,
            'quiet': False,
            'no_warnings': False,
            'default_search': 'ytsearch1:',
            'source_address': '0.0.0.0',
            'logger': ydl_logger,
            # No postprocessors for search only
        }
        
        with yt_dlp.YoutubeDL(search_options) as ydl:
            data = ydl.extract_info(query, download=False)
        return data
    except Exception as e:
        logger.error(f"Error within run_yt_dlp_search for '{query}': {e}")
        raise


# --- Bot Class ---
intents = discord.Intents.default()
intents.message_content = True  # Enable message content intent
intents.voice_states = True     # Enable voice state intent for joining/leaving tracking
intents.members = True # Needed for stats command

bot = commands.Bot(command_prefix="!", intents=intents, case_insensitive=True)

# --- Music Cog ---
class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queues = {}  # Dictionary to hold queues for each guild {guild_id: deque()}
        self.current_song = {} # Dictionary to hold current song info {guild_id: song_info}
        self.voice_clients = {} # Dictionary to hold voice clients {guild_id: voice_client}
        self.last_activity = {} # Dictionary to track last activity time {guild_id: timestamp}
        self.song_cache = {}  # Dictionary to hold cached song info {youtube_id: file_path}
        # Use 1 quarter the cores, minimum 1
        cpu_cores = os.cpu_count() or 1
        max_workers = max(1, cpu_cores // 4)
        logger.info(f"Initializing ProcessPoolExecutor with max_workers={max_workers}")
        self.process_executor = concurrent.futures.ProcessPoolExecutor(max_workers=max_workers)
        # ThreadPoolExecutor for download operations - avoids pickling issues with yt-dlp
        # when downloading (especially for SoundCloud, where yt-dlp returns unpickleable objects)
        self.thread_executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

        self.db_manager = DatabaseManager(DATABASE_FILE)
        
        # Load existing cache
        self._load_cache()

        self.inactivity_check.start()

    def cog_unload(self):
        logger.info("Shutting down ProcessPoolExecutor...")
        self.process_executor.shutdown(wait=True)
        logger.info("Shutting down ThreadPoolExecutor...")
        self.thread_executor.shutdown(wait=True)
        logger.info("Cancelling inactivity check task.")
        self.inactivity_check.cancel()
    
    def _load_cache(self):
        """Load existing song cache from the song_cache directory."""
        cache_dir = "song_cache"
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)
            return
            
        logger.info("Loading existing song cache...")
        # Supported audio extensions
        valid_extensions = ('.opus', '.m4a', '.webm', '.mp3', '.aac', '.wav')
        
        for filename in os.listdir(cache_dir):
            if filename.endswith(valid_extensions):
                # Extract YouTube ID from filename (format: extractor-id.ext)
                # We need to handle IDs that might have hyphens, so split by extension first
                try:
                    name_part = os.path.splitext(filename)[0]
                    # Assuming format extractor-id
                    parts = name_part.split('-')
                    if len(parts) >= 2:
                        # Join all parts except the first one (extractor) to get the ID
                        # This handles IDs with hyphens better
                        youtube_id = "-".join(parts[1:]) 
                        file_path = os.path.join(cache_dir, filename)
                        self.song_cache[youtube_id] = file_path
                except Exception as e:
                    logger.error(f"Error loading cache file {filename}: {e}")
                    
        logger.info(f"Loaded {len(self.song_cache)} songs from cache")
    
    def _get_cached_file(self, youtube_id):
        """Check if a song is cached and return the file path."""
        return self.song_cache.get(youtube_id)
    
    def _add_to_cache(self, youtube_id, file_path):
        """Add a song to the cache."""
        self.song_cache[youtube_id] = file_path
        logger.info(f"Added song {youtube_id} to cache")

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


    async def _extract_info(self, query, download=False):
        """Extracts info using yt-dlp in an executor to avoid blocking."""
        loop = asyncio.get_event_loop()
        try:
            # First, search for the video without downloading
            logger.debug(f"Submitting yt-dlp search for '{query}' to process pool.")
            data = await loop.run_in_executor(
                self.process_executor,
                run_yt_dlp_search,
                query
            )
            logger.debug(f"Successfully retrieved search data for '{query}' from process pool.")

            if not data:
                logger.warning(f"Search returned no data for '{query}'.")
                return None

            if 'entries' in data:
                logger.info(f"Found multiple entries for '{query}', using first result.")
                data = data['entries'][0]

            # Check song duration against the maximum limit
            duration = data.get('duration')
            if duration and duration > MAX_SONG_DURATION_SECONDS:
                logger.warning(f"Song '{data.get('title', 'Unknown')}' exceeds maximum duration limit "
                             f"({duration}s > {MAX_SONG_DURATION_SECONDS}s)")
                return {
                    'error': 'duration_exceeded',
                    'title': data.get('title', 'Unknown Title'),
                    'duration': duration,
                    'max_duration': MAX_SONG_DURATION_SECONDS,
                    'webpage_url': data.get('webpage_url', query)
                }

            # Get YouTube ID for caching
            youtube_id = data.get('id')
            if not youtube_id:
                logger.warning(f"Could not extract YouTube ID for '{query}'.")
                return None

            # Check if already cached
            cached_file = self._get_cached_file(youtube_id)
            if cached_file and os.path.exists(cached_file):
                logger.info(f"Using cached file for '{query}': {cached_file}")
                # Prepare song info dictionary with cached file
                song_info = {
                    'title': data.get('title', 'Unknown Title'),
                    'url': cached_file,  # Use local file path instead of stream URL
                    'thumbnail': data.get('thumbnail'),
                    'duration': data.get('duration'),
                    'webpage_url': data.get('webpage_url', query),
                    'channel': data.get('channel', 'Unknown Channel'),
                    'youtube_id': youtube_id,
                    'start_time': None,
                    'is_cached': True,
                    'was_previously_cached': True  # Track if it was already cached
                }
                return song_info

            # If not cached and download is requested, download the file
            if download:
                logger.info(f"Downloading '{query}' to cache...")
                # Use ThreadPoolExecutor for downloads to avoid pickling issues
                downloaded_data = await loop.run_in_executor(
                    self.thread_executor,
                    run_yt_dlp_extractor,
                    query,
                    True
                )
                
                # Dynamic filename check
                # When downloading from a search, yt-dlp returns the search wrapper in 'entries'
                # We need to get the actual video entry for correct extractor/extension info
                actual_video_data = downloaded_data
                if 'entries' in downloaded_data and downloaded_data['entries']:
                    actual_video_data = downloaded_data['entries'][0]
                
                # yt-dlp usually returns the 'ext' it decided on in actual_video_data
                ext = actual_video_data.get('ext', 'opus') # Default fallback
                
                # Get the extractor name from yt-dlp (e.g., 'youtube', 'soundcloud')
                # The outtmpl uses %(extractor)s-%(id)s.%(ext)s format
                # Use the actual video's extractor, not the search wrapper's extractor
                extractor_name = actual_video_data.get('extractor', 'youtube')
                
                # Construct the expected filename pattern
                # Note: yt-dlp might sanitize the filename, but our outtmpl is simple
                expected_filename_base = f"{extractor_name}-{youtube_id}"
                
                found_file = None
                # Check for the file with the expected extension, or scan for it
                potential_path = os.path.join("song_cache", f"{expected_filename_base}.{ext}")
                
                if os.path.exists(potential_path):
                    found_file = potential_path
                else:
                    # Fallback: Scan directory for the file we just downloaded
                    for fname in os.listdir("song_cache"):
                        if fname.startswith(expected_filename_base):
                            found_file = os.path.join("song_cache", fname)
                            break
                
                if found_file:
                    self._add_to_cache(youtube_id, found_file)
                    logger.info(f"Successfully downloaded and cached '{query}' at {found_file}")
                    
                    song_info = {
                        'title': data.get('title') or downloaded_data.get('title', 'Unknown Title'),
                        'url': found_file,
                        'thumbnail': data.get('thumbnail') or downloaded_data.get('thumbnail'),
                        'duration': data.get('duration') or downloaded_data.get('duration'),
                        'webpage_url': data.get('webpage_url') or downloaded_data.get('webpage_url', query),
                        'channel': data.get('channel') or downloaded_data.get('channel', 'Unknown Channel'),
                        'youtube_id': youtube_id,
                        'start_time': None,
                        'is_cached': True,
                        'was_previously_cached': False
                    }
                    return song_info
                else:
                    logger.error(f"Downloaded file not found. Expected base: {expected_filename_base}")
                    return None
            else:
                # If not cached and download is NOT requested, return metadata
                # This allows the caller (play command) to know what song was found
                # and then decide to download it.
                return {
                    'title': data.get('title', 'Unknown Title'),
                    'url': data.get('url'), # Might be None, but that's okay for now
                    'thumbnail': data.get('thumbnail'),
                    'duration': data.get('duration'),
                    'webpage_url': data.get('webpage_url', query),
                    'channel': data.get('channel', 'Unknown Channel'),
                    'youtube_id': youtube_id,
                    'start_time': None,
                    'is_cached': False,
                    'was_previously_cached': False
                }

        # This catches errors from within run_yt_dlp_search or run_yt_dlp_extractor
        except Exception as e:
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
            return None


    def _play_next(self, guild_id, error=None):
        """Callback function executed after a song finishes or errors."""
        if error:
            logger.error(f'Player error in guild {guild_id}: {error}')
            # Potentially notify the channel about the error

        # Check if the song was intentionally skipped
        was_skipped = self.current_song.get(guild_id, {}).get('was_skipped', False)
        
        # If the song finished naturally (wasn't skipped), mark it as completed
        if not was_skipped and self.current_song.get(guild_id):
            request_id = self.current_song[guild_id].get('request_id')
            if request_id:
                self.db_manager.update_play_status(request_id, 'completed')

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
        next_song_info['start_time'] = time.time()
        self.current_song[guild_id] = next_song_info
        if 'request_id' in next_song_info:
            self.db_manager.update_play_start_timestamp(next_song_info['request_id'])
            self.db_manager.update_play_status(next_song_info['request_id'], 'playing')
        logger.info(f"Playing next song in guild {guild_id}: {next_song_info['title']}")
        logger.debug(f"Attempting to play next URL: {next_song_info['url']}")

        if guild_id not in self.voice_clients or not self.voice_clients[guild_id].is_connected():
            logger.warning(f"Voice client not available or disconnected in guild {guild_id} when trying to play next.")
            self.current_song.pop(guild_id, None) # Clear current song as we can't play
            return

        vc = self.voice_clients[guild_id]
        try:
            # Use different FFmpeg options for local files vs streaming
            if next_song_info.get('is_cached', False):
                local_ffmpeg_options = {
                    'before_options': '-re -nostdin -loglevel error',
                    'options': f'-vn -f s16le -ar 48000 -ac 2 -af "volume={FFMPEG_VOLUME}"',
                    'executable': FFMPEG_EXECUTABLE
                }
                source = discord.FFmpegPCMAudio(next_song_info['url'], **local_ffmpeg_options)
            else:
                source = discord.FFmpegPCMAudio(next_song_info['url'], **FFMPEG_OPTIONS)
            
            vc.play(source, after=lambda e: self._play_next(guild_id, error=e))
            self.last_activity[guild_id] = time.time() # Update activity time when song starts
        except discord.ClientException as e:
             logger.error(f"Discord ClientException while trying to play next in {guild_id}: {e}")
             self.current_song.pop(guild_id, None) # Clear failed song
             self._play_next(guild_id) # Recursive call to try next song
        except Exception as e:
            logger.exception(f"Unexpected error during playback setup in {guild_id}: {e}")
            self.current_song.pop(guild_id, None) # Clear failed song
            self._play_next(guild_id) # Try next song on unexpected error


    @commands.command(name='join', help='Joins the voice channel you are currently in.')
    async def join(self, ctx: commands.Context):
        """Joins the invoker's voice channel."""
        logger.info(f"'join' command invoked by '{ctx.author}' in guild '{ctx.guild.name}' ({ctx.guild.id})")
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
        logger.info(f"'leave' command invoked by '{ctx.author}' in guild '{ctx.guild.name}' ({ctx.guild.id})")
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

    @commands.command(name='play', help='Plays a song from YouTube or SoundCloud (URL or search query).')
    async def play(self, ctx: commands.Context, *, query: str):
        """Plays audio from a YouTube or SoundCloud URL, or searches YouTube for a query."""
        logger.info(f"'play' command invoked by '{ctx.author}' in guild '{ctx.guild.name}' ({ctx.guild.id}) with query: {query}")
        guild_id = ctx.guild.id
        self.last_activity[guild_id] = time.time() # Update activity

        # Ensure user is in a voice channel
        if ctx.author.voice is None:
            await ctx.send("You need to be in a voice channel to play music.")
            return

        # Ensure bot is in a voice channel (or join the user's)
        user_channel = ctx.author.voice.channel
        
        # Check if we have a stale voice client reference (bot thinks it's not connected,
        # but Discord still has an active/reconnecting voice client for this guild)
        existing_guild_vc = ctx.guild.voice_client
        our_vc = self.voice_clients.get(guild_id)
        
        # Determine if we need to connect
        need_to_connect = False
        if our_vc is None or not our_vc.is_connected():
            # Our reference is missing or stale
            if existing_guild_vc is not None:
                # Discord still has a voice client for us - could be reconnecting or in a weird state
                if existing_guild_vc.is_connected():
                    # Reuse the existing connection
                    logger.info(f"Reusing existing guild voice client for {guild_id}.")
                    self.voice_clients[guild_id] = existing_guild_vc
                else:
                    # It exists but isn't connected - clean it up and reconnect
                    logger.info(f"Cleaning up stale voice client for {guild_id} before reconnecting.")
                    try:
                        await existing_guild_vc.disconnect(force=True)
                    except Exception as cleanup_error:
                        logger.warning(f"Error during stale voice client cleanup: {cleanup_error}")
                    self.voice_clients.pop(guild_id, None)
                    need_to_connect = True
            else:
                need_to_connect = True
        
        if need_to_connect:
            logger.info(f"Play command used, joining {user_channel.name} in {guild_id}.")
            try:
                self.voice_clients[guild_id] = await user_channel.connect()
            except discord.ClientException as e:
                # Handle "Already connected" edge case - try to recover
                if "Already connected" in str(e):
                    logger.warning(f"Already connected error in {guild_id}, attempting to recover...")
                    existing_vc = ctx.guild.voice_client
                    if existing_vc:
                        self.voice_clients[guild_id] = existing_vc
                        logger.info(f"Recovered existing voice client for {guild_id}.")
                    else:
                        await ctx.send("Having trouble connecting to voice. Please try again in a moment.")
                        logger.error(f"Could not recover voice client for {guild_id}.")
                        return
                else:
                    await ctx.send(f"Failed to join your voice channel: {e}")
                    logger.exception(f"Failed to join {user_channel.name} for play command.")
                    return
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
        is_soundcloud = is_url and "soundcloud.com" in query_stripped.lower()

        # Extract song info - Send status message conditionally
        processing_message = None  # Keep track of the message if we send one
        if is_url:
            if is_soundcloud:
                processing_message = await ctx.send(f"Processing SoundCloud URL...")
                logger.info(f"Processing SoundCloud URL: {query_stripped}")
            else:
                processing_message = await ctx.send(f"Processing URL...")
                logger.info(f"Processing direct URL: {query_stripped}")
            pass  # No searching message needed
        else:
            processing_message = await ctx.send(f"Searching for `{query_stripped}`...")

        # First, search for the video without downloading to check cache
        song_info = await self._extract_info(query_stripped, download=False)

        if not song_info:
            await ctx.send(f"Could not find or process `{query}`. Please check the URL or search terms.")
            return
        
        # Check if song exceeds maximum duration limit
        if isinstance(song_info, dict) and song_info.get('error') == 'duration_exceeded':
            title = song_info.get('title', 'Unknown Title')
            duration_str = self._format_duration(song_info.get('duration'))
            max_duration_str = self._format_duration(song_info.get('max_duration'))
            
            embed = discord.Embed(
                title="⚠️ Song Too Long",
                description=f"**{title}** exceeds the maximum duration limit.",
                color=discord.Color.red()
            )
            embed.add_field(name="Duration", value=duration_str, inline=True)
            embed.add_field(name="Maximum Allowed", value=max_duration_str, inline=True)
            embed.add_field(name="URL", value=f"[Link]({song_info.get('webpage_url')})", inline=False)
            embed.set_footer(text="This limit helps save performance and disk space.")
            
            await ctx.send(embed=embed)
            return

        # If not cached, download the song
        if not song_info.get('is_cached', False):
            if processing_message:
                # Use the title from the initial search if available
                title = song_info.get('title', 'Unknown Title')
                await processing_message.edit(content=f"Downloading **{title}**...")
            
            # Extract info again with download=True to actually download the file
            song_info = await self._extract_info(query_stripped, download=True)
            
            if not song_info:
                await ctx.send(f"Failed to download `{query}`. Please try again.")
                return

        try:
            # Call the method on the db_manager instance
            request_id = self.db_manager.log_song_request(
                user_id=ctx.author.id,
                user_name=str(ctx.author),
                guild_id=ctx.guild.id,
                query=query_stripped,
                resolved_title=song_info.get('title', 'N/A'),
                resolved_url=song_info.get('webpage_url'),
                channel_name=song_info.get('channel'),
                duration=song_info.get('duration')
            )
            song_info['request_id'] = request_id
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
             if song_info.get('is_cached', False):
                 if song_info.get('was_previously_cached', False):
                     embed.add_field(name="Source", value="📁 Cached", inline=True)
                 else:
                     embed.add_field(name="Source", value="⬇️ New Download", inline=True)
             else:
                 embed.add_field(name="Source", value="🌐 Stream", inline=True)
             await ctx.send(embed=embed)
        else:
            # Set start time *just before* playback
            song_info['start_time'] = time.time()
            self.current_song[guild_id] = song_info
            if 'request_id' in song_info:
                self.db_manager.update_play_start_timestamp(song_info['request_id'])
                self.db_manager.update_play_status(song_info['request_id'], 'playing')
            logger.info(f"Playing immediately in guild {guild_id}: {song_info['title']}")
            logger.debug(f"Attempting to play URL: {song_info['url']}")
            try:
                # Use different FFmpeg options for local files vs streaming
                if song_info.get('is_cached', False):
                    local_ffmpeg_options = {
                        'before_options': '-re -nostdin -loglevel error',
                        'options': f'-vn -f s16le -ar 48000 -ac 2 -af "volume={FFMPEG_VOLUME}"',
                        'executable': FFMPEG_EXECUTABLE
                    }
                    source = discord.FFmpegPCMAudio(song_info['url'], **local_ffmpeg_options)
                else:
                    source = discord.FFmpegPCMAudio(song_info['url'], **FFMPEG_OPTIONS)
                
                vc.play(source, after=lambda e: self._play_next(guild_id, error=e))

                embed = discord.Embed(title="Now Playing", description=f"[{song_info['title']}]({song_info['webpage_url']})", color=discord.Color.green())
                if song_info.get('thumbnail'):
                    embed.set_thumbnail(url=song_info['thumbnail'])
                if song_info.get('duration'):
                    embed.add_field(name="Duration", value=self._format_duration(song_info['duration']))
                if song_info.get('is_cached', False):
                    if song_info.get('was_previously_cached', False):
                        embed.add_field(name="Source", value="📁 Cached", inline=True)
                    else:
                        embed.add_field(name="Source", value="⬇️ New Download", inline=True)
                else:
                    embed.add_field(name="Source", value="🌐 Stream", inline=True)
                await ctx.send(embed=embed)

            except discord.ClientException as e:
                await ctx.send(f"Error starting playback: {e}")
                logger.error(f"ClientException during initial play in {guild_id}: {e}")
                self.current_song.pop(guild_id, None) # Clear current song if playback failed
            except Exception as e:
                await ctx.send("An unexpected error occurred while trying to play.")
                logger.exception(f"Unexpected error during initial play in {guild_id}: {e}")
                self.current_song.pop(guild_id, None) # Clear current song

    @commands.command(name='skip', help='Skips the currently playing song.')
    async def skip(self, ctx: commands.Context):
        """Skips the current song."""
        logger.info(f"'skip' command invoked by '{ctx.author}' in guild '{ctx.guild.name}' ({ctx.guild.id})")
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
        
        # Check playback progress
        elapsed_time = time.time() - current.get('start_time', 0)
        duration = current.get('duration')
        
        # Handle case where duration is None
        if duration is None or duration <= 0 or (elapsed_time / duration) < 0.6:
            # Less than 60% played, so mark as skipped
            if 'request_id' in current:
                self.db_manager.update_play_status(current['request_id'], 'skipped')
        else:
            # 60% or more played, so mark as completed
            if 'request_id' in current:
                self.db_manager.update_play_status(current['request_id'], 'completed')


        logger.info(f"Skipping song in guild {guild_id} by command: {current['title']}")
        await ctx.send(f"Skipping: {current['title']}")
        # Set a flag to indicate a skip was requested.
        # The 'after' callback (_play_next) will handle the rest.
        self.current_song[guild_id]['was_skipped'] = True
        vc.stop()

    @commands.command(name='queue', aliases=['q'], help='Shows the current song queue.')
    async def queue(self, ctx: commands.Context):
        """Displays the song queue."""
        logger.info(f"'queue' command invoked by '{ctx.author}' in guild '{ctx.guild.name}' ({ctx.guild.id})")
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
        logger.info(f"'nowplaying' command invoked by '{ctx.author}' in guild '{ctx.guild.name}' ({ctx.guild.id})")
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

        start_time = current.get('start_time')
        total_duration = current.get('duration')
        title = current.get('title', 'Unknown Title')
        webpage_url = current.get('webpage_url', '')
        thumbnail = current.get('thumbnail')

        progress_str = ""
        if start_time and total_duration:
            # Note: This calculation might be slightly inaccurate if the bot was paused.
            # Implementing perfect pause handling requires more state tracking.
            elapsed_seconds = time.time() - start_time
            # Clamp elapsed time to not exceed total duration
            elapsed_seconds = max(0, min(elapsed_seconds, total_duration))

            formatted_elapsed = self._format_duration(elapsed_seconds)
            formatted_total = self._format_duration(total_duration)
            progress_str = f"{formatted_elapsed} / {formatted_total}"

             # Simple progress bar (optional)
            bar_length = 20 # characters
            progress_ratio = elapsed_seconds / total_duration if total_duration > 0 else 0
            filled_length = int(bar_length * progress_ratio)
            bar = '█' * filled_length + '░' * (bar_length - filled_length)
            progress_str += f"\n`[{bar}]`"

        elif total_duration:
            formatted_total = self._format_duration(total_duration)
            progress_str = f"??:?? / {formatted_total}"
        else:
            progress_str = "Progress unavailable"

        state = "Playing"
        if vc.is_paused():
            state = "Paused" # Add paused state info

        embed = discord.Embed(title=f"{state}: {title}", description=f"[{title}]({webpage_url})", color=discord.Color.green() if state == "Playing" else discord.Color.orange())
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)

        embed.add_field(name="Progress", value=progress_str, inline=False)

        await ctx.send(embed=embed)
        # Log potential inaccuracy if paused
        if vc.is_paused():
            logger.debug(f"NP command used while paused in guild {guild_id}. Displayed time may not reflect exact pause point.")

    @commands.command(name='remove', help='Removes a song from the queue by its number (use !queue to see numbers).')
    async def remove(self, ctx: commands.Context, position: int):
        """Removes a song from the queue specified by its 1-based position."""
        logger.info(f"'remove' command invoked by '{ctx.author}' in guild '{ctx.guild.name}' ({ctx.guild.id}) with position: {position}")
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

                # Update status in DB to 'skipped'
                if 'request_id' in removed_song_info:
                    self.db_manager.update_play_status(removed_song_info['request_id'], 'skipped')
                    logger.info(f"Updated status to 'skipped' for removed song with request_id: {removed_song_info['request_id']}")

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

    @commands.command(name='clear', help='Clears the song queue.')
    async def clear(self, ctx: commands.Context):
        """Clears all songs from the queue."""
        logger.info(f"'clear' command invoked by '{ctx.author}' in guild '{ctx.guild.name}' ({ctx.guild.id})")
        guild_id = ctx.guild.id
        self.last_activity[guild_id] = time.time() # Update activity

        queue = self.get_queue(guild_id)
        if not queue:
            await ctx.send("The queue is already empty.")
            return

        queue.clear()
        await ctx.send("Song queue cleared!")
        logger.info(f"Queue cleared for guild {guild_id} by command.")

    @commands.command(name='stats', help='Shows song request stats for a user (or yourself) in this server.')
    async def stats(self, ctx: commands.Context, *, member: discord.Member = None):
        """Shows the total number of songs requested by the specified user or yourself in the current server."""
        target_user = member or ctx.author
        guild_id = ctx.guild.id

        logger.info(f"Stats command invoked by {ctx.author} for user {target_user} in guild {guild_id}")

        # --- Fetch stats using the DatabaseManager ---
        try:
            # Call the method on the db_manager instance, passing guild_id
            request_count = self.db_manager.get_user_stats(target_user.id, guild_id)
        except Exception as e:
             logger.error(f"Error getting stats via DB Manager for user {target_user.id} in guild {guild_id}: {e}", exc_info=True)
             await ctx.send("An error occurred while fetching stats.")
             return

        # Send the result
        await ctx.send(f"📊 **{target_user.display_name}** has requested **{request_count}** track(s) in this server.")

    @stats.error
    async def stats_error(self, ctx: commands.Context, error: commands.CommandError):
        """Handles errors for the !stats command."""
        if isinstance(error, commands.MemberNotFound):
            # 'argument' contains the raw string input that failed conversion
            user_input = error.argument
            await ctx.send(
                f"Could not find a member matching '{user_input}' in this server. Please use their @mention, username#discriminator, or user ID.")
            logger.warning(f"MemberNotFound error in stats command: Input='{user_input}', Guild='{ctx.guild.id}'")
            error.handled = True
        elif isinstance(error, commands.CommandInvokeError):
            # This catches errors *inside* the stats command logic (e.g., database errors that weren't caught)
            logger.error(f"Error during stats command execution: {error.original}", exc_info=True)
            await ctx.send("An unexpected error occurred while processing the stats command.")
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

    @commands.command(name='statslong', help='Shows detailed song request stats for a user.')
    async def statslong(self, ctx: commands.Context, *, member: discord.Member = None):
        """Shows detailed statistics for a user."""
        target_user = member or ctx.author
        guild_id = ctx.guild.id

        logger.info(f"Statslong command invoked by {ctx.author} for user {target_user} in guild {guild_id}")

        try:
            stats = self.db_manager.get_user_stats_long(target_user.id, guild_id)
        except Exception as e:
            logger.error(f"Error getting long stats via DB Manager for user {target_user.id} in guild {guild_id}: {e}", exc_info=True)
            await ctx.send("An error occurred while fetching stats.")
            return

        embed = discord.Embed(
            title=f"📊 Detailed Stats for {target_user.display_name}",
            color=discord.Color.blue()
        )
        embed.set_thumbnail(url=target_user.display_avatar.url)

        embed.add_field(name="Requests Today", value=stats['today'], inline=True)
        embed.add_field(name="Requests This Week", value=stats['this_week'], inline=True)
        embed.add_field(name="Requests This Month", value=stats['this_month'], inline=True)
        embed.add_field(name="Requests This Year", value=stats['this_year'], inline=True)
        embed.add_field(name="All Time Requests", value=stats['all_time'], inline=True)
        embed.add_field(name="Longest Streak", value=f"{stats['longest_streak']} days", inline=True)

        if stats['top_5_requests']:
            top_requests_str = ""
            for i, item in enumerate(stats['top_5_requests']):
                top_requests_str += f"{i+1}. {item['title']} ({item['count']} times)\n"
            embed.add_field(name="Top 5 Requests", value=top_requests_str, inline=False)
        else:
            embed.add_field(name="Top 5 Requests", value="No requests yet!", inline=False)

        await ctx.send(embed=embed)

    @statslong.error
    async def statslong_error(self, ctx: commands.Context, error: commands.CommandError):
        """Handles errors for the !statslong command."""
        if isinstance(error, commands.MemberNotFound):
            user_input = error.argument
            await ctx.send(
                f"Could not find a member matching '{user_input}' in this server. Please use their @mention, username#discriminator, or user ID.")
            logger.warning(f"MemberNotFound error in statslong command: Input='{user_input}', Guild='{ctx.guild.id}'")
            error.handled = True
        elif isinstance(error, commands.CommandInvokeError):
            logger.error(f"Error during statslong command execution: {error.original}", exc_info=True)
            await ctx.send("An unexpected error occurred while processing the statslong command.")
        else:
            logger.error(f"Unhandled error in statslong command: {error}", exc_info=True)
            await ctx.send("An error occurred processing the statslong command.")

    @commands.command(name='cache', help='Shows information about the song cache.')
    async def cache_info(self, ctx: commands.Context):
        """Displays information about the song cache."""
        logger.info(f"'cache' command invoked by '{ctx.author}' in guild '{ctx.guild.name}' ({ctx.guild.id})")
        self.last_activity[ctx.guild.id] = time.time() # Update activity
        
        cache_size = len(self.song_cache)
        file_sizes_mb = []
        
        # Calculate file sizes for statistics
        for file_path in self.song_cache.values():
            if os.path.exists(file_path):
                try:
                    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)  # Convert to MB
                    file_sizes_mb.append(file_size_mb)
                except (OSError, IOError):
                    # Skip files that can't be accessed
                    continue
        
        # Calculate statistics
        if file_sizes_mb:
            total_size_mb = sum(file_sizes_mb)
            average_size_mb = total_size_mb / len(file_sizes_mb)
            largest_size_mb = max(file_sizes_mb)
        else:
            total_size_mb = 0
            average_size_mb = 0
            largest_size_mb = 0
        
        embed = discord.Embed(
            title="📁 Song Cache Information",
            color=discord.Color.blue()
        )

        embed.add_field(name="Cached Songs", value=f"{cache_size} songs", inline=True)
        embed.add_field(name="Total Size", value=f"{total_size_mb:.2f} MB", inline=True)

        embed.add_field(name="\u200b", value="\u200b", inline=True)

        embed.add_field(name="Average Size", value=f"{average_size_mb:.2f} MB", inline=True)
        embed.add_field(name="Largest File", value=f"{largest_size_mb:.2f} MB", inline=True)

        embed.add_field(name="\u200b", value="\u200b", inline=True)
        
        await ctx.send(embed=embed)
    
    @commands.command(name='clearcache', help='Clears the song cache (admin only).')
    @commands.has_permissions(administrator=True)
    async def clear_cache(self, ctx: commands.Context):
        """Clears all cached songs."""
        logger.info(f"'clearcache' command invoked by '{ctx.author}' in guild '{ctx.guild.name}' ({ctx.guild.id})")
        
        # Check for admin permission
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("You need administrator permissions to use this command.")
            return
        
        # Confirm with the user
        confirm_msg = await ctx.send("⚠️ This will delete all cached songs. Are you sure? Type `confirm` to proceed.")
        
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() == "confirm"
        
        try:
            # Wait for confirmation
            await self.bot.wait_for('message', check=check, timeout=30.0)
            
            # Clear the cache
            cache_dir = "song_cache"
            if os.path.exists(cache_dir):
                for filename in os.listdir(cache_dir):
                    if filename.endswith(".opus"):
                        file_path = os.path.join(cache_dir, filename)
                        try:
                            os.remove(file_path)
                            logger.info(f"Deleted cached file: {file_path}")
                        except Exception as e:
                            logger.error(f"Failed to delete {file_path}: {e}")
            
            # Reset the cache dictionary
            self.song_cache.clear()
            
            await ctx.send("✅ Song cache has been cleared.")
            logger.info(f"Song cache cleared by {ctx.author} in guild {ctx.guild.id}")
            
        except asyncio.TimeoutError:
            await ctx.send("Cache clear cancelled - no confirmation received.")
        except Exception as e:
            await ctx.send(f"An error occurred while clearing the cache: {e}")
            logger.exception(f"Error in clear_cache command: {e}")
    
    @clear_cache.error
    async def clear_cache_error(self, ctx: commands.Context, error: commands.CommandError):
        """Handles errors for the clearcache command."""
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need administrator permissions to use this command.")
        else:
            logger.error(f"An unexpected error occurred in the clear_cache command: {error}")

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
        if hasattr(error, 'handled') and error.handled:
            return

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
    """
    Called when the bot is ready and connected to Discord.
    This can be called multiple times (e.g., on reconnect).
    """
    # Clean up any orphaned songs from a previous session
    db_manager = DatabaseManager(DATABASE_FILE)
    db_manager.cleanup_queued_songs()
    
    logger.info(f'Logged in as {bot.user.name} ({bot.user.id})')
    logger.info(f'Discord.py Version: {discord.__version__}')
    logger.info(f'PyNaCl Version: {nacl.__version__}')
    logger.info(f'yt-dlp Version: {yt_dlp.version.__version__}')
    logger.info('-------------------')
    logger.info('Bot is ready and online.')
    logger.info('-------------------')
    
    # Set the bot's activity/presence
    await bot.change_presence(activity=discord.Game(name="Music | !help"))

async def handle_logs(request):
    try:
        import html
        from collections import deque

        # Read only the most recent lines of the log file
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            last_lines = deque(f, 500)
            log_content = "".join(last_lines)

        # Read the HTML template
        with open('log_viewer.html', 'r', encoding='utf-8') as f:
            html_template = f.read()

        # Escape the log content and inject it into the template
        escaped_log_content = html.escape(log_content)
        final_html = html_template.replace('{log_content}', escaped_log_content)

        return web.Response(text=final_html, content_type='text/html', charset='utf-8')

    except FileNotFoundError as e:
        # Handle either the log file or the template file not being found
        error_message = f"<h1>File Not Found</h1><p>Could not find: {e.filename}</p>"
        logger.error(f"Web server error: {e.filename} not found.")
        return web.Response(text=error_message, content_type='text/html', status=404)
    except Exception as e:
        logger.error(f"Error reading log file for web server: {e}")
        return web.Response(text=f"<h1>Error reading log file</h1><p>{e}</p>", content_type='text/html', status=500)

# This handler triggers the graceful shutdown
async def handle_shutdown(request):
    logger.info("Shutdown command received via web interface.")
    # We create a task to close the bot. This allows us to send the HTTP
    # response back to the browser before the application fully terminates.
    asyncio.create_task(bot.close())
    return web.Response(text="Shutdown signal sent. The bot will now terminate gracefully.")

# This function sets up and starts the aiohttp server
async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_logs)
    app.router.add_get("/shutdown", handle_shutdown)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, SERVER_HOST, SERVER_PORT)
    
    # Use a try/finally block to ensure cleanup happens when the task is cancelled.
    try:
        await site.start()
        logger.info(f"--- Log server running on http://{SERVER_HOST}:{SERVER_PORT} ---")
        logger.info(f"--- View logs at: http://{SERVER_HOST}:{SERVER_PORT} ---")
        logger.info(f"--- Shutdown bot at: http://{SERVER_HOST}:{SERVER_PORT}/shutdown ---")
        await asyncio.Event().wait()
    finally:
        logger.info("Web server is shutting down.")
        await runner.cleanup()

async def main():
    if not DISCORD_TOKEN or DISCORD_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.critical("DISCORD_BOT_TOKEN not found or not set correctly. Halting.")
        return

    # Create tasks for the bot and the web server to run concurrently
    async with bot:
        # Add the cog before starting
        await bot.add_cog(MusicCog(bot))
        
        # Start the web server as a background task
        web_server_task = asyncio.create_task(start_web_server())
        
        logger.info("Starting bot...")
        try:
            await bot.start(DISCORD_TOKEN)
        except discord.LoginFailure:
            logger.critical("Login failed: Invalid Discord token provided.")
        finally:
            # When bot.start() finishes (due to bot.close()), we ensure other tasks are cancelled.
            logger.info("Bot has been closed. Cleaning up remaining tasks.")
            if not web_server_task.done():
                web_server_task.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received. Shutting down.")
    finally:
        logger.info("Application has finished.")
