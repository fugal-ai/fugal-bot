#!/bin/sh
# Wait for the bot's screen, then start the upstream gateway.
#
# `openclaw doctor --fix` (state migration and repair) runs only when this
# image has not run it on this data directory yet: after an image upgrade, or
# on a data directory from before this rule. Upstream's container entrypoint
# (/app/docker-entrypoint.mjs) would run it at every container start, and in a
# sandbox room it is most of the start: 111 s of a 210 s first start, 160 s of
# a 253 s restart (docs/CATHEDRAL.md). A brand-new data directory needs no
# repair: entrypoint.sh marks it as made by this image. Delete
# ~/.openclaw/fugal-doctor.ok to have doctor run at the next start.
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

image=$(cat /etc/fugal-bot/image-id 2>/dev/null || true)
marker="$HOME/.openclaw/fugal-doctor.ok"
done_by=$(cat "$marker" 2>/dev/null || true)
if [ -z "$image" ] || [ "$done_by" != "$image" ]; then
  echo "[fugal-bot] openclaw doctor --fix: first start of this image on this data directory"
  # As upstream's entrypoint runs it: the activation marker (in this
  # container's /tmp) lets doctor take its pre-repair capture once. Signals
  # are passed on, so a stop during doctor stops doctor cleanly.
  OPENCLAW_IMAGE_ACTIVATION_CAPTURE_MARKER=/tmp/openclaw-image-activation-fugal \
    node /app/openclaw.mjs doctor --fix --non-interactive &
  pid=$!
  trap 'kill -TERM "$pid" 2>/dev/null || true' TERM INT HUP QUIT
  rc=0
  wait "$pid" || rc=$?
  while kill -0 "$pid" 2>/dev/null; do rc=0; wait "$pid" || rc=$?; done
  trap - TERM INT HUP QUIT
  if [ "$rc" != 0 ]; then
    echo "[fugal-bot] openclaw doctor exited $rc; the gateway is not started (supervisord tries again)"
    exit "$rc"
  fi
  if [ -n "$image" ]; then
    printf '%s\n' "$image" > "$marker.tmp" && mv "$marker.tmp" "$marker"
  fi
else
  echo "[fugal-bot] openclaw doctor: already run by this image on this data directory, skipped"
fi
# The owner's name in the web UI: their short wallet address instead of the
# sign-in id "owner" (owner-name.mjs; while the gateway is stopped, never fatal).
node --no-warnings /opt/fugal-bot/owner-name.mjs || true
exec node /app/openclaw.mjs gateway --bind "${GATEWAY_BIND:-lan}" --port 18789
