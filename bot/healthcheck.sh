#!/bin/sh
# Healthy = the gateway admits traffic AND its first chat turn is warm AND the
# desktop bridge is up. (/healthz is only liveness and turns 200 earlier.)
#
# /startupz: the agent database is open, so a chat right after "healthy" does
# not get "Agent database is starting". /readyz additionally waits until the
# reply and connection modules are loaded ("coreReady"). Before, the bot was
# reported healthy on /startupz alone and the first reply of a new bot paid for
# that loading (26-47 s before the model was even called, measured in a room).
# readyz also fails while a channel (Telegram) is unhealthy; that must not make
# the bot unhealthy, so only coreReady is required (the details are given to
# local callers; without them this check is skipped), and after 10 minutes of
# uptime /startupz alone is enough.
set -eu
node -e '
const get = (u) => fetch(u, { signal: AbortSignal.timeout(5000) }).then((r) => { if (!r.ok) throw new Error(u + " " + r.status); });
const core = () => fetch("http://127.0.0.1:18789/readyz", { signal: AbortSignal.timeout(5000) })
  .then((r) => r.json())
  .then((j) => { if (j.coreReady === false && !(j.uptimeMs > 600000)) throw new Error("readyz: " + JSON.stringify(j.failing || j)); });
Promise.all([get("http://127.0.0.1:18789/startupz"), core(), get("http://127.0.0.1:6080/vnc.html")])
  .then(() => process.exit(0), (e) => { console.error(String(e)); process.exit(1); });
'
