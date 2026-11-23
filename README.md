# Fugal Bot

An AI agent with its own Linux computer, packaged as one Docker Compose app.
You chat with it in a web page (or Telegram); it works in its own shell, files
and Chromium browser, and you can watch and take over its desktop in a tab.
It uses any OpenAI-compatible model endpoint, and its memory survives restarts.

This is **stage 0, a test version**. It runs on an ordinary host, and whoever
controls that host can read everything in it. Read "What is private today"
below before you put anything sensitive in it.

The agent is [OpenClaw](https://github.com/openclaw/openclaw) (MIT), pinned to
release v2026.11.6 by image digest, with its marketplace, installs, update
checks and telemetry switched off. Details: `UPSTREAM.md`, `SECURITY-NOTES.md`.

## Quickstart

Needs Docker with the compose plugin, about 3 GB of free RAM, and an x86-64
machine (the upstream image is amd64 only).

```bash
cp .env.example .env
# edit .env: ACCESS_PASSWORD, and MODEL_BASE_URL / MODEL_NAME / MODEL_API_KEY
docker compose up -d --build
```

The first build downloads the upstream image and Chromium (several minutes).
When `docker compose ps` shows the services healthy, open
**http://127.0.0.1:8080/** and enter the access password.

- `/` is the web chat (OpenClaw's Control UI): chat, Settings, Channels,
  Automations, Agents, Sessions.
- `/desktop/` is the bot's screen: a terminal, and Chromium when the agent
  browses. Click and type to take over; you and the agent share it. The same
  screen is also in the web chat's side panel (panel button at the top right,
  then **Desktop**, then **Take control**).
- `/_gate/logout` logs out.

The agent can only use its computer with a model that supports tool calls.
Small local models often cannot (Ollama rejects tool requests for `gemma3:4b`
and `deepseek-r1:8b`); for those set `MODEL_SUPPORTS_TOOLS=false` and the bot
will only chat.

## Use a Bittensor inference endpoint (Chutes)

Chutes (Bittensor subnet 64) serves open models through an OpenAI-compatible
API. Create a key at https://chutes.ai/app/settings/api-keys, then in `.env`:

```
MODEL_BASE_URL=https://llm.chutes.ai/v1
MODEL_API_KEY=<your Chutes key>
MODEL_NAME=moonshotai/Kimi-K2.6-TEE
MODEL_SUPPORTS_TOOLS=true
MODEL_CONTEXT_WINDOW=262144
MODEL_MAX_TOKENS=8192
```

and `docker compose up -d`. The current list, with prices and a
`confidential_compute` flag, is at `https://llm.chutes.ai/v1/models`; any
entry whose `supported_features` include `tools` should work. It is paid per
token, and every agent turn sends the whole conversation plus OpenClaw's
system prompt, so a turn can cost tens of thousands of input tokens.

Any other OpenAI-compatible endpoint works the same way: change the `MODEL_*`
lines and restart. For example Engy (`https://api.engy.ai/v1`), gm
(`https://api.saygm.com/v1`), vLLM, LiteLLM, OpenRouter or a local Ollama.

## Model key, usage and budget

The agent never holds the model API key. `MODEL_BASE_URL` and `MODEL_API_KEY`
go only to a small separate service, `modelproxy`; the agent container is
configured with that service's internal address and a placeholder key. The
model proxy puts the real key on each request, passes the reply back as it
arrives (streaming and tool calls included), and forwards nothing but
inference calls (`/v1/chat/completions`, `/v1/completions`, `/v1/responses`,
`/v1/embeddings`, `/v1/models`). It has no published port and is not behind
the public proxy. What this does and does not protect: `SECURITY-NOTES.md`.

It also counts the prompt and completion tokens of every call, per UTC day
and in total, in `modelproxy/usage.json` in the data volume (it survives
restarts). To read the counters:

```bash
docker compose exec modelproxy python3 -I -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9001/usage').read().decode())"
```

Optional hard caps in `.env`: `BUDGET_TOKENS_PER_DAY`, `BUDGET_TOKENS_TOTAL`,
`BUDGET_USD_PER_DAY` and `BUDGET_USD_TOTAL` (these two need `BUDGET_PRICES`,
a price table per model), and `BUDGET_REQUESTS_PER_MINUTE`. Once a cap is
reached the model proxy refuses further model calls, and instead of an answer
the web chat shows "Your request couldn't be completed: LLM request rejected:
This bot has used up its inference allowance for today and it renews at
midnight UTC" (for a total cap: "... its whole inference allowance and the
owner of the bot can raise it"). That lasts until the day ends (daily caps)
or you raise the cap and run `docker compose up -d`; the next message is then
answered at once. A cap is checked before each call, so the last call can
overshoot it by its own size. If an endpoint reports no token usage, the
model proxy counts a rough estimate and marks it (`estimated_requests`).

The refusal is an HTTP 400 on purpose: the usual `insufficient_quota` forms
(429, 402) make OpenClaw switch the model provider off for ten minutes, also
after the cap is raised. The measurements, the other forms
(`MODELPROXY_EXHAUSTED_REPLY`) and the proxy's second role, a central gateway
for many bots with per-bot tokens and allowances, are in `docs/MODELPROXY.md`.

## Run it on a rented machine

**With a public address and a DNS name** (any Linux server with Docker):

```
BIND_ADDRESS=0.0.0.0
PORT=443
PUBLIC_HOSTNAME=bot.example.com
ACCESS_PASSWORD=<at least 12 characters, long and random>
```

`docker compose up -d --build`. Caddy gets a Let's Encrypt certificate by
itself, using the TLS-ALPN challenge on port 443 only. The proxy refuses to
start on a non-loopback address without a password of 12 or more characters,
and refuses plain HTTP there unless `ALLOW_PUBLIC_HTTP=yes`.

**Without any inbound address** (a sandbox or a machine behind NAT): keep
`BIND_ADDRESS=127.0.0.1` and turn on the tunnel profile:

```bash
docker compose --profile tunnel up -d --build
docker compose logs tunnel-url        # prints https://<random>.trycloudflare.com
```

This starts a Cloudflare quick tunnel (no account needed) that dials out and
publishes the proxy at a random `trycloudflare.com` URL. The URL is also
written to `TUNNEL_URL.txt` in the data directory, and a new URL is issued
whenever the tunnel restarts. The tunnel will not start unless
`ACCESS_PASSWORD` has 12 or more characters. Cloudflare terminates TLS and can
see the traffic.

**Keep the data in a host directory** (for example a sandbox's persistent
disk) instead of a Docker volume:

```
DATA_DIR=/workspace/fugal-bot-data      # must exist: mkdir -p it first
COMPOSE_FILE=compose.yaml:compose.datadir.yaml
```

The stack runs under a plain `dockerd`, including Docker-in-Docker; it needs no
systemd and no privileged containers. It is limited to about 2.8 GB of RAM
(`BOT_MEM_LIMIT`, `BOT_CPUS`).

**Hosts where containers get no NAT** (for example Cathedral confidential
sandboxes, where Docker runs with `--bridge=none --iptables=false`): add
`compose.cathedral.yaml`, which puts every service on the host network and
wires them over `127.0.0.1`. Step-by-step: `docs/CATHEDRAL.md`.

**First start takes a while**: building the bot image downloads the OpenClaw
image (about 1 GB) and the desktop and Chromium packages, typically several
minutes on a fresh machine; later starts reuse the image. To skip building,
set `BOT_IMAGE`, `MODELPROXY_IMAGE`, `GATE_IMAGE` and `PROXY_IMAGE` to
published, digest-pinned images and run `docker compose pull` then
`docker compose up -d --no-build` (none is published yet).

## Login page

The login page shows the name, one sentence about the bot and the password
field. Three optional lines appear only when set in `.env`: `HOST_LABEL`
(where it runs), `MODEL_LABEL` (which model; nothing about the model is shown
otherwise) and `REPO_URL` (a link to the source). Values are HTML-escaped; the
page loads no external assets.

## Telegram

1. Create a bot with @BotFather and copy its token.
2. In `.env`: `TELEGRAM_BOT_TOKEN=<token>`. Optionally
   `TELEGRAM_ALLOWED_USER_IDS=<your numeric Telegram user id>`.
3. `docker compose up -d`.
4. Send the bot a direct message. Without an allowlist, it replies with a
   pairing code; approve it in the web UI under **Settings → Channels → DM
   access requests**.

It uses long polling, so it needs no webhook and no extra port. Group chats
are off.

## Schedules, named agents, approvals (built into OpenClaw)

- **Automations**: ask in chat ("every weekday at 8:00, summarise the front
  page of example.com for me") or use the Automations page. Results arrive in
  the chat or on Telegram. Needs a tool-capable model.
- **Named agents**: add agents with their own name, instructions, memory and
  workspace on the Agents page.
- **Approvals**: `EXEC_MODE=ask` makes the agent ask before shell commands that
  are not on its allowlist; you approve in the web UI. The default, `full`,
  runs commands without asking.

## What is private today

- **Not private from the host.** Conversations, memory, files, browser logins,
  the model key and the access password are all readable by whoever runs the
  machine (the model proxy keeps the key from the agent, not from the host).
  Disks are not encrypted by this package.
- **Not private from the model provider.** Every prompt, tool result and page
  the agent reads goes to `MODEL_BASE_URL`. Chutes' `-TEE` models are
  advertised as running in confidential VMs; this package does not verify that.
- **Not private from Telegram** (if enabled) or **Cloudflare** (if the tunnel
  is on).
- **Is** protected by one access password on every page, the desktop and the
  API, with rate-limited logins, a single published port, and containers
  without root or Linux capabilities. The agent cannot read the model API key
  (a separate container holds it), but it can read its Telegram token and
  reach the internet freely; see `SECURITY-NOTES.md`.

## Roadmap to sealed boxes

1. Run this same compose app inside an Intel TDX confidential VM (dstack), one VM per bot.
2. Encrypt the data disk with a key released only to the attested VM.
3. Terminate TLS inside the VM and let users check attestation evidence for it.
4. Enter secrets only into the attested VM. (The model key is already out of the agent's reach, in the model proxy; the Telegram token is not yet.)
5. Allow outbound traffic only to the model endpoint, the chat platform and what the user's tasks need.

## Stop, data, wipe

- Stop: `docker compose down` (data kept). Start: `docker compose up -d`.
- Data: one Docker volume, `fugal-bot_botdata` (`<BOT_NAME>_botdata`), or
  `DATA_DIR`. `home/` is the bot's home (`~/workspace`; `~/.openclaw` holds the
  config, sessions, memory and browser profile); `proxy/` holds the TLS
  certificate; `modelproxy/` the token counters; `TUNNEL_URL.txt` the tunnel
  address.
- Wipe everything: `docker compose down -v` (named volume), or delete
  `DATA_DIR`'s contents.
- Back up: `docker run --rm -v fugal-bot_botdata:/v -v "$PWD":/out alpine tar czf /out/bot-backup.tgz -C /v .`
  The archive holds the full conversation history and browser cookies.

## Smoke test

`scripts/smoke.sh [--fresh] [--keep]` builds and runs a separate copy (project
`fugal-bot-smoke`, port 18080, its own volume) and checks, through the proxy:
the login gate, the web UI, a model reply, a shell command, file and browser
page made by the agent, then drives the real web chat in headless Chromium
(websocket origin checks, a message typed in the page and its reply, the
desktop page connecting), then restarts and checks that the file, the
conversation and the token counters are still there. It also checks the model
proxy: that its counters go up, that the model API key cannot be found in the
agent container (environment, `/proc`, files, data directory), that it is not
reachable from outside, and last that a very low budget makes it refuse, that
the user is told why, and that the next message is answered once the budget
is raised.

By default the model is a test-only scripted one (`scripts/scripted-brain/`)
placed behind the model proxy like a real endpoint, so the run needs no paid
call, no real key and no local model. `SMOKE_BRAIN=configured` uses the model
from `.env` instead (a hosted model costs money; the computer-use checks are
skipped if that model cannot call tools). Secrets for a hosted model can be
supplied with `SMOKE_SECRETS_FILE=<env file>`. The model proxy's own unit
tests: `python3 -I modelproxy/test_modelproxy.py`.
`scripts/gateway-chain-test.sh` runs the hosted arrangement the same way:
agent, the bot's model proxy, a second model proxy in gateway mode, the
scripted model (`docs/MODELPROXY.md`).

## Layout

```
compose.yaml           the bot: bot, modelproxy, gate, proxy, volume-init (+ tunnel profile)
compose.datadir.yaml   optional: data in a host directory (DATA_DIR)
compose.cathedral.yaml optional: every service on the host network (hosts without container NAT)
bot/                   OpenClaw image + desktop, browser, config renderer
modelproxy/            holds the model key, meters tokens, enforces the budget; also the
                       central gateway for many bots (one program, Python stdlib)
gate/                  password login with rate limiting (Python stdlib)
proxy/                 Caddy: the one port, TLS, routes to chat and desktop
scripts/               smoke test, gateway chain test and their test-only scripted model
docs/                  CATHEDRAL.md (sandbox rooms), MODELPROXY.md (the model proxy's two roles)
.github/workflows/     images.yml: CI publishes the four images to ghcr.io (tags v*, manual)
UPSTREAM.md            pinned upstream, licences, what was changed
SECURITY-NOTES.md      what is disabled, what the agent can reach, gaps
NEXT.md                what is still missing; open questions
```

## Licence

MIT, see `LICENSE`. OpenClaw and the other components keep their own licences;
see `UPSTREAM.md`.
