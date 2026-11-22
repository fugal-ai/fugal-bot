#!/bin/sh
# Wait for the bot's screen, then start the upstream gateway through the
# upstream container entrypoint (it runs `openclaw doctor --fix` first).
set -eu
i=0
while ! xdpyinfo -display :1 >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -gt 60 ] && break; sleep 0.5
done
cd /home/node

# One bot = one container = one gateway on this volume, and supervisord only
# starts the gateway after the previous one exited. A lock left by a gateway
# that did not shut down cleanly (container killed, host crash, container
# recreated: new PID namespace) can therefore be recovered safely. Upstream
# refuses to reclaim it automatically because in general it cannot know that.
lock=$(node /app/openclaw.mjs gateway recover-lock --json 2>/dev/null || true)
fp=$(printf '%s' "$lock" | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{try{const j=JSON.parse(s);if(j.present&&j.confirmation)process.stdout.write(j.confirmation)}catch{}})')
if [ -n "$fp" ]; then
  echo "[fugal-bot] recovering a stale gateway lock left by a previous run"
  node /app/openclaw.mjs gateway recover-lock --confirm "$fp" >/dev/null 2>&1 || true
fi
exec node /app/docker-entrypoint.mjs node /app/openclaw.mjs gateway --bind "${GATEWAY_BIND:-lan}" --port 18789
