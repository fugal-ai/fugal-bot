#!/bin/sh
# Bot container entrypoint (runs as uid 1000 "node"; never root).
set -eu
umask 077

# The persistent volume is mounted at /home/node. First start: lay it out.
mkdir -p "$HOME/.openclaw" "$HOME/workspace" "$HOME/Desktop" "$HOME/.config"
[ -e "$HOME/Desktop/workspace" ] || ln -s "$HOME/workspace" "$HOME/Desktop/workspace"

# Ollama and some local servers accept any key; OpenAI-compatible clients still
# want one. A real key arrives only through the environment (.env), never the image.
: "${MODEL_API_KEY:=not-needed}"
export MODEL_API_KEY
: "${DESKTOP_GEOMETRY:=1280x800}"
export DESKTOP_GEOMETRY

node /opt/fugal-bot/render-config.mjs

exec /usr/bin/supervisord -c /etc/fugal-bot/supervisord.conf
