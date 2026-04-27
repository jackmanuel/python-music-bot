import asyncio
import logging
import time

import discord
from discord.ext import commands

from youtube import FFMPEG_OPTIONS
from config import FFMPEG_EXECUTABLE

logger = logging.getLogger(__name__)


class VoiceCommandsMixin:
    @commands.command(name='join', help='Joins the voice channel you are currently in.')
    async def join(self, ctx: commands.Context):
        """Joins the invoker's voice channel."""
        logger.info(f"'join' command invoked by '{ctx.author}' in guild '{ctx.guild.name}' ({ctx.guild.id})")
        if ctx.author.voice is None:
            await ctx.send("You are not connected to a voice channel.")
            return

        channel = ctx.author.voice.channel
        guild_id = ctx.guild.id
        self.last_activity[guild_id] = time.time()

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
        self.last_activity[guild_id] = time.time()

        if ctx.author.voice is None:
            await ctx.send("You need to be in a voice channel to play music.")
            return

        user_channel = ctx.author.voice.channel
        
        # Check if we have a stale voice client reference (bot thinks it's not connected,
        # but Discord still has an active/reconnecting voice client for this guild)
        existing_guild_vc = ctx.guild.voice_client
        our_vc = self.voice_clients.get(guild_id)
        
        need_to_connect = False
        if our_vc is None or not our_vc.is_connected():
            if existing_guild_vc is not None:
                # Discord still has a voice client for us - could be reconnecting or in a weird state
                if existing_guild_vc.is_connected():
                    logger.info(f"Reusing existing guild voice client for {guild_id}.")
                    self.voice_clients[guild_id] = existing_guild_vc
                else:
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

        query_stripped = query.strip()
        is_url = query_stripped.startswith("http://") or query_stripped.startswith("https://")
        is_soundcloud = is_url and "soundcloud.com" in query_stripped.lower()

        processing_message = None
        if is_url:
            if is_soundcloud:
                processing_message = await ctx.send(f"Processing SoundCloud URL...")
                logger.info(f"Processing SoundCloud URL: {query_stripped}")
            else:
                processing_message = await ctx.send(f"Processing URL...")
                logger.info(f"Processing direct URL: {query_stripped}")
        else:
            processing_message = await ctx.send(f"Searching for `{query_stripped}`...")

        song_info = await self._extract_info(query_stripped, download=False)

        if not song_info:
            await ctx.send(f"Could not find or process `{query}`. Please check the URL or search terms.")
            return
        
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

        if not song_info.get('is_cached', False):
            if processing_message:
                title = song_info.get('title', 'Unknown Title')
                await processing_message.edit(content=f"Downloading **{title}**...")

            song_info = await self._extract_info(query_stripped, download=True)
            
            if not song_info:
                await ctx.send(f"Failed to download `{query}`. Please try again.")
                return

        try:
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
            song_info['start_time'] = time.time()
            self.current_song[guild_id] = song_info
            if 'request_id' in song_info:
                self.db_manager.update_play_start_timestamp(song_info['request_id'])
                self.db_manager.update_play_status(song_info['request_id'], 'playing')
            logger.info(f"Playing immediately in guild {guild_id}: {song_info['title']}")
            logger.debug(f"Attempting to play URL: {song_info['url']}")
            try:
                if song_info.get('is_cached', False):
                    local_ffmpeg_options = {
                        'executable': FFMPEG_EXECUTABLE
                    }
                else:
                    local_ffmpeg_options = FFMPEG_OPTIONS
                
                source = await discord.FFmpegOpusAudio.from_probe(
                    song_info['url'], **local_ffmpeg_options
                )
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
                self.current_song.pop(guild_id, None)
            except Exception as e:
                await ctx.send("An unexpected error occurred while trying to play.")
                logger.exception(f"Unexpected error during initial play in {guild_id}: {e}")
                self.current_song.pop(guild_id, None)

    @commands.command(name='skip', help='Skips the currently playing song.')
    async def skip(self, ctx: commands.Context):
        """Skips the current song."""
        logger.info(f"'skip' command invoked by '{ctx.author}' in guild '{ctx.guild.name}' ({ctx.guild.id})")
        guild_id = ctx.guild.id
        self.last_activity[guild_id] = time.time()

        if guild_id not in self.voice_clients or not self.voice_clients[guild_id].is_connected():
            await ctx.send("I'm not connected to a voice channel.")
            return

        vc = self.voice_clients[guild_id]
        if not vc.is_playing() and not vc.is_paused():
            await ctx.send("I am not playing anything right now.")
            return

        current = self.current_song.get(guild_id)
        if not current:
             await ctx.send("There's no song currently marked as playing, but attempting to stop.")
             vc.stop()
             return
        
        elapsed_time = time.time() - current.get('start_time', 0)
        duration = current.get('duration')

        if duration is None or duration <= 0 or (elapsed_time / duration) < 0.6:
            if 'request_id' in current:
                self.db_manager.update_play_status(current['request_id'], 'skipped')
        else:
            if 'request_id' in current:
                self.db_manager.update_play_status(current['request_id'], 'completed')


        logger.info(f"Skipping song in guild {guild_id} by command: {current['title']}")
        await ctx.send(f"Skipping: {current['title']}")
        # The 'after' callback (_play_next) will handle the rest.
        self.current_song[guild_id]['was_skipped'] = True
        vc.stop()
