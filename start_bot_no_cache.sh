#!/bin/sh
set -eu
exec "$(dirname "$0")/start_bot.sh" --no-cache
