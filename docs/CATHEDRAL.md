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

Optional, for a bot that should introduce itself and be backed up (all in
`.env.example`; the model values there are the tested ones):

```
BOT_DISPLAY_NAME=<name>
BOT_EMOJI=<one emoji>
UI_ACCENT_COLOR=#2f6fed
SEED_WORKSPACE=1
OWNER_WALLET_SS58=<the owner's public address>
BACKGROUND_MODEL_USE=off
BACKUP_AGE_RECIPIENT=age1...        # the public key; the private key stays with you
```

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

The images are published by the `images` workflow to `ghcr.io/fugal-ai`
(`fugal-bot`, `fugal-bot-modelproxy`, `fugal-bot-gate`, `fugal-bot-proxy`),
tagged with the commit they were built from and, for a release, with its tag.
Three places give the digests: the summary of the workflow run, `images.env`
in the bundle, and the registry itself, for example
`docker buildx imagetools inspect ghcr.io/fugal-ai/fugal-bot:<commit or tag>`.
With the bundle a room needs no copy of this repository: the three compose
files, the four lines of `images.env` and your own settings in `.env` are
enough (`scripts/hosted-test.sh` starts a bot from exactly that).

**Starting: never `up --wait`.** In real rooms a start took between 276 and
621 seconds, and when it takes more than about 6 minutes Docker reports the
bot as `unhealthy` for a while before it turns `healthy` by itself (the
health check of the images published up to 2026-11-22 allows a 300-second
start period and then six tries; later images allow 900 seconds).
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
off-site backup (section 4).

## 4. Back up and restore in a room

`/workspace` is best effort and a room is replaced every day, so take a
backup before each replacement and keep it somewhere else. The scripts are in
the bot image (`README.md`, "Back up and restore"; what a backup holds and
who can read it: `SECURITY-NOTES.md`). This section is for a bot you run
yourself with the two files above; a hosted bot has one-shot services for the
same two scripts ("Controller contract" below). Both read a presigned URL from
standard input, so send it as the exec call's input, not inside the command:
commands are kept in Cathedral's exec logs, input is not part of the command
line. Working directory `/workspace/app`.

```
# while the bot runs (9 to 13 s on a new bot)
docker compose exec -T bot /opt/fugal-bot/backup.sh hot           < the PUT URL

# before deleting the room: stop the bot, then a cold backup
docker compose stop bot
docker compose run --rm --no-deps -T --entrypoint /opt/fugal-bot/backup.sh bot cold   < the PUT URL
```

Each prints one line of JSON with the size and SHA-256 of the encrypted
object. To bring a bot back when `/workspace/fugal-bot-data/home` came back
empty: put the age identity in a file outside `/workspace` (for example
`/work/restore/identity.txt`; `/work` is wiped with the room), then, before
starting the bot:

```
docker compose up volume-init
docker compose run --rm --no-deps -T -v /work/restore/identity.txt:/run/backup.key:ro \
    --entrypoint /opt/fugal-bot/restore.sh bot /run/backup.key    < the GET URL
```

and delete the identity file. The file must be readable by uid 1000, the
bot's user. `restore.sh` refuses a data directory that already holds files
unless it is given `--force`. While the identity is in the room, Cathedral's
operator can read it.

Not yet run in a real room: both scripts (they were tested on an ordinary
Docker host and in the shared-loopback test layout), and scratch space: a hot
backup writes about twice the data's size under `/tmp` inside the bot's
container, which is on the room's own disk, not on `/workspace`.

## 5. Logs and stopping

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
- **`backup`** and **`restore`** are one-shots that `up` never starts. They
  run `backup.sh cold` and `restore.sh` from the bot image as the bot's user.
  The commands, their input, output and exit codes are in "Controller
  contract" below.

Tested: the three files together with Compose 2.33.0 and dummy values
(`docker compose config`; with the profile the services are `volume-init`,
`bot`, `modelproxy`, `gate`, `proxy`, `tunnel`); cloudflared's flags and its
metrics address without a network connection; the sign-in, the framing rules
and the cookie in `scripts/smoke.sh`, in both layouts, behind a stand-in for
the site and for Cloudflare; and the whole hosted set of files, started from a
directory that holds nothing but the bundle and a `.env`, in
`scripts/hosted-test.sh` (sign-in, chat, name and colour in the frame,
desktop, hot and cold backup, the data directory deleted, restore, start). A
named tunnel was run in a real room by hand with the same command minus
`--metrics` (it connects under gVisor with `--protocol http2`; the token was
not found on the metrics port). **Not run:** this overlay's `tunnel` service
itself (it needs a real Cloudflare token; only its definition is checked),
and the sign-in, `backup` and `restore` in a real room.

## Controller contract

What a site's controller can rely on when it runs a hosted bot. Everything
here except the tunnel is exercised by `scripts/hosted-test.sh`.

