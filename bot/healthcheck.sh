#!/bin/sh
# Healthy = the gateway admits traffic (/startupz: the agent database is open,
# so a chat right after "healthy" does not get "Agent database is starting")
# AND the desktop bridge is up. (/healthz is only liveness and turns 200 earlier.)
set -eu
node -e '
const get = (u) => fetch(u, { signal: AbortSignal.timeout(5000) }).then((r) => { if (!r.ok) throw new Error(u + " " + r.status); });
Promise.all([get("http://127.0.0.1:18789/startupz"), get("http://127.0.0.1:6080/vnc.html")])
  .then(() => process.exit(0), (e) => { console.error(String(e)); process.exit(1); });
'
