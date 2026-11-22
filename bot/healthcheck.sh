#!/bin/sh
# Healthy = gateway answers its liveness probe AND the desktop bridge is up.
set -eu
node -e '
const get = (u) => fetch(u, { signal: AbortSignal.timeout(5000) }).then((r) => { if (!r.ok) throw new Error(u + " " + r.status); });
Promise.all([get("http://127.0.0.1:18789/healthz"), get("http://127.0.0.1:6080/vnc.html")])
  .then(() => process.exit(0), (e) => { console.error(String(e)); process.exit(1); });
'
