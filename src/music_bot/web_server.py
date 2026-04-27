import asyncio
import html
import logging
from collections import deque

import discord
from aiohttp import web

from config import LOG_FILE, LOG_VIEWER_TEMPLATE, SERVER_HOST, SERVER_PORT

logger = logging.getLogger(__name__)


async def handle_logs(request):
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            last_lines = deque(f, 500)
            log_content = "".join(last_lines)

        with open(LOG_VIEWER_TEMPLATE, 'r', encoding='utf-8') as f:
            html_template = f.read()

        escaped_log_content = html.escape(log_content)
        final_html = html_template.replace('{log_content}', escaped_log_content)

        return web.Response(text=final_html, content_type='text/html', charset='utf-8')

    except FileNotFoundError as e:
        error_message = f"<h1>File Not Found</h1><p>Could not find: {e.filename}</p>"
        logger.error(f"Web server error: {e.filename} not found.")
        return web.Response(text=error_message, content_type='text/html', status=404)
    except Exception as e:
        logger.error(f"Error reading log file for web server: {e}")
        return web.Response(text=f"<h1>Error reading log file</h1><p>{e}</p>", content_type='text/html', status=500)

async def handle_shutdown(request):
    bot = request.app["bot"]
    logger.info("Shutdown command received via web interface.")
    async def perform_shutdown():
        await bot.change_presence(status=discord.Status.offline)
        # Add a tiny delay to ensure the presence update is sent before the websocket closes
        await asyncio.sleep(1)
        await bot.close()

    # Creating a task lets the HTTP response return before shutdown completes.
    asyncio.create_task(perform_shutdown())
    return web.Response(text="Shutdown signal sent. The bot will now terminate gracefully.")

async def start_web_server(bot):
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", handle_logs)
    app.router.add_post("/shutdown", handle_shutdown)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, SERVER_HOST, SERVER_PORT)
    
    try:
        await site.start()
        logger.info(f"--- Log server running on http://{SERVER_HOST}:{SERVER_PORT} ---")
        logger.info(f"--- View logs at: http://{SERVER_HOST}:{SERVER_PORT} ---")
        logger.info(f"--- To shutdown bot, send a POST request to: http://{SERVER_HOST}:{SERVER_PORT}/shutdown ---")
        await asyncio.Event().wait()
    finally:
        logger.info("Web server is shutting down.")
        await runner.cleanup()