**The app directory** holds the three compose files from
`fugal-bot-room-<version>.tgz` and one `.env` (mode 600): the bot's settings
(`.env.example`, "hosted mode", lists them all) followed by the four lines of
`images.env`. Nothing else: no source tree, nothing is built. Every command
below runs with the app directory as its working directory. Put it under
`/work` (for example `/work/app`) and the secrets in `.env` go when the room
goes; `DATA_DIR` is on `/workspace`.

Compose reads the whole of `.env` for every command, the one-shots included,
and refuses to go on when one of `DATA_DIR`, `ACCESS_PASSWORD`,
`MODEL_BASE_URL`, `MODEL_NAME` or `TUNNEL_TOKEN` is missing or empty. So the
file has to be complete before the first command, and stay in place for a
backup or a restore.

**Services.** `docker compose up` with `COMPOSE_PROFILES=hosted` starts
`volume-init` (a one-shot that lays out `DATA_DIR` and exits 0), `modelproxy`,
`gate`, `proxy`, `bot` and `tunnel`. `backup` and `restore` are one-shots
that only `docker compose run` starts. `tunnel-guard` and `tunnel-url` belong
to the quick tunnel and never start here.

**Start, stop, change.**

```
docker compose pull                    # the four images, the helper image, cloudflared
docker compose up -d --no-build        # returns in seconds; does not wait for the bot
docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q bot)"
                                       # poll: "starting" and "unhealthy" mean not yet,
                                       # "healthy" means ready; allow 20 minutes
docker compose stop bot                # the agent only; up to 45 s
docker compose start bot
docker compose down                    # everything; the data stays in DATA_DIR
```

To change a setting, rewrite `.env` and run `docker compose up -d --no-build`
again: only the services whose settings changed are replaced.

**Sign-in** is described under "Hosted mode" above: the token
`v1.<bot_id>.<exp>.<jti>.<mac>` goes in the form field `token` of a POST to
`https://<bot host>/_gate/sso`, with `next`; the answer is 303 and the cookie
`__Host-pb_session`, or 403.

**Backups and restore.** Each command reads exactly one line on standard
input, the presigned URL (`https://` only, at most 8192 characters), and
never takes it as an argument: commands are kept in exec logs, their input is
not. The URL is not printed, not even in an error.

```
# hot: while the bot runs
docker compose exec -T bot /opt/fugal-bot/backup.sh hot       < the PUT URL

# cold: with the bot stopped, for example before the room is replaced
docker compose stop bot
docker compose run --rm -T backup                             < the PUT URL
docker compose start bot

# restore: the bot stopped; DATA_DIR empty or missing
#   the age identity, as a file readable by uid 1000, for this one run:
sh -c 'umask 077 && rm -rf /work/restore && mkdir -p /work/restore \
       && cat > /work/restore/identity.txt \
       && chown 1000:1000 /work/restore/identity.txt'         < the identity
docker compose run --rm -T restore                            < the GET URL
rm -f /work/restore/identity.txt
docker compose up -d --no-build

# restore over a DATA_DIR that still holds files (they are deleted, but only
# after the backup has been downloaded, decrypted and checked)
docker compose run --rm -T restore --force /run/restore/identity.txt   < the GET URL
```

`/work/restore/identity.txt` is the default place on the host;
`RESTORE_IDENTITY_FILE` in `.env` names another. Inside the container it is
always `/run/restore/identity.txt`, which is why that path follows `--force`.
`restore` runs `volume-init` first, so `DATA_DIR` gets its layout even when
it came back empty.

Standard output is exactly one line of JSON; Docker's and compose's own
messages go to standard error.

```
backup   {"ok":true,"kind":"hot"|"cold","format":1,"bytes":N,"sha256":"...","seconds":S,"openclaw":"<version>"}
restore  {"ok":true,"kind":"hot"|"cold","created":"<when the backup was made>","bytes":N,"sha256":"...","files":N,"seconds":S}
either   {"ok":false,"error":"<what went wrong>"}
```

`bytes` and `sha256` describe the encrypted object as uploaded or downloaded.

| Exit status | Standard output | Meaning |
|---|---|---|
| 0 | `{"ok":true,...}` | Done. |
| 1 | `{"ok":false,"error":...}` | The script refused or failed, and says why: the bot is running (cold backup, restore), no `BACKUP_AGE_RECIPIENT`, no usable URL on standard input, the data directory holds no bot (`backup`: never an empty backup), the upload or download failed or was refused (`HTTP 403` for an expired URL, `404` for a missing object), the identity file cannot be read or does not decrypt the object, the data directory is not empty (restore without `--force`). Nothing was changed in the data directory, except after `unpacking failed part-way`, which asks for `--force`. |
| not 0 | empty | Docker could not run the command at all; the reason is on standard error. Examples: the bot is not running (`exec`), the image is not pulled, a mount failed. 137 means the container was killed (out of memory). |

