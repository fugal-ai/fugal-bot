#!/bin/sh
# Bot container entrypoint (runs as uid 1000 "node"; never root).
set -eu
umask 077

# The persistent volume is mounted at /home/node. First start: lay it out.
mkdir -p "$HOME/.openclaw" "$HOME/workspace" "$HOME/Desktop" "$HOME/.config"
[ -e "$HOME/Desktop/workspace" ] || ln -s "$HOME/workspace" "$HOME/Desktop/workspace"

# OpenAI-compatible clients want some key. This container only ever gets a
# placeholder: the real key stays in the modelproxy service, which replaces
# the Authorization header of every model request.
: "${MODEL_API_KEY:=held-by-modelproxy}"
export MODEL_API_KEY
: "${NOVNC_LISTEN:=0.0.0.0:6080}"
export NOVNC_LISTEN
: "${DESKTOP_GEOMETRY:=1280x800}"
export DESKTOP_GEOMETRY

# VNC password (VncAuth): generated once, kept on the volume (mode 600), never
# logged. x11vnc requires it; OpenClaw's built-in Desktop panel refuses an
# unauthenticated VNC server and reads it from this file; our noVNC page at
# /desktop/ (only reachable past the password gate) gets it in the URL
# fragment, which browsers never send to the server, so it is not in any log.
# VNC passwords are at most 8 characters.
mkdir -p "$HOME/.vnc"
VNC_PASSWORD_FILE="$HOME/.vnc/password.txt"
if [ ! -s "$VNC_PASSWORD_FILE" ]; then
  head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 8 > "$VNC_PASSWORD_FILE"
fi
chmod 600 "$VNC_PASSWORD_FILE"
export VNC_PASSWORD_FILE
# noVNC web root with a landing page that carries the password in the fragment.
rm -rf /tmp/novnc-web && cp -r /usr/share/novnc /tmp/novnc-web
vncpw=$(cat "$VNC_PASSWORD_FILE")
target="vnc.html?autoconnect=1&amp;resize=scale&amp;reconnect=1&amp;path=desktop/websockify#password=${vncpw}"
cat > /tmp/novnc-web/index.html <<HTML
<!doctype html><meta charset="utf-8"><title>Bot desktop</title>
<meta name="referrer" content="no-referrer">
<meta http-equiv="refresh" content="0; url=${target}">
<a href="${target}">Open the bot's desktop</a>
HTML
unset vncpw target

# Optional starter files for the agent's workspace (identity, house rules, the
# first-conversation script). Must run before render-config.mjs: "no config
# file yet" is how it knows the data directory is new. Existing files are
# never overwritten.
if [ "${SEED_WORKSPACE:-}" = "1" ]; then
  node /opt/fugal-bot/seed-workspace.mjs
fi

node /opt/fugal-bot/render-config.mjs

exec /usr/bin/supervisord -c /etc/fugal-bot/supervisord.conf
