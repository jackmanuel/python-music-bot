import asyncio
import logging
import os
import time

import discord
from discord.ext import commands

from config import SONG_CACHE_DIR

logger = logging.getLogger(__name__)


def youtube_id_from_cache_path(file_path):
    """Extract a YouTube video ID from a yt-dlp cache filename."""
    filename = os.path.basename(file_path)
    stem, extension = os.path.splitext(filename)
    if extension.lower() not in ('.opus', '.m4a', '.webm', '.mp3', '.aac', '.wav'):
        return None
    if not stem.startswith('youtube-'):
        return None
    youtube_id = stem[len('youtube-'):]
    return youtube_id or None


class CacheCommandsMixin:
    @commands.command(name='cache', help='Shows information about the song cache.')
    async def cache_info(self, ctx: commands.Context):
        """Displays information about the song cache."""
        logger.info(f"'cache' command invoked by '{ctx.author}' in guild '{ctx.guild.name}' ({ctx.guild.id})")
        self.last_activity[ctx.guild.id] = time.time()
        
        cache_size = len(self.song_cache)
        file_sizes_mb = []
        
        for file_path in self.song_cache.values():
            if os.path.exists(file_path):
                try:
                    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                    file_sizes_mb.append(file_size_mb)
                except (OSError, IOError):
                    continue

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
        cache_mode = "Downloads enabled" if getattr(self, 'cache_downloads_enabled', True) else "Streaming uncached songs"
        embed.add_field(name="Cache Mode", value=cache_mode, inline=True)

        embed.add_field(name="Average Size", value=f"{average_size_mb:.2f} MB", inline=True)
        embed.add_field(name="Largest File", value=f"{largest_size_mb:.2f} MB", inline=True)

        embed.add_field(name="\u200b", value="\u200b", inline=True)
        
        await ctx.send(embed=embed)

    @commands.command(
        name='cacheleaderboard',
        aliases=['cachelb'],
        help='Shows which users are responsible for the most cached music.'
    )
    async def cache_leaderboard(self, ctx: commands.Context):
        """Credit each cached YouTube file to its earliest recorded requester."""
        logger.info("Cache leaderboard command invoked by %s in guild %s", ctx.author, ctx.guild.id)
        self.last_activity[ctx.guild.id] = time.time()

        cache_files = []
        for file_path in self.song_cache.values():
            youtube_id = youtube_id_from_cache_path(file_path)
            if not youtube_id:
                continue
            try:
                size_bytes = os.path.getsize(file_path)
            except OSError:
                continue
            cache_files.append((youtube_id, size_bytes))

        try:
            requesters = self.db_manager.get_first_youtube_requesters(
                {youtube_id for youtube_id, _ in cache_files},
                guild_id=ctx.guild.id
            )
        except Exception as e:
            logger.error("Error matching cache files to requesters: %s", e, exc_info=True)
            await ctx.send("An error occurred while generating the cache leaderboard.")
            return

        totals = {}
        for youtube_id, size_bytes in cache_files:
            requester = requesters.get(youtube_id)
            if not requester:
                continue
            user_id = requester['user_id']
            user_total = totals.setdefault(
                user_id,
                {
                    'user_name': requester['user_name'],
                    'size_bytes': 0,
                    'file_count': 0
                }
            )
            user_total['size_bytes'] += size_bytes
            user_total['file_count'] += 1

        if not totals:
            await ctx.send("No cached YouTube files could be matched to request history.")
            return

        ranked_users = sorted(
            totals.items(),
            key=lambda item: (-item[1]['size_bytes'], item[1]['user_name'].casefold())
        )[:10]
        rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
        description_lines = []
        for rank, (user_id, data) in enumerate(ranked_users, start=1):
            member = ctx.guild.get_member(user_id)
            display_name = member.display_name if member else data['user_name']
            safe_name = discord.utils.escape_markdown(display_name)
            size_mb = data['size_bytes'] / (1024 * 1024)
            file_label = "file" if data['file_count'] == 1 else "files"
            description_lines.append(
                f"{rank_emojis.get(rank, f'{rank}.')} **{safe_name}** — "
                f"**{size_mb:.2f} MB** ({data['file_count']} {file_label})"
            )

        embed = discord.Embed(
            title="📁 Cache Leaderboard",
            description="\n".join(description_lines),
            color=discord.Color.gold()
        )
        embed.set_footer(
            text="Files are credited to their earliest recorded requester in this server."
        )
        await ctx.send(embed=embed)
    
    @commands.command(name='clearcache', help='Clears the song cache (admin only).')
    @commands.has_permissions(administrator=True)
    async def clear_cache(self, ctx: commands.Context):
        """Clears all cached songs."""
        logger.info(f"'clearcache' command invoked by '{ctx.author}' in guild '{ctx.guild.name}' ({ctx.guild.id})")
        
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("You need administrator permissions to use this command.")
            return
        
        confirm_msg = await ctx.send("⚠️ This will delete all cached songs. Are you sure? Type `confirm` to proceed.")
        
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() == "confirm"
        
        try:
            await self.bot.wait_for('message', check=check, timeout=30.0)

            cache_dir = SONG_CACHE_DIR
            if os.path.exists(cache_dir):
                for filename in os.listdir(cache_dir):
                    if filename.endswith(".opus"):
                        file_path = str(cache_dir / filename)
                        try:
                            os.remove(file_path)
                            logger.info(f"Deleted cached file: {file_path}")
                        except Exception as e:
                            logger.error(f"Failed to delete {file_path}: {e}")
            
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