So: a line of JSON with `"ok":true` and exit status 0 is the only success.
Depending on the Compose version, a missing `DATA_DIR/home` or a missing
identity file either stops Docker from starting the container (no JSON) or
is created by Docker as an empty directory, which the script then refuses
(JSON, exit 1). In the second case an empty directory is left at that path:
that is why the command above removes `/work/restore` before it writes the
identity.

A transfer is tried up to three times and each try may take 900 seconds
(`BACKUP_UPLOAD_TIMEOUT`, `BACKUP_DOWNLOAD_TIMEOUT`, in seconds), so do not
give up on a command before that.

Scratch space is `/tmp` inside the container that runs the script, which is
the container's own writable layer on Docker's disk: not `/workspace`, and
not memory. A hot backup needs about twice the size of the data there, a cold
one the size of the compressed archive, a restore the encrypted object plus
the decrypted archive.

Not part of a backup, so new after a restore: the model proxy's usage
counters, the VNC password, caches, and everything that lives only in `.env`.
`README.md`, "Back up and restore", has the full list.

## Measured in rooms (2026-11-23, `sealed-4x8`, published images, test model)

- **Faster start (2026-11-23, later; images of e20571d, controller with a
  second Docker daemon on `/work`, real model).** `openclaw doctor --fix` now
  runs only at the first start of an image on a data directory
  (`bot/start-gateway.sh`): a new bot's gateway was listening 38 s after the
  container started, up -> healthy 73 s. A new bot: create -> running
  **4 min 47 s** (pull of all images 144 s on the second daemon, `up` 39 s,
  healthy 73 s), then a real first conversation (greeting 1 call; daily stake
  report 6 calls; job isolated, light context; BOOTSTRAP.md retired). A user
  restart: **5 min 35 s** in all, the address unavailable for **4 min 46 s**
  (cold backup 9 s, room swap 18 s, pull 159 s, `up` 38 s, healthy 88 s).
  Before: 6.7 to 14.5 min for a new bot and about 12 min for a restart.
  Locally (`scripts/hosted-test.sh`): healthy 16-29 s without doctor, 81 s
  at the first start after an image change (doctor runs once).

- **Start is slow.** From `up -d` to a healthy bot took 276 to 621 s, most of
  it reading a freshly unpacked image; pulling and unpacking the bot image
  took another 4 to 8 minutes. Whatever starts the bot must wait at least 15
  minutes and must not treat a slow start as a failure. The image's health
  check now allows 900 s before it counts failures (it was 300 s, and a
  start that took longer was shown as `unhealthy` on its way to healthy).
  Nothing was found that makes the start itself shorter: keeping Node's
  compile cache on the data directory was tried locally and was slower
  (73 to 78 s against 66 s), because the image already carries one.
- **Memory.** The bot container used 1.65 GB idle, 2.3 to 3.0 GB with its
  browser open and peaked at 3.33 GB during a browser check, against the 4 GB
  limit (`BOT_MEM_LIMIT=4g`). Do not lower the limit, and do not turn on
  anything that keeps a second browser or a large model in memory. A hot
  backup runs OpenClaw's command-line program next to the gateway: about
  0.3 GB more for the seconds it takes (323 MB measured locally), which
  still fits under 4 GB at that peak, with little to spare.
- **Desktop in a browser tab.** Through a Cloudflare tunnel an idle
  `/desktop/` connection was closed after 125 s. The desktop bridge now sends
  a websocket ping every 30 s (`websockify --heartbeat=30`), which kept an
  idle session open in a room.
- **Desktop panel in the web chat.** It failed in rooms with "gateway host
  desktop is unavailable at 127.0.0.1:5900": the VNC server took 1.1 to 1.9 s
  to greet a new connection and OpenClaw allows 1.5 s for the whole
  handshake, with no setting for it. The VNC server now greets connections
  from their own thread (`x11vnc -threads`). Locally that took the handshake
  from 200-390 ms (400-890 ms with the container held to 0.3 CPU) to about
  100 ms in both cases. **Not yet confirmed in a room.**
- **The agent's browser in a room** (a real bot, real model, from OpenClaw's
  own records): starting Chromium and opening a heavy JavaScript page 21 s
  (12 s of it the browser start), later navigations 4 to 15 s, a snapshot or
  a `text` read about 1 s. Locally, without gVisor: 16 to 17 s for the first
  open, 4 s for a snapshot, under 1 s for `text`. So the browser works under
  gVisor and is not slow enough to hit a time limit. Locally, taostats.io's
  subnet page answered the agent's browser with a bot check ("Couldn't verify
  your browser") and no figures; in the room the snapshot of that page had
  the same size (711 characters), the page text was not read there. Its home
  page loaded locally.

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
