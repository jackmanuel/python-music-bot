import asyncio
import logging
import time

import discord
from discord.ext import commands, tasks

from config import INACTIVITY_TIMEOUT_MINUTES
from youtube import AGE_RESTRICTED_PLAYBACK_MESSAGE, is_age_restricted_yt_dlp_error

logger = logging.getLogger(__name__)


class VoiceLifecycleMixin:
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

        if before.channel == vc.channel and after.channel != vc.channel:
            human_members = [m for m in vc.channel.members if not m.bot]

            if not human_members:
                logger.info(f"Voice channel {vc.channel.name} in guild {guild_id} is empty. Scheduling disconnect.")
                # Introduce a small delay before disconnecting
                # This helps prevent race conditions if someone quickly rejoins
                await asyncio.sleep(10)

                vc = self.voice_clients.get(guild_id)
                if vc and vc.is_connected() and before.channel == vc.channel:
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


    @tasks.loop(minutes=1.0)
    async def inactivity_check(self):
        """Periodically checks for inactive voice clients and disconnects them."""
        now = time.time()
        inactive_threshold = INACTIVITY_TIMEOUT_MINUTES * 60

        for guild_id in list(self.voice_clients.keys()):
            vc = self.voice_clients.get(guild_id)
            last_act = self.last_activity.get(guild_id)

            if vc and vc.is_connected() and not vc.is_playing() and not vc.is_paused() and last_act:
                if (now - last_act) > inactive_threshold:
                    logger.info(f"Disconnecting from guild {guild_id} due to inactivity.")
                    await vc.disconnect()
                    self.voice_clients.pop(guild_id, None)
                    self.queues.pop(guild_id, None)
                    self.current_song.pop(guild_id, None)
                    self.last_activity.pop(guild_id, None)
            elif vc and vc.is_connected() and (vc.is_playing() or vc.is_paused()):
                self.last_activity[guild_id] = now


    @inactivity_check.before_loop
    async def before_inactivity_check(self):
        """Ensures the bot is ready before the loop starts."""
        await self.bot.wait_until_ready()
        logger.info("Inactivity check loop ready.")

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Handles errors specific to this cog."""
        if hasattr(error, 'handled') and error.handled:
            return

        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)) and ctx.command.name == 'remove':
             return

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
            if is_age_restricted_yt_dlp_error(error.original):
                await ctx.send(AGE_RESTRICTED_PLAYBACK_MESSAGE)
                logger.exception(f"CommandInvokeError in {ctx.command.qualified_name}: {error.original}")
                return
            await ctx.send(f"An error occurred while executing the command. Please check the logs or contact the admin. Error: {error.original}")
            logger.exception(f"CommandInvokeError in {ctx.command.qualified_name}: {error.original}")
        else:
            await ctx.send(f"An unexpected error occurred: {error}")
