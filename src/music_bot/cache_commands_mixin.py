import asyncio
import logging
import os
import time

import discord
from discord.ext import commands

from config import SONG_CACHE_DIR

logger = logging.getLogger(__name__)


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
