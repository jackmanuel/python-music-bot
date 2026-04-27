import asyncio
import logging
import os

import discord
from discord.ext import commands

from leaderboard_graph import generate_cumulative_graph
from leaderboard_race import generate_race_video

logger = logging.getLogger(__name__)


class StatsCommandsMixin:
    @commands.command(name='stats', help='Shows song request stats for a user (or yourself) in this server.')
    async def stats(self, ctx: commands.Context, *, member: discord.Member = None):
        """Shows the total number of songs requested by the specified user or yourself in the current server."""
        target_user = member or ctx.author
        guild_id = ctx.guild.id

        logger.info(f"Stats command invoked by {ctx.author} for user {target_user} in guild {guild_id}")

        try:
            request_count = self.db_manager.get_user_stats(target_user.id, guild_id)
        except Exception as e:
             logger.error(f"Error getting stats via DB Manager for user {target_user.id} in guild {guild_id}: {e}", exc_info=True)
             await ctx.send("An error occurred while fetching stats.")
             return

        await ctx.send(f"📊 **{target_user.display_name}** has requested **{request_count}** track(s) in this server.")

    @stats.error
    async def stats_error(self, ctx: commands.Context, error: commands.CommandError):
        """Handles errors for the !stats command."""
        if isinstance(error, commands.MemberNotFound):
            user_input = error.argument
            await ctx.send(
                f"Could not find a member matching '{user_input}' in this server. Please use their @mention, username#discriminator, or user ID.")
            logger.warning(f"MemberNotFound error in stats command: Input='{user_input}', Guild='{ctx.guild.id}'")
            error.handled = True
        elif isinstance(error, commands.CommandInvokeError):
            logger.error(f"Error during stats command execution: {error.original}", exc_info=True)
            await ctx.send("An unexpected error occurred while processing the stats command.")
        else:
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
            db_user_name = user_data['user_name']
            request_count = user_data['request_count']

            member = ctx.guild.get_member(user_id)
            display_name = member.display_name if member else db_user_name
            not_found_tag = "" if member else " *(user not in server)*"

            rank_display = rank_emojis.get(rank, f"{rank}.")
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

    @commands.command(name='leaderboardrace', aliases=['lbrace'], help='Generates an animated bar chart race video of song play history.')
    async def leaderboard_race(self, ctx: commands.Context):
        """Creates an MP4 animation showing the leaderboard evolving over time."""
        logger.info(f"Leaderboard race command invoked by {ctx.author} in guild {ctx.guild.id}")
        guild_id = ctx.guild.id

        status_msg = await ctx.send("🎬 Generating leaderboard race video... This may take a moment.")

        try:
            play_data = self.db_manager.get_play_history_for_race(guild_id)

            if not play_data:
                await status_msg.edit(content="📊 No play history data available yet. Play some songs first!")
                return

            if len(play_data) < 5:
                await status_msg.edit(content="📊 Not enough play history to generate a race. Need at least 5 completed plays.")
                return

            loop = asyncio.get_event_loop()
            output_path = await loop.run_in_executor(
                None,
                lambda: generate_race_video(play_data, ctx.guild)
            )

            if output_path is None:
                await status_msg.edit(content="❌ Failed to generate the video. Please check the logs.")
                return

            await status_msg.edit(content="📤 Uploading video...")
            try:
                with open(output_path, 'rb') as f:
                    video_file = discord.File(f, filename="leaderboard_race.mp4")
                    await ctx.send("🏆 **Leaderboard Race** - Watch the competition unfold!", file=video_file)
                await status_msg.delete()
            finally:
                try:
                    os.remove(output_path)
                    logger.debug(f"Cleaned up temp video file: {output_path}")
                except OSError as e:
                    logger.warning(f"Failed to clean up temp file {output_path}: {e}")

        except Exception as e:
            logger.error(f"Error in leaderboard_race command: {e}", exc_info=True)
            await status_msg.edit(content="❌ An error occurred while generating the video.")

    @leaderboard_race.error
    async def leaderboard_race_error(self, ctx: commands.Context, error: commands.CommandError):
        """Handles errors for the leaderboard race command."""
        if isinstance(error, commands.CommandInvokeError):
            logger.error(f"Error during leaderboard_race command execution: {error.original}", exc_info=True)
            await ctx.send("An unexpected error occurred while generating the leaderboard race.")
        else:
            logger.error(f"Unhandled error in leaderboard_race command: {error}", exc_info=True)
            await ctx.send("An error occurred processing the leaderboard race command.")

    @commands.command(name='cumulativegraph', aliases=['cg'], help='Generates a static line graph of cumulative song plays over time.')
    async def cumulative_graph(self, ctx: commands.Context):
        """Creates a static line graph showing cumulative plays per user over time."""
        logger.info(f"Cumulative graph command invoked by {ctx.author} in guild {ctx.guild.id}")
        guild_id = ctx.guild.id

        status_msg = await ctx.send("📊 Generating cumulative graph... This may take a moment.")

        try:
            play_data = self.db_manager.get_play_history_for_race(guild_id)

            if not play_data:
                await status_msg.edit(content="📊 No play history data available yet. Play some songs first!")
                return

            if len(play_data) < 5:
                await status_msg.edit(content="📊 Not enough play history to generate a graph. Need at least 5 completed plays.")
                return

            loop = asyncio.get_event_loop()
            output_path = await loop.run_in_executor(
                None,
                lambda: generate_cumulative_graph(play_data, ctx.guild)
            )

            if output_path is None:
                await status_msg.edit(content="❌ Failed to generate the graph. Please check the logs.")
                return

            await status_msg.edit(content="📤 Uploading graph...")
            try:
                with open(output_path, 'rb') as f:
                    image_file = discord.File(f, filename="cumulative_plays.png")
                    await ctx.send("📈 **Cumulative Song Plays** - Song plays over time by user", file=image_file)
                await status_msg.delete()
            finally:
                try:
                    os.remove(output_path)
                    logger.debug(f"Cleaned up temp graph file: {output_path}")
                except OSError as e:
                    logger.warning(f"Failed to clean up temp file {output_path}: {e}")

        except Exception as e:
            logger.error(f"Error in cumulative_graph command: {e}", exc_info=True)
            await status_msg.edit(content="❌ An error occurred while generating the graph.")

    @cumulative_graph.error
    async def cumulative_graph_error(self, ctx: commands.Context, error: commands.CommandError):
        """Handles errors for the cumulative graph command."""
        if isinstance(error, commands.CommandInvokeError):
            logger.error(f"Error during cumulative_graph command execution: {error.original}", exc_info=True)
            await ctx.send("An unexpected error occurred while generating the cumulative graph.")
        else:
            logger.error(f"Unhandled error in cumulative_graph command: {error}", exc_info=True)
            await ctx.send("An error occurred processing the cumulative graph command.")
