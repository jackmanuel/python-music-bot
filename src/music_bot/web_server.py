import asyncio
import html
import logging
import os
import subprocess
import sys
import textwrap
import time
from collections import deque
from pathlib import Path

import discord
from aiohttp import web

from config import LOG_FILE, LOG_VIEWER_TEMPLATE, SERVER_HOST, SERVER_PORT

logger = logging.getLogger(__name__)


async def close_bot_gracefully(bot):
    for cog in bot.cogs.values():
        begin_shutdown = getattr(cog, "begin_shutdown", None)
        if begin_shutdown:
            begin_shutdown()

    await bot.change_presence(status=discord.Status.offline)
    # Add a tiny delay to ensure the presence update is sent before the websocket closes
    await asyncio.sleep(1)
    await bot.close()


def build_restart_command():
    script_path = Path(sys.argv[0]).resolve()
    return [sys.executable, str(script_path), *sys.argv[1:]]


def schedule_restart():
    restart_command = build_restart_command()
    script_path = Path(restart_command[1])
    project_root = script_path.parents[2]
    restart_code = textwrap.dedent(
        f"""
        import os
        import subprocess
        import time

        creation_flags = 0
        if os.name == "nt":
            creation_flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

        time.sleep(3)
        subprocess.Popen(
            {restart_command!r},
            cwd={str(project_root)!r},
            close_fds=True,
            creationflags=creation_flags,
        )
        """
    )

    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    subprocess.Popen(
        [sys.executable, "-c", restart_code],
        cwd=project_root,
        close_fds=True,
        creationflags=creation_flags,
    )


async def handle_logs(request):
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            last_lines = deque(f, 500)
            log_content = "".join(last_lines)

        with open(LOG_VIEWER_TEMPLATE, 'r', encoding='utf-8') as f:
            html_template = f.read()

        escaped_log_content = html.escape(log_content)
        uptime_seconds = int(time.monotonic() - request.app["started_at"])
        final_html = (
            html_template
            .replace('{log_content}', escaped_log_content)
            .replace('{uptime_seconds}', str(uptime_seconds))
        )

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
    if request.app.get("shutdown_requested"):
        return web.Response(text="Shutdown is already in progress.")
    if request.app.get("restart_requested"):
        return web.Response(text="Restart is already in progress.")

    request.app["shutdown_requested"] = True
    logger.info("Shutdown command received via web interface.")

    async def perform_shutdown():
        try:
            await close_bot_gracefully(bot)
        except Exception:
            logger.exception("Error while shutting down the bot.")

    # Creating a task lets the HTTP response return before shutdown completes.
    asyncio.create_task(perform_shutdown())
    return web.Response(text="Shutdown signal sent. The bot will now terminate gracefully.")


async def handle_restart(request):
    bot = request.app["bot"]
    if request.app.get("restart_requested"):
        return web.Response(text="Restart is already in progress.")
    if request.app.get("shutdown_requested"):
        return web.Response(text="Shutdown is already in progress.")

    request.app["restart_requested"] = True
    logger.info("Restart command received via web interface.")

    async def perform_restart():
        try:
            schedule_restart()
            await close_bot_gracefully(bot)
        except Exception:
            logger.exception("Error while restarting the bot.")

    # Creating a task lets the HTTP response return before restart completes.
    asyncio.create_task(perform_restart())
    return web.Response(text="Restart signal sent. The bot will now restart gracefully.")

async def start_web_server(bot):
    app = web.Application()
    app["bot"] = bot
    app["started_at"] = time.monotonic()
    app.router.add_get("/", handle_logs)
    app.router.add_post("/restart", handle_restart)
    app.router.add_post("/shutdown", handle_shutdown)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, SERVER_HOST, SERVER_PORT)
    
    try:
        await site.start()
        logger.info(f"--- Log server running on http://{SERVER_HOST}:{SERVER_PORT} ---")
        logger.info(f"--- View logs at: http://{SERVER_HOST}:{SERVER_PORT} ---")
        logger.info(f"--- To restart bot, send a POST request to: http://{SERVER_HOST}:{SERVER_PORT}/restart ---")
        logger.info(f"--- To shutdown bot, send a POST request to: http://{SERVER_HOST}:{SERVER_PORT}/shutdown ---")
        await asyncio.Event().wait()
    finally:
        logger.info("Web server is shutting down.")
        await runner.cleanup()
