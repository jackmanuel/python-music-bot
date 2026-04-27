import logging

import yt_dlp

from config import FFMPEG_EXECUTABLE, SONG_CACHE_DIR

logger = logging.getLogger(__name__)

YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'outtmpl': str(SONG_CACHE_DIR / '%(extractor)s-%(id)s.%(ext)s'),
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
    },
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'opus',
        'preferredquality': '192',
    }],
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
    'executable': FFMPEG_EXECUTABLE
}


def run_yt_dlp_extractor(query, download=False):
    """Runs yt-dlp extract_info in a way that's pickleable for multiprocessing."""
    try:
        ydl_logger = logging.getLogger('yt-dlp')
        ydl_logger.setLevel(logging.DEBUG)

        ydl_options = YDL_OPTIONS.copy()
        ydl_options['logger'] = ydl_logger

        with yt_dlp.YoutubeDL(ydl_options) as ydl:
            data = ydl.extract_info(query, download=download)
        return data
    except Exception as e:
        logger.error(f"Error within run_yt_dlp_extractor for '{query}': {e}")
        raise


def run_yt_dlp_search(query):
    """Runs yt-dlp to search for a video without downloading."""
    try:
        ydl_logger = logging.getLogger('yt-dlp')
        ydl_logger.setLevel(logging.DEBUG)

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
        }

        with yt_dlp.YoutubeDL(search_options) as ydl:
            data = ydl.extract_info(query, download=False)
        return data
    except Exception as e:
        logger.error(f"Error within run_yt_dlp_search for '{query}': {e}")
        raise
