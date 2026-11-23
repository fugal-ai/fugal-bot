# Running Fugal Bot in a Cathedral confidential sandbox

Cathedral "rooms" are short-lived sandboxes on AMD SEV-SNP machines. A room
started from `docker:27-dind` already runs `dockerd`, has outbound internet,
no inbound address and no SSH; commands go through Cathedral's HTTPS exec API.
The room has Docker 27.5 with **Compose v2.33.0** (the overlay is validated
against that version). Four platform rules shape how the bot runs there:

1. **No NAT for containers.** Docker runs with `--bridge=none --iptables=false`
   under gVisor, so containers on a compose network have no internet and
   `host.docker.internal` does not resolve. `compose.cathedral.yaml` puts every
   service on the host network and wires them over `127.0.0.1` ports.
2. **Only data persists, not images.** Programs cannot run from `/workspace`,
   and Docker's data root is per room, so every new room pulls and builds the
   images again. All bot data lives under `DATA_DIR` on `/workspace` (10 GiB).
3. **A room lives at most 24 hours**, and the public tunnel URL changes with
   every new room.
4. **Files cannot carry Linux file capabilities** (gVisor), so the official
   `caddy` image, which sets one on its binary, cannot be unpacked. The
   overlay builds the proxy from Caddy's release archive instead
   (`proxy/Dockerfile.nocap`, SHA-512 checked) and uses the pinned
   python-alpine image for the two one-shot helpers.

The steps below use `cathedral.py`, a small command-line client for
Cathedral's sandbox API (create, push-dir, push-env, up, logs, extend, down,
delete). It is not part of this repository.
`cathedral.py up` runs `docker compose --project-directory /workspace/app
--profile tunnel up -d --wait` as a background command and prints the
`trycloudflare.com` URL from the tunnel's log. That `--wait` is a known
weakness: see "Starting: never `up --wait`" below.

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

`MODEL_BASE_URL` and `MODEL_API_KEY` reach only the `modelproxy` service, not
the agent (see "What differs" below for what that means in this layout).
Optional `BUDGET_*` caps: `.env.example`.

`COMPOSE_FILE` makes a plain `docker compose ...` in the room use the overlay.
Compose resolves `COMPOSE_FILE` from `.env` against the **working directory**,
not `--project-directory`, so run compose with the working directory set to
the app directory (`/workspace/app`).
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

**Cold start:** building in a room works but is slow. In the first real room
the bot image built with host networking, and exporting the built image alone
took about 12 minutes. Pulling published images is much faster. Set, in the env
file, digest-pinned published images:

```
BOT_IMAGE=<registry>/fugal-bot@sha256:...
MODELPROXY_IMAGE=<registry>/fugal-bot-modelproxy@sha256:...
GATE_IMAGE=<registry>/fugal-bot-gate@sha256:...
PROXY_IMAGE=<registry>/fugal-bot-proxy@sha256:...   # the proxy/Dockerfile.nocap build
```

(The `images` workflow writes these four lines, with digests, to its job
summary and into the `images.env` of the release bundle
`fugal-bot-room-<version>.tgz`, next to the compose files.)

and in the room (working directory `/workspace/app`):

```
docker compose --profile tunnel pull
docker compose --profile tunnel up -d --no-build
```

`pull` fetches the published images even though the services also have a
build section; `--no-build` guarantees nothing is built. Without
`--no-build`, compose still builds nothing as long as the images are present.
Nothing is published yet.

**Starting: never `up --wait`.** In real rooms a start took between 276 and
621 seconds, and when it takes more than about 6 minutes Docker reports the
bot as `unhealthy` for a while before it turns `healthy` by itself (the
image's health check allows a 300-second start period and then six tries).
`up --wait` gives up at that first `unhealthy`. Run `up -d`, which returns at
once, and then poll until the bot is healthy, with a long deadline (20
minutes) and treating `unhealthy` as "not yet":

```
docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q bot)"
```

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

## Hosted mode: a fixed hostname and sign-in from a site's page

`compose.hosted.yaml` is a third file on top of the two above, for a bot that
a site runs for its owner: the bot gets a fixed hostname through a Cloudflare
**named** tunnel, and the owner signs in on the site's page, which shows the
bot in a frame. It is switched on by the profile `hosted`:

```
COMPOSE_FILE=compose.yaml:compose.cathedral.yaml:compose.hosted.yaml
COMPOSE_PROFILES=hosted
DATA_DIR=/workspace/fugal-bot-data
BIND_ADDRESS=127.0.0.1
PORT=8080
PUBLIC_HOSTNAME=
ACCESS_PASSWORD=<32+ random characters; nobody types it>
GATE_SSO=on
GATE_BOT_ID=<the bot's id: "b" and 10 characters of a-z, 2-7>
GATE_LOGIN_URL=https://site.example/bot
# GATE_PARENT_ORIGIN=https://site.example   (default: the origin of GATE_LOGIN_URL)
SESSION_HOURS=24
TUNNEL_TOKEN=<token of the named tunnel>
BACKUP_AGE_RECIPIENT=<age public key>
```

plus the model and image settings as before. Then, in `/workspace/app`:

```
docker compose pull
docker compose up -d --no-build
```

What the profile does:

- **`tunnel`** runs `cloudflared tunnel --no-autoupdate --metrics
  127.0.0.1:2000 run --protocol http2`. The token comes from the environment
  variable `TUNNEL_TOKEN` only. The hostname and the target are set on
  Cloudflare's side, in the tunnel's ingress rule: it must send the hostname
  to `http://127.0.0.1:<PORT>`. The quick tunnel's helpers (`tunnel-guard`,
  `tunnel-url`) do not start, there is no `TUNNEL_URL.txt`, and
  `PUBLIC_HOSTNAME` stays empty (TLS ends at Cloudflare; the proxy listens
  on loopback in plain HTTP). Do not add `--profile tunnel` to this set of
  files. The `--metrics` flag matters: without it cloudflared's metrics port
  listens on every interface (`[::]:20241`), not on loopback.
