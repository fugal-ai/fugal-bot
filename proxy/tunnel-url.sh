#!/bin/sh
# Reads the quick-tunnel hostname from cloudflared's local metrics endpoint and
# publishes it: to the logs and to TUNNEL_URL.txt at the top of the data volume.
last=""
while true; do
  h=$(wget -qO- -T 5 http://tunnel:2000/quicktunnel 2>/dev/null | sed -n 's/.*"hostname":"\([^"]*\)".*/\1/p')
  if [ -n "$h" ] && [ "$h" != "$last" ]; then
    echo "https://$h" > /v/TUNNEL_URL.txt
    echo "[tunnel] PUBLIC URL: https://$h   (also in TUNNEL_URL.txt in the data directory)"
    last="$h"
  fi
  sleep 10
done
