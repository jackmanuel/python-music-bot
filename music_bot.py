import discord
from discord.ext import commands, tasks
import yt_dlp
import asyncio
import logging
import time
from collections import deque
import os # For token loading
from dotenv import load_dotenv

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

FFMPEG_PATH = "ffmpeg" # Or specify the full path if not in system PATH, e.g., "/usr/bin/ffmpeg"
INACTIVITY_TIMEOUT_MINUTES = 30 # Minutes before leaving the voice channel due to inactivity

# --- Basic Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('discord')

# --- yt-dlp Options ---
# Optimized for streaming, low resource usage
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'opus', # Opus is efficient for Discord
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True, # Process only single videos by default
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch', # Use "ytsearch" for queries
    'source_address': '0.0.0.0', # Might help with binding issues on some systems
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
    'executable': FFMPEG_PATH
}

# --- Bot Class ---
intents = discord.Intents.default()
intents.message_content = True  # Enable message content intent
intents.voice_states = True     # Enable voice state intent for joining/leaving tracking

bot = commands.Bot(command_prefix="!", intents=intents)

# --- Music Cog ---
class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queues = {}  # Dictionary to hold queues for each guild {guild_id: deque()}
        self.current_song = {} # Dictionary to hold current song info {guild_id: song_info}
        self.voice_clients = {} # Dictionary to hold voice clients {guild_id: voice_client}
        self.last_activity = {} # Dictionary to track last activity time {guild_id: timestamp}
        self.inactivity_check.start()

    def get_queue(self, guild_id):
        """Gets the queue for a guild, creating it if it doesn't exist."""
        if guild_id not in self.queues:
            self.queues[guild_id] = deque()
        return self.queues[guild_id]

    async def _extract_info(self, query):
        """Extracts info using yt-dlp in an executor to avoid blocking."""
        loop = asyncio.get_event_loop()
        try:
            # Use run_in_executor for the blocking I/O operation
            data = await loop.run_in_executor(
                None, lambda: yt_dlp.YoutubeDL(YDL_OPTIONS).extract_info(query, download=False)
            )

            if 'entries' in data:
                # Take the first item from a playlist or search result
                logger.info(f"Found multiple entries for '{query}', using first result.")
                data = data['entries'][0]

            if not data or 'url' not in data:
                 logger.warning(f"Could not extract stream URL for '{query}'. Data: {data}")
                 return None

            # Prepare song info dictionary
            song_info = {
                'title': data.get('title', 'Unknown Title'),
                'url': data['url'], # The crucial stream URL
                'thumbnail': data.get('thumbnail'),
                'duration': data.get('duration'),
                'webpage_url': data.get('webpage_url', query), # Link back to YT page
            }
            return song_info

        except yt_dlp.utils.DownloadError as e:
            logger.error(f"yt-dlp error extracting info for '{query}': {e}")
            return None
        except Exception as e:
            logger.exception(f"Unexpected error extracting info for '{query}': {e}")
            return None

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
        self.current_song[guild_id] = next_song_info
        logger.info(f"Playing next song in guild {guild_id}: {next_song_info['title']}")

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
             self._play_next(guild_id) # Recursive call to try next song
        except Exception as e:
            logger.exception(f"Unexpected error during playback setup in {guild_id}: {e}")
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

        # 1. Ensure user is in a voice channel
        if ctx.author.voice is None:
            await ctx.send("You need to be in a voice channel to play music.")
            return

        # 2. Ensure bot is in a voice channel (or join the user's)
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

        # 3. Extract song info
        await ctx.send(f"Searching for `{query}`...")
        song_info = await self._extract_info(query)

        if not song_info:
            await ctx.send(f"Could not find or process `{query}`. Please check the URL or search terms.")
            return

        # 4. Add to queue or play immediately
        if vc.is_playing() or vc.is_paused() or guild_id in self.current_song:
             queue.append(song_info)
             embed = discord.Embed(title="Added to Queue", description=f"[{song_info['title']}]({song_info['webpage_url']})", color=discord.Color.blue())
             if song_info.get('thumbnail'):
                 embed.set_thumbnail(url=song_info['thumbnail'])
             embed.add_field(name="Position in queue", value=len(queue))
             await ctx.send(embed=embed)
        else:
            self.current_song[guild_id] = song_info
            logger.info(f"Playing immediately in guild {guild_id}: {song_info['title']}")
            try:
                source = discord.FFmpegPCMAudio(song_info['url'], **FFMPEG_OPTIONS)
                vc.play(source, after=lambda e: self._play_next(guild_id, e))

                embed = discord.Embed(title="Now Playing", description=f"[{song_info['title']}]({song_info['webpage_url']})", color=discord.Color.green())
                if song_info.get('thumbnail'):
                    embed.set_thumbnail(url=song_info['thumbnail'])
                if song_info.get('duration'):
                    embed.add_field(name="Duration", value=time.strftime('%H:%M:%S', time.gmtime(song_info['duration'])))
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
        guild_id = ctx.guild.id
        self.last_activity[guild_id] = time.time() # Update activity

        if guild_id not in self.voice_clients or not self.voice_clients[guild_id].is_connected():
            await ctx.send("I'm not connected to a voice channel.")
            return

        vc = self.voice_clients[guild_id]
        if not vc.is_playing() and not vc.is_paused():
            await ctx.send("I am not playing anything right now.")
            return

        if guild_id not in self.current_song:
             await ctx.send("There's no song currently marked as playing.")
             # Try stopping anyway, might be in a weird state
             vc.stop()
             return

        logger.info(f"Skipping song in guild {guild_id} by command.")
        await ctx.send(f"Skipping: {self.current_song[guild_id]['title']}")
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
             embed.add_field(name="Now Playing", value=f"[{current['title']}]({current['webpage_url']})", inline=False)
        else:
             embed.add_field(name="Now Playing", value="Nothing currently playing.", inline=False)


        if queue:
            queue_list = ""
            # Limit display to avoid huge messages
            for i, song in enumerate(list(queue)[:10]): # Show first 10 songs
                queue_list += f"{i + 1}. [{song['title']}]({song['webpage_url']})\n"
            if len(queue) > 10:
                 queue_list += f"\n... and {len(queue) - 10} more."

            embed.add_field(name="Up Next", value=queue_list if queue_list else "Queue is empty.", inline=False)
        else:
             embed.add_field(name="Up Next", value="Queue is empty.", inline=False)

        await ctx.send(embed=embed)

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
                # Check if inactivity timeout exceeded
                if (now - last_act) > inactive_threshold:
                    logger.info(f"Disconnecting from guild {guild_id} due to inactivity.")
                    await vc.disconnect()
                    # Clean up state for this guild
                    self.voice_clients.pop(guild_id, None)
                    self.queues.pop(guild_id, None)
                    self.current_song.pop(guild_id, None)
                    self.last_activity.pop(guild_id, None)
                    # Optionally send a message to the last channel the bot was used in
                    # Requires storing the last text channel used, which adds complexity.
            elif vc and vc.is_connected() and (vc.is_playing() or vc.is_paused()):
                # If playing or paused, ensure the last activity time is recent
                # This prevents the bot from disconnecting if paused for a long time
                # but user intends to resume. Could be adjusted based on preference.
                self.last_activity[guild_id] = now


    @inactivity_check.before_loop
    async def before_inactivity_check(self):
        """Ensures the bot is ready before the loop starts."""
        await self.bot.wait_until_ready()
        logger.info("Inactivity check loop ready.")

    # --- Cog Error Handling ---
    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Handles errors specific to this cog."""
        logger.error(f"Error in command '{ctx.command}': {error}")
        if isinstance(error, commands.CommandNotFound):
            # This might be handled globally, but good to have locally too
            await ctx.send("Invalid command. Use `!help` to see available commands.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing argument: `{error.param.name}`. Use `!help {ctx.command}` for usage.")
        elif isinstance(error, commands.CheckFailure):
            await ctx.send("You don't have the necessary permissions to use this command.")
        else:
            # Generic error message for other cases
            await ctx.send(f"An error occurred: {error}")


# --- Bot Event Handlers ---
@bot.event
async def on_ready():
    """Called when the bot is ready and connected to Discord."""
    logger.info(f'Logged in as {bot.user.name} ({bot.user.id})')
    logger.info('Bot is ready and online.')
    # Add the cog after the bot is ready
    await bot.add_cog(MusicCog(bot))
    print(f'Bot {bot.user.name} is ready.')
    await bot.change_presence(activity=discord.Game(name="Music | !help")) # Set status


# --- Run the Bot ---
if __name__ == "__main__":
    if DISCORD_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ERROR: Please replace 'YOUR_BOT_TOKEN_HERE' with your actual bot token in the script.")
    else:
        try:
            bot.run(DISCORD_TOKEN, log_handler=None) # Use our basicConfig, disable default
        except discord.LoginFailure:
             logger.error("Login failed: Invalid Discord token provided.")
        except Exception as e:
             logger.exception(f"Fatal error during bot execution: {e}")