- **While the room is down or starting**, Cloudflare answers for the hostname
  with HTTP 530 and the bare text `error code: 1033`, and the gate's page
  comes back minutes before the bot itself is healthy. Nothing in the room
  can change that: the page that frames the bot has to cover the frame with
  its own notice until the controller reports the bot as running. Start it
  as described above (`up -d`, then poll; never `up --wait`). The tunnel
  waits for the proxy only, so it is up while the bot is still starting.
- **Sign-in** is by one-time token (`GATE_SSO=on`). The site's controller
  keeps a copy of `ACCESS_PASSWORD` and mints, for a signed-in owner, a token
  `v1.<bot_id>.<exp>.<jti>.<mac>` that lives 60 seconds and works once; the
  site's page posts it as a form (fields `token` and `next`) into the frame,
  to `https://<bot host>/_gate/sso`. The gate answers 303 to `next` with the
  session cookie `__Host-pb_session`. The password form is switched off. A
  frame without a session shows "Signing you in" and sends
  `{type: "fugal-bot:auth-required"}` to the page around it (`postMessage`,
  to `GATE_PARENT_ORIGIN` only), which then posts a fresh token; a failed
  sign-in sends `{type: "fugal-bot:auth-failed"}` instead. The page signs the
  frame out with `POST https://<bot host>/_gate/logout` (credentials
  included). Only `GATE_PARENT_ORIGIN` may frame the bot. Details and limits:
  `SECURITY-NOTES.md`, "Hosted mode".
- **`backup`** is a one-shot that `up` never starts. It runs
  `/opt/fugal-bot/backup.sh cold` from the bot image with `DATA_DIR/home`
  mounted read-only, reads the upload address (a presigned URL) as one line
  on standard input, and encrypts to `BACKUP_AGE_RECIPIENT`:

  ```
  docker compose stop bot
  docker compose run --rm -T backup < url.txt
  docker compose start bot
  ```

  It needs a bot image that contains `backup.sh`.

Tested: the three files together with Compose 2.33.0 and dummy values
(`docker compose config`; with the profile the services are `volume-init`,
`bot`, `modelproxy`, `gate`, `proxy`, `tunnel`); cloudflared's flags and its
metrics address without a network connection; the sign-in, the framing rules
and the cookie in `scripts/smoke.sh`, in both layouts, behind a stand-in for
the site and for Cloudflare. A named tunnel was run in a real room by hand with the
same command minus `--metrics` (it connects under gVisor with `--protocol
http2`; the token was not found on the metrics port). **Not run:** this
overlay's `tunnel` service itself (it needs a real Cloudflare token), the
`backup` service, and the sign-in in a real room.

## What differs from the default layout, and what is untested

- All services share the room's network namespace. The gateway therefore
  accepts the proxy's identity header from `127.0.0.1`, so any process in the
  room, including the agent's own shell, could present it. The agent already
  runs inside the gateway with the same rights, so this widens access only
  for other processes in the room (there are none besides ours).
- Every listener binds to `127.0.0.1` (proxy 8080, health 8081, gate 9000,
  model proxy 9001, gateway 18789 and OpenClaw's neighbouring ports, noVNC
  6080, VNC 5900, cloudflared metrics 2000). Only the tunnel publishes
  anything.
- The model proxy (`modelproxy`, `127.0.0.1:9001`) holds the model key; the
  agent is configured with `http://127.0.0.1:9001/v1` and a placeholder. In
  this layout there is no network separation around it: every process in the
  room can call it and so spend inference, up to the budget. The key itself
  stays in the model proxy's container (its environment is not readable from
  the agent's container), and the public proxy does not route to port 9001.
  `SECURITY-NOTES.md`, "The model key", has the details.
- Seen working in a real room so far: the overlay validates with Compose
  2.33, the `Dockerfile.nocap` proxy image builds (archive checksum passed),
  the python-alpine helper image pulls.
- Tested locally: the loopback wiring (all services in one shared network
  namespace on an ordinary Docker host, `scripts/loopback-test.compose.yaml`),
  including the model proxy on `127.0.0.1:9001`, and the quick tunnel on the
  default layout.
- Not yet confirmed in a real room: the bot image build under gVisor,
  `DATA_DIR` bind mounts on `/workspace` (Cathedral reports that they work for
  data and SQLite), the cold-start time, and Chromium under gVisor.
