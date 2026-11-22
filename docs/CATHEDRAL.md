# Running Fugal Bot in a Cathedral confidential sandbox

Cathedral "rooms" are short-lived sandboxes on AMD SEV-SNP machines. A room
started from `docker:27-dind` already runs `dockerd`, has outbound internet,
no inbound address and no SSH; commands go through Cathedral's HTTPS exec API.
Three platform rules shape how the bot runs there:

1. **No NAT for containers.** Docker runs with `--bridge=none --iptables=false`
   under gVisor, so containers on a compose network have no internet and
   `host.docker.internal` does not resolve. `compose.cathedral.yaml` puts every
   service on the host network and wires them over `127.0.0.1` ports.
2. **Only data persists, not images.** Programs cannot run from `/workspace`,
   and Docker's data root is per room, so every new room pulls and builds the
   images again. All bot data lives under `DATA_DIR` on `/workspace` (10 GiB).
3. **A room lives at most 24 hours**, and the public tunnel URL changes with
   every new room.

The steps below use `cathedral.py`, a small command-line client for
Cathedral's sandbox API (create, push-dir, push-env, up, logs, extend, down,
delete). It is not part of this repository.
`cathedral.py up` runs `docker compose --project-directory /workspace/app
--profile tunnel up -d --wait` as a background command and prints the
`trycloudflare.com` URL from the tunnel's log.

## 1. Prepare the env file (on your machine, never committed)

Start from `.env.example` and set at least:

```
COMPOSE_FILE=compose.yaml:compose.cathedral.yaml
DATA_DIR=/workspace/fugal-bot-data
ACCESS_PASSWORD=<12+ characters, long and random>
BIND_ADDRESS=127.0.0.1
PORT=8080
MODEL_BASE_URL=https://llm.chutes.ai/v1
MODEL_API_KEY=<key>
MODEL_NAME=moonshotai/Kimi-K2.6-TEE
MODEL_SUPPORTS_TOOLS=true
MODEL_CONTEXT_WINDOW=262144
MODEL_MAX_TOKENS=8192
BOT_MEM_LIMIT=4g
# optional: TELEGRAM_BOT_TOKEN=...
```

`COMPOSE_FILE` makes a plain `docker compose ...` in the room use the overlay.
Cathedral's operator can read this file, both when it is uploaded (plain API
body) and on the host disk under `/workspace`. Use keys you can revoke.

## 2. Create a room and start the bot

```bash
python3 cathedral.py create --size sealed-4x8 --image docker:27-dind \
    --workspace fugal-bot --lifetime 86400 --max-spend 0.50
python3 cathedral.py push-dir .                 # the repository (skips .git and .env)
python3 cathedral.py push-env fugal-bot.env     # your env file -> /workspace/app/.env (mode 600)
python3 cathedral.py exec mkdir -p /workspace/fugal-bot-data
python3 cathedral.py up --health-timeout 1200   # builds, starts, prints the public URL
```

Open the printed `https://<random>.trycloudflare.com/` and log in with the
access password. The URL is also in `/workspace/fugal-bot-data/TUNNEL_URL.txt`
and in `python3 cathedral.py logs tunnel-url`.

**Cold start (estimated, not yet measured in a room):** the first `up` in a
new room pulls the OpenClaw image (about 1 GB) and installs the desktop and
Chromium packages, roughly 4 to 8 minutes. After that, the bot itself takes
about 30 to 60 seconds to become healthy. To skip the build, publish the bot
image once and set `BOT_IMAGE=<registry>/fugal-bot@sha256:...` in the env file
(nothing is published yet).

## 3. Keep it alive

- Extend within the 24-hour limit: `python3 cathedral.py extend 3600`.
- Beyond 24 hours, recreate on the same workspace (one live room per
  workspace, so delete first):

```bash
python3 cathedral.py delete
python3 cathedral.py create --size sealed-4x8 --image docker:27-dind \
    --workspace fugal-bot --lifetime 86400 --max-spend 0.50
python3 cathedral.py exec ls /workspace/app/.env /workspace/fugal-bot-data/home
#   if either is missing (the workspace was lost): push-dir and push-env again
python3 cathedral.py up --health-timeout 1200
```

Conversations, memory, files and browser logins are kept (they are in
`DATA_DIR`); the public URL changes; expect several minutes of downtime per
cycle because the images are rebuilt. `/workspace` is best effort: keep an
off-site backup.

## 4. Logs and stopping

- `python3 cathedral.py logs bot` (the client masks `.env` values inside the
  room before output leaves it). Do not run `docker compose config`, `env` or
  `docker inspect` through a plain exec: their output is kept in Cathedral's
  exec logs in plain text.
- `python3 cathedral.py down` stops the stack; `delete` ends the room. The
  data under `/workspace` stays.

## What differs from the default layout, and what is untested

- All services share the room's network namespace. The gateway therefore
  accepts the proxy's identity header from `127.0.0.1`, so any process in the
  room, including the agent's own shell, could present it. The agent already
  runs inside the gateway with the same rights, so this widens access only
  for other processes in the room (there are none besides ours).
- Every listener binds to `127.0.0.1` (proxy 8080, health 8081, gate 9000,
  gateway 18789 and OpenClaw's neighbouring ports, noVNC 6080, VNC 5900,
  cloudflared metrics 2000). Only the tunnel publishes anything.
- Tested locally: the loopback wiring (all services in one shared network
  namespace on an ordinary Docker host, `scripts/loopback-test.compose.yaml`),
  and the quick tunnel on the default layout.
- Not tested in a real room: the build with `network: host` under gVisor,
  `DATA_DIR` bind mounts on `/workspace` (Cathedral reports that they work for
  data and SQLite), the cold-start time, Chromium under gVisor, and
  `up --wait` with Compose v2.33 (the stack has two one-shot services,
  `volume-init` and `tunnel-guard`).
