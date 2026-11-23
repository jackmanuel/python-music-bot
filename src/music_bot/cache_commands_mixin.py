import logging
import os
import time

import discord
from discord.ext import commands

from config import MAX_SONG_DURATION_SECONDS
from formatting import format_duration
from youtube import AGE_RESTRICTED_PLAYBACK_MESSAGE
from youtube_urls import (
    canonical_youtube_url,
    extract_youtube_video_id,
    normalize_search_query,
    youtube_id_from_cache_path,
)

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

    @commands.command(
        name='setquery',
        help='Maps a search query to a YouTube URL, caching the song if needed.'
    )
    async def set_query(self, ctx: commands.Context, *, raw_args: str):
        """Explicitly maps a normalized search query to a YouTube video."""
        logger.info(
            "'setquery' command invoked by '%s' in guild '%s' (%s) with args: %s",
            ctx.author,
            ctx.guild.name,
            ctx.guild.id,
            raw_args,
        )
        self.last_activity[ctx.guild.id] = time.time()

        if getattr(self, 'is_shutting_down', False):
            await ctx.send("The bot is shutting down. Please try again after it restarts.")
            return

        parts = raw_args.strip().rsplit(None, 1)
        if len(parts) != 2:
            await ctx.send("Usage: `!setquery <query> <YouTube URL>`")
            return

        raw_query, raw_url = parts
        youtube_id = extract_youtube_video_id(raw_url)
        if not youtube_id:
            await ctx.send(
                "Please provide a valid YouTube video URL at the end. "
                "Usage: `!setquery <query> <YouTube URL>`"
            )
            return

        normalized_query = normalize_search_query(raw_query)
        if not normalized_query:
            await ctx.send(
                "Please provide a plain text search query (not a URL or search prefix). "
                "Usage: `!setquery <query> <YouTube URL>`"
            )
            return

        canonical_url = canonical_youtube_url(youtube_id)
        safe_query = discord.utils.escape_markdown(normalized_query)

        # Fast path: file is already valid on disk and metadata is in cached_songs.
        cached_file = self.song_cache.get(youtube_id) if getattr(self, 'song_cache', None) else None
        is_valid_file = False
        if cached_file:
            try:
                is_valid_file = os.path.exists(cached_file) and os.path.getsize(cached_file) > 0
            except OSError:
                is_valid_file = False

        cached_song = self.db_manager.get_cached_song(youtube_id) if is_valid_file else None
        if (
            is_valid_file
            and cached_song
            and cached_song.get('title')
            and cached_song.get('duration') is not None
        ):
            self.db_manager.upsert_cached_query(normalized_query, youtube_id, overwrite=True)
            safe_title = discord.utils.escape_markdown(cached_song['title'])
            await ctx.send(
                f"✅ Mapped `{safe_query}` → **{safe_title}** (<{canonical_url}>)"
            )
            return

        status_msg = await ctx.send(f"Checking video for `{safe_query}`...")

        song_info = await self._extract_info(canonical_url, download=False)
        if getattr(self, 'is_shutting_down', False):
            return

        if not song_info:
            await status_msg.edit(
                content=f"Could not fetch video info for <{canonical_url}>. Please check the URL."
            )
            return

        if isinstance(song_info, dict) and song_info.get('error') == 'livestream':
            await status_msg.edit(content="Video is a livestream and cannot be cached.")
            return

        if isinstance(song_info, dict) and song_info.get('error') == 'age_restricted':
            await status_msg.edit(content=AGE_RESTRICTED_PLAYBACK_MESSAGE)
            return

        downloaded_now = False
        if not song_info.get('is_cached', False):
            title = song_info.get('title', 'Unknown Title')
            safe_title = discord.utils.escape_markdown(title)
            duration = song_info.get('duration')

            if song_info.get('exceeds_cache_duration', False) or (
                duration is not None and duration > MAX_SONG_DURATION_SECONDS
            ):
                duration_str = format_duration(duration)
                max_str = format_duration(MAX_SONG_DURATION_SECONDS)
                await status_msg.edit(
                    content=(
                        f"Cannot cache **{safe_title}** because its duration ({duration_str}) "
                        f"exceeds the maximum cache length ({max_str})."
                    )
                )
                return

            if not getattr(self, 'cache_downloads_enabled', True):
                await status_msg.edit(
                    content=f"Cannot cache **{safe_title}** because song cache downloads are currently disabled."
                )
                return

            song_cache = getattr(self, 'song_cache', None)
            if song_cache is not None and not song_cache.can_accept_download(
                song_info.get('estimated_size_bytes', 0)
            ):
                await status_msg.edit(
                    content=f"Cannot cache **{safe_title}** because the song cache size limit has been reached."
                )
                return

            await status_msg.edit(content=f"Downloading **{safe_title}** to cache...")
            song_info = await self._extract_info(canonical_url, download=True)
            if getattr(self, 'is_shutting_down', False):
                return

            if not song_info:
                await status_msg.edit(content=f"Failed to download <{canonical_url}> to cache.")
                return

            if isinstance(song_info, dict) and song_info.get('error') == 'livestream':
                await status_msg.edit(content="Video is a livestream and cannot be cached.")
                return

            if isinstance(song_info, dict) and song_info.get('error') == 'age_restricted':
                await status_msg.edit(content=AGE_RESTRICTED_PLAYBACK_MESSAGE)
                return

            if not song_info.get('is_cached', False):
                if song_info.get('exceeds_cache_duration', False):
                    max_str = format_duration(MAX_SONG_DURATION_SECONDS)
                    await status_msg.edit(
                        content=(
                            f"Cannot cache **{safe_title}** because it exceeds the "
                            f"maximum cache length ({max_str})."
                        )
                    )
                else:
                    await status_msg.edit(
                        content=f"Could not cache **{safe_title}** because the song cache size limit was reached."
                    )
                return

            downloaded_now = True

        saved = self.db_manager.upsert_cached_query(normalized_query, youtube_id, overwrite=True)
        if not saved:
            await status_msg.edit(
                content=(
                    f"Could not save query mapping for `{safe_query}` because usable "
                    f"metadata could not be stored for <{canonical_url}>."
                )
            )
            return

        safe_title = discord.utils.escape_markdown(song_info.get('title', 'Unknown Title'))
        suffix = " (downloaded and cached)" if downloaded_now else ""
        await status_msg.edit(
            content=f"✅ Mapped `{safe_query}` → **{safe_title}** (<{canonical_url}>){suffix}"
        )

    @set_query.error
    async def set_query_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: `!setquery <query> <YouTube URL>`")
            error.handled = True
        else:
            logger.error("Error in setquery command: %s", error, exc_info=True)

    @commands.command(
        name='delquery',
        help='Removes a cached search query mapping without deleting the audio file.'
    )
    async def del_query(self, ctx: commands.Context, *, query: str):
        """Removes a normalized search query from cached_queries."""
        logger.info(
            "'delquery' command invoked by '%s' in guild '%s' (%s) with query: %s",
            ctx.author,
            ctx.guild.name,
            ctx.guild.id,
            query,
        )
        self.last_activity[ctx.guild.id] = time.time()

        normalized_query = normalize_search_query(query)
        if not normalized_query:
            await ctx.send("Please provide a valid search query to remove. Usage: `!delquery <query>`")
            return

        safe_query = discord.utils.escape_markdown(normalized_query)
        if self.db_manager.delete_cached_query(normalized_query):
            await ctx.send(
                f"🗑️ Removed cached query mapping for `{safe_query}`. "
                "(The cached audio file was kept.)"
            )
        else:
            await ctx.send(f"No cached query mapping found for `{safe_query}`.")

    @del_query.error
    async def del_query_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: `!delquery <query>`")
            error.handled = True
        else:
            logger.error("Error in delquery command: %s", error, exc_info=True)
