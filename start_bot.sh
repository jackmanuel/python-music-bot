#!/bin/sh
set -eu
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "Missing .venv/bin/python. Create the virtual environment first." >&2
    exit 1
fi

if [ ! -f src/music_bot/music_bot.pyw ]; then
    echo "Missing src/music_bot/music_bot.pyw." >&2
    exit 1
fi

if [ -n "$(ss -H -ltn 'sport = :8000')" ]; then
    echo "Port 8000 is already in use. If the bot is running, use ./stop_bot.sh first." >&2
    exit 1
fi

mkdir -p logs
nohup .venv/bin/python -u src/music_bot/music_bot.pyw "$@" \
    >> logs/launcher.log 2>&1 </dev/null &
echo "Launch requested (PID $!). Check logs/launcher.log for startup output."
