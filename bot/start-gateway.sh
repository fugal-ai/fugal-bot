#!/bin/sh
# Wait for the bot's screen, then start the upstream gateway through the
# upstream container entrypoint (it runs `openclaw doctor --fix` first).
set -eu
i=0
while ! xdpyinfo -display :1 >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -gt 60 ] && break; sleep 0.5
done
cd /home/node
exec node /app/docker-entrypoint.mjs node /app/openclaw.mjs gateway --bind lan --port 18789
