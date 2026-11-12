#!/bin/sh
set -eu
curl --fail --silent --show-error --max-time 10 \
    --request POST http://localhost:8000/shutdown
printf '\n'
