import logging
import time

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)


class QueueCommandsMixin:
    @commands.command(name='queue', aliases=['q'], help='Shows the current song queue.')
    async def queue(self, ctx: commands.Context):
        """Displays the song queue."""
        logger.info(f"'queue' command invoked by '{ctx.author}' in guild '{ctx.guild.name}' ({ctx.guild.id})")
        guild_id = ctx.guild.id
        self.last_activity[guild_id] = time.time()

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
            for i, song in enumerate(list(queue)[:10]):
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
        self.last_activity[guild_id] = time.time()

        vc = self.voice_clients.get(guild_id)
        current = self.current_song.get(guild_id)

        if not vc or not vc.is_connected() or not current:
            await ctx.send("I am not playing anything right now.")
            return

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
            elapsed_seconds = max(0, min(elapsed_seconds, total_duration))

            formatted_elapsed = self._format_duration(elapsed_seconds)
            formatted_total = self._format_duration(total_duration)
            progress_str = f"{formatted_elapsed} / {formatted_total}"

            bar_length = 20
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
            state = "Paused"

        embed = discord.Embed(title=f"{state}: {title}", description=f"[{title}]({webpage_url})", color=discord.Color.green() if state == "Playing" else discord.Color.orange())
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)

        embed.add_field(name="Progress", value=progress_str, inline=False)

        await ctx.send(embed=embed)
        if vc.is_paused():
            logger.debug(f"NP command used while paused in guild {guild_id}. Displayed time may not reflect exact pause point.")

    @commands.command(name='remove', help='Removes a song from the queue by its number (use !queue to see numbers).')
    async def remove(self, ctx: commands.Context, position: int):
        """Removes a song from the queue specified by its 1-based position."""
        logger.info(f"'remove' command invoked by '{ctx.author}' in guild '{ctx.guild.name}' ({ctx.guild.id}) with position: {position}")
        guild_id = ctx.guild.id
        self.last_activity[guild_id] = time.time()

        queue = self.get_queue(guild_id)

        if not queue:
            await ctx.send("The queue is currently empty.")
            return

        index_to_remove = position - 1

        if 0 <= index_to_remove < len(queue):
            try:
                removed_song_info = queue[index_to_remove]
                del queue[index_to_remove]

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
        self.last_activity[guild_id] = time.time()

        queue = self.get_queue(guild_id)
        if not queue:
            await ctx.send("The queue is already empty.")
            return

        queue.clear()
        await ctx.send("Song queue cleared!")
        logger.info(f"Queue cleared for guild {guild_id} by command.")
