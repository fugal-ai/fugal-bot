# Fugal Bot

An AI agent with its own Linux computer, packaged as one Docker Compose app.
You chat with it in a web page (or Telegram); it works in its own shell, files
and Chromium browser, and you can watch and take over its desktop in a tab.
It uses any OpenAI-compatible model endpoint, and its memory survives restarts.

This is **stage 0, a test version**. It runs on an ordinary host, and whoever
controls that host can read everything in it. Read "What is private today"
below before you put anything sensitive in it.

The agent is [OpenClaw](https://github.com/openclaw/openclaw) (MIT), pinned to
release v2026.11.7 by image digest, with its marketplace, installs, update
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
MODEL_NAME=deepseek-ai/DeepSeek-V4-Flash-0731-TEE
MODEL_SUPPORTS_TOOLS=true
MODEL_INPUT=text
MODEL_CONTEXT_WINDOW=64000
MODEL_MAX_TOKENS=4096
```

and `docker compose up -d`. The current list, with prices and a
`confidential_compute` flag, is at `https://llm.chutes.ai/v1/models`; any
entry whose `supported_features` include `tools` should work. It is paid per
token, and every agent turn sends the whole conversation plus OpenClaw's
system prompt, so a turn can cost tens of thousands of input tokens.

Three models were measured on 2026-11-22 with direct calls to Chutes (small
samples, one evening; not yet through the whole bot):

| Use | `MODEL_NAME` | Notes |
|---|---|---|
| Default | `deepseek-ai/DeepSeek-V4-Flash-0731-TEE` | Text only. By far the fastest (a tool call back in about 1.4 s); 21 of 22 tool calls right; does not think unless asked. |
| If the default fails | `zai-org/GLM-5.1-TEE` | Text only. No tool failure seen; about twice the price per turn. Nothing switches to it automatically: change `MODEL_NAME`. |
| When the bot must read images | `moonshotai/Kimi-K2.6-TEE` | `MODEL_INPUT=text,image`. Turn its thinking off with `MODEL_EXTRA_BODY={"chat_template_kwargs":{"enable_thinking":false}}`, or a tool call takes 5 to 40 s instead of about 2 s. |

The two text-only models answer HTTP 400 to a request with an image in it, so
keep `MODEL_INPUT=text` for them.

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
a price table per model, optionally with a cached-input price as a third number), and `BUDGET_REQUESTS_PER_MINUTE`. Once a cap is
reached the model proxy refuses further model calls, and instead of an answer
the web chat shows "Your request couldn't be completed: LLM request rejected:
The free allowance for today is used up and it comes back at midnight UTC To
keep going now add your own model key in Settings on the Fugal Bot page" (for
a total cap: "The free allowance for this bot is used up To keep going add your
own model key in Settings on the Fugal Bot page"; the texts have no
punctuation because the web chat escapes it). That lasts until the day ends (daily caps)
or you raise the cap and run `docker compose up -d`; the next message is then
answered at once. A cap is checked before each call, so the last call can
overshoot it by its own size. If an endpoint reports no token usage, the
model proxy counts a rough estimate and marks it (`estimated_requests`).

The refusal is an HTTP 400 on purpose: the usual `insufficient_quota` forms
(429, 402) make OpenClaw switch the model provider off for ten minutes, also
after the cap is raised. The measurements, the other forms
(`MODELPROXY_EXHAUSTED_REPLY`) and the proxy's second role, a central gateway
for many bots with per-bot tokens and allowances, are in `docs/MODELPROXY.md`.

## Model settings: speed and cost

Set in `.env`, applied by `bot/render-config.mjs` at every start:

- `MODEL_INPUT` (`text`, or `text,image`): what the model accepts.
- `MODEL_EXTRA_BODY`: JSON fields added to every model request, for example
  the switch that turns a model's "thinking" off. OpenClaw sends them itself
  (`params.extra_body`), so nothing else has to rewrite requests.
- `BACKGROUND_MODEL_USE=off`: no model calls without a message from you. It
  switches off OpenClaw's nightly memory consolidation ("dreaming"), the one
  job this version schedules by itself. Schedules you set up still run.
- `TOOLS_DENY`: tools left out of what is sent to the model. By default the 16
  that cannot work in this package (paired devices, voice and meeting capture,
  publishing to GitHub, plugin installs, self-written skills, portals: an app
  preview needs a private wildcard HTTPS ingress that a hosted bot behind one
  hostname does not have; OpenClaw has no switch that hides the Portals page
  itself).
- `TZ` (an IANA name; the controller passes the owner's browser zone) is also
  written to `agents.defaults.userTimezone`, the agent's local time. The
  Automations form's grey "America/Los_Angeles" is only OpenClaw's placeholder;
  a blank zone there means the container's `TZ`.

Always on: OpenClaw is told the endpoint reports token usage on streamed
replies (so it knows how full the context is), the browser tool returns
its compact page snapshot unless the agent asks for more (it lists what can be
clicked, not the page's words; AGENTS.md tells the agent to read a page with
the browser's `text` action and `maxChars: 4000`), and `web_fetch` returns at
most 6,000 characters of a page, 12,000 if the agent asks (OpenClaw's own
default and cap: 20,000). Measured with the
test model on OpenClaw v2026.11.6: the first request of a new bot shrank from
69,700 to 47,400 bytes with the tool list trimmed and the shorter starter
files below (about 5,600 fewer input tokens on every call at four characters
a token).

Not set, because it could not be tested without paid calls: prompt-cache keys
(`compat.supportsPromptCacheKey`) and a separate image model
(`agents.defaults.imageModel`).

"OpenClaw couldn't finish this reply" after a long turn: seen with
DeepSeek-V4-Flash on Chutes (2026-11-23). Now and then a streamed answer turns
into garbage part way (mixed languages, repeated letters); when that happens
inside a tool call, its arguments are not valid JSON, and OpenClaw
(v2026.11.6) records "Provider returned an incomplete or malformed tool call".
It continues the turn once after the first such answer and ends it at the
second; the number is fixed in OpenClaw, not a setting. The HTTP status stays
200. Recorded cases also carry the model's own template markup
(`</｜DSML｜>`) inside otherwise valid JSON arguments. Both model proxies
now check every answer to a request that offers tools and, if its tool call
is broken, send the same request again before anything reaches the agent (at
most twice; counted in `/usage` and `/admin/usage` as `malformed_*`, the
thrown-away attempts as `discarded_*`, never billed to the bot): see
"Broken tool calls" in docs/MODELPROXY.md. Short turns remain the other
defence: AGENTS.md sends chain questions to `taoq` (also when the user names
a site such as taostats.io, which shows automated browsers a bot check), and
tells the agent to stop after a few fruitless steps and say what went wrong.

## The web UI in a hosted bot

Changes to OpenClaw's Control UI for a bot that has no marketplace, no
channels of its own and no speech-to-text provider (OpenClaw v2026.11.7):

- Config: an Activity recap per finished run, on the chat model
  (`agents.defaults.utilityModel`; OpenClaw finds no small model for a custom
  provider, so every recap said "Recap unavailable"). About 0.0005 USD a call
  on DeepSeek V4 Flash. `ACTIVITY_RECAP=off` turns it off. The live session
  observer stays off.
- Checked patches of the built files (`bot/patch-openclaw.mjs`, applied in the
  image build, which stops if any patched text has changed upstream): the
  Plugins page shows installed plugins without the marketplace's "fetch
  failed" error; Systems > Backups says Fugal backs the bot up instead of
  showing a command; a new automation runs in the main session, so its result
  appears in the chat (it used to run apart with no delivery, and announcing
  needs a channel: "Channel is required"); the microphone button is hidden
  (dictation and Talk need a speech-to-text provider).
- The owner's name: the sign-in id stays "owner" (the trusted-proxy
  allowlist); the profile's display name becomes the short wallet address
  (`OWNER_WALLET_SS58`) or "You", at the gateway start after the first sign-in
  (`bot/owner-name.mjs`). A name set under Settings > Profile is kept.
- Profile pictures: the proxy answers `/api/users/<id>/avatar` with 404 (the
  UI shows initials). The gateway answered 503, or looked the sign-in id up on
  gravatar.com.
- The main conversation is the Home button (house icon, bottom of the
  sidebar; Mod+Shift+H). OpenClaw never lists it among the sessions.
- "Healthy" now also waits for OpenClaw's reply runtime to be loaded
  (`/readyz` `coreReady`), so the first message of a new bot does not wait for
  it. After 10 minutes `/startupz` alone counts, as before.
- A first visit loads about 100 files (OpenClaw's UI is split into many small
  files); they are compressed and cached by the browser as immutable, so later
  visits fetch few of them.

## Name, look and first conversation

The suggestion buttons in an empty chat ("What can you do?", "Summarize my
recent sessions", ...) are fixed strings in OpenClaw v2026.11.6's Control UI
bundle (`dist/control-ui/assets`, one set per language); no setting changes
them. `BOOTSTRAP.md` handles "What can you do?" as a first message: a short
answer plus a one-line offer of the daily briefing.

All optional; leave them unset and the bot behaves as before.

- `BOT_DISPLAY_NAME`, `BOT_EMOJI`: the bot's name and emoji in the web chat
  (page title, heading, message box). They are defaults: a name you change
  later on the Agents page is kept.
- `UI_ACCENT_COLOR` (`#rrggbb`): the accent colour of the web chat. A colour
  you pick under Settings, Appearance wins over it.
- `SEED_WORKSPACE=1`: at start, starter files are written into the agent's
  workspace where they are missing, and kept current: a file is replaced by
  a newer image's version only while its content is still exactly what an
  image wrote (the SHA-256 recorded in `~/.openclaw/fugal-seed.json` when it
  was written, or any earlier template in `bot/seed-history.json` rendered
  with this bot's name, emoji and address). A file the user or the agent
  changed in any way is never touched again. When a template changes, add the
  version being replaced to `bot/seed-history.json` (`node bot/test_seed.mjs`
  tests the rules).
  - `IDENTITY.md`, `SOUL.md`, `USER.md`: the name and emoji, two lines on
    tone, and what is known about the user (the public address below).
  - `AGENTS.md`: house rules. Ask before sending a message to anyone else,
    posting, buying, deleting or anything that cannot be undone; never ask
    for or store wallet secrets; spend few model calls (page text before
    screenshots, batch work, at most 8 tool calls per message, stop after a
    tool fails twice); say when the daily allowance is nearly used; for any
    Bittensor chain question run `taoq`, never install packages or write
    RPC code.
  - `BOOTSTRAP.md`, in a brand-new data directory only: the first
    conversation. The bot says who it is, that it will never ask for a seed
    phrase or private key and only reads public chain data, and offers a
    daily briefing for the owner's wallet (stake, what changed on its subnets,
    the day's Bittensor news, from `taoq briefing`), then a subnet watch, a hotkey
    watch or research.
    For each job it runs `taoq` once and shows the result, sets the job up
    as a schedule with OpenClaw's own scheduler (a report: an isolated run that
    costs 2 model calls; a watch: a script job with no model call that sends a
    message only when something is new, worse or resolved), writes what it learned to its
    memory files and deletes `BOOTSTRAP.md`, which ends the first run: the
    file is not written again at later starts or after a restore.
- The Bittensor reference (in the image, `bot/skills/bittensor/`: a primer on
  how the network works, a glossary, a map of sources) is an OpenClaw skill
  loaded from `/opt/fugal-bot/skills` (`skills.load.extraDirs`). Only its name
  and one-line description are in every request; the agent reads one section
  of a file when a question needs it (`AGENTS.md` says when). It changes with
  the image, not with the workspace.
- `taoq` (in the image, on `PATH`): read-only Bittensor reports for those
  jobs (stake, subnet, hotkey, the week, a subnet brief), JSON or `--text`.
  No wallet, no keys; public finney endpoints. Units, checks and the
  real-model test to run: `docs/TAOQ.md`.
- `OWNER_WALLET_SS58`: the owner's public Bittensor address, written into
  those files so the bot can report on it without asking. Public data.

The templates are in `bot/seed/`. They are short on purpose: OpenClaw sends
every one of these files to the model with every request, so each line costs
input tokens for the life of the bot. The five together are 7.5 kB, against
18.8 kB for the files OpenClaw writes by itself, and 4.1 kB against 11.3 kB
once `BOOTSTRAP.md` is gone. These rules are instructions to a model,
not controls: OpenClaw only enforces approval for shell commands
(`EXEC_MODE=ask`), not for messages, posts or purchases made in the browser.

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
(`BOT_MEM_LIMIT`, `BOT_CPUS`). The bot's container needs most of that when
its browser is busy: in a sandbox room it used 1.65 GB idle and peaked at
3.33 GB under a 4 GB limit, so give it 4 GB where you can.

**Hosts where containers get no NAT** (for example Cathedral confidential
sandboxes, where Docker runs with `--bridge=none --iptables=false`): add
`compose.cathedral.yaml`, which puts every service on the host network and
wires them over `127.0.0.1`. Step-by-step: `docs/CATHEDRAL.md`.

**Run for somebody else, inside a site's page** (hosted mode): a third file,
`compose.hosted.yaml`, gives the bot a fixed hostname through a Cloudflare
named tunnel and replaces the password by a one-time sign-in token that the
site posts into a frame (`GATE_SSO=on`). A bot you run for yourself does not
need it. `docs/CATHEDRAL.md`, "Hosted mode"; limits in `SECURITY-NOTES.md`.

**First start takes a while**: building the bot image downloads the OpenClaw
image (about 1 GB) and the desktop and Chromium packages, typically several
minutes on a fresh machine; later starts reuse the image. To skip building,
set `BOT_IMAGE`, `MODELPROXY_IMAGE`, `GATE_IMAGE` and `PROXY_IMAGE` to
published, digest-pinned images and run `docker compose pull` then
`docker compose up -d --no-build`. The `images` workflow publishes them to
`ghcr.io/fugal-ai` (`fugal-bot`, `fugal-bot-modelproxy`, `fugal-bot-gate`,
`fugal-bot-proxy`); the four lines with their digests are in the summary of
each workflow run and in `images.env` inside `fugal-bot-room-<version>.tgz`,
which is attached to each release and to each run.

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
- Back up: see the next section.

## Back up and restore

`bot/backup.sh` makes one archive of the bot's home directory, encrypts it
with [age](https://age-encryption.org) to the public key in
`BACKUP_AGE_RECIPIENT`, and uploads it with one HTTP PUT to a presigned URL
(S3, R2 or anything else that accepts one). Nothing runs on a timer: you, or
the service that operates the bot, start each backup. The bot only ever holds
the public key, so it can make backups and cannot read them.

```bash
age-keygen -o bot-backup.key          # keep this file somewhere else; it prints the public key
# .env: BACKUP_AGE_RECIPIENT=age1...   then: docker compose up -d

# while the bot runs ("hot"): the URL goes in on standard input
echo "$PRESIGNED_PUT_URL" | docker compose exec -T bot /opt/fugal-bot/backup.sh hot
# with the bot stopped ("cold")
docker compose stop bot
echo "$PRESIGNED_PUT_URL" | docker compose run --rm --no-deps -T --entrypoint /opt/fugal-bot/backup.sh bot cold
```

It prints one line of JSON, for example
`{"ok":true,"kind":"hot","format":1,"bytes":467817,"sha256":"64b7...","seconds":13.368,"openclaw":"2026.11.6"}`
(size and SHA-256 of the encrypted object), or `{"ok":false,"error":"..."}`
and a non-zero exit status. The URL is never printed or put on a command
line.

- **Hot** uses OpenClaw's own export (`openclaw backup create --verify`),
  which snapshots its databases instead of copying them mid-write, for the
  state directory and the workspace, and copies the rest of the home
  directory as plain files. Checked on v2026.11.6 with the gateway running.
  It needs about 0.3 GB of memory in the bot's container while it runs.
- **Cold** is a plain tar of the home directory. It refuses to run while the
  gateway answers.
- **In a backup**: conversations, memory, the workspace, schedules, OpenClaw's
  config, and the browser profile with its cookies and logins.
- **Left out**: browser caches, `~/.cache`, `~/.npm`, OpenClaw's logs and
  temporary files, the VNC password (made again at start). Never in one,
  because they are outside the bot's home: the model proxy's usage counters
  (`modelproxy/`; a restored bot counts from zero, which matters if you use
  `BUDGET_TOKENS_TOTAL` or `BUDGET_USD_TOTAL`), the proxy's TLS state,
  `TUNNEL_URL.txt`, and `.env`.

Measured on new bots after a few conversations and one page visit (home
directories of about 14 MB): 0.2 to 0.7 MB encrypted, 6 to 13 s hot (almost
all of it OpenClaw's export, which took 34 s in a sandbox room), 0.15 s cold,
0.2 to 0.4 s to restore. A bot with months of history was not measured.

To restore, start from an empty data directory (a new volume, or `--force` to
replace what is there), with the bot stopped:

```bash
docker compose up volume-init          # a new, empty volume
echo "$PRESIGNED_GET_URL" | docker compose run --rm --no-deps -T \
  -v "$PWD/bot-backup.key:/run/backup.key:ro" \
  --entrypoint /opt/fugal-bot/restore.sh bot /run/backup.key
docker compose up -d
```

The key file must be readable by uid 1000, the bot's user. `restore.sh`
downloads, decrypts and checks the archive before it touches the
data directory, then unpacks it and prints one line of JSON. At the next
start the bot has its conversations and files back, and the first-run script
does not run again. The key file is needed for that one command only.

A hosted bot (`compose.hosted.yaml`) has the same two scripts as one-shot
services, `docker compose run --rm -T backup` and `docker compose run --rm -T
restore`; `docs/CATHEDRAL.md`, "Controller contract", lists every command
with its input, output and exit status.

## Smoke test

`scripts/smoke.sh [--fresh] [--keep]` builds and runs a separate copy (project
`fugal-bot-smoke`, port 18080, its own volume) and checks, through the proxy:
the login gate, the web UI, a model reply, a shell command, file and browser
page made by the agent, then drives the real web chat in headless Chromium
(websocket origin checks, a message typed in the page and its reply, the
desktop page connecting), then restarts and checks that the file, the
conversation and the token counters are still there. In between it restarts
the gate and the proxy once in hosted mode (`GATE_SSO=on`, a throwaway
password) and repeats the chat and desktop checks the way a hosted bot is
used: signed in by a one-time token, in a frame on the page of another origin
of the same site, over HTTPS; it also checks who may frame the bot, what a
sibling host can do with the cookie, and sign-out (`SMOKE_SSO=0` skips this
part). It also checks the model
proxy: that its counters go up, that the model API key cannot be found in the
agent container (environment, `/proc`, files, data directory), that it is not
reachable from outside, and last that a very low budget makes it refuse, that
the user is told why, and that the next message is answered once the budget
is raised. With the scripted model it also gives the bot a name, an emoji, an
accent colour and the starter files, and checks them: the files as seeded and
untouched by OpenClaw after conversations and a restart, the name in the real
web page, the extra request field on every model call, no background job
scheduled. Last before the budget check, it takes an encrypted hot backup to
a throwaway local object store, checks that the object cannot be read without
the key, restores it into an empty volume, and takes and restores a cold
backup there.

By default the model is a test-only scripted one (`scripts/scripted-brain/`)
placed behind the model proxy like a real endpoint, so the run needs no paid
call, no real key and no local model. `SMOKE_BRAIN=configured` uses the model
from `.env` instead (a hosted model costs money; the computer-use checks are
skipped if that model cannot call tools). Secrets for a hosted model can be
supplied with `SMOKE_SECRETS_FILE=<env file>`. Unit tests (stdlib only):
`python3 -I modelproxy/test_modelproxy.py`, `python3 -I gate/test_gate.py`
and `python3 -I bot/tools/test_taoq.py`; `bot/tools/livecheck.py` checks
`taoq` against finney (network, `docs/TAOQ.md`). With the scripted model and
a new data directory both tests end with the first conversation
(`scripts/first-conversation.sh`): the greeting, a real `taoq` read in the
bot's shell (`SMOKE_TAOQ` / `HOSTED_TEST_TAOQ=fixture` for a recorded read
without network), the schedule in OpenClaw's job table and one forced run of
it, `BOOTSTRAP.md` deleted.
`scripts/gateway-chain-test.sh` runs the hosted arrangement the same way:
agent, the bot's model proxy, a second model proxy in gateway mode, the
scripted model (`docs/MODELPROXY.md`).

`scripts/hosted-test.sh` runs a hosted bot the way a site's controller does:
from a directory that holds only the three compose files of the room bundle
and a `.env`, with `GATE_SSO=on` and every service in one network namespace.
It signs in with a one-time token, chats through the bot's model proxy,
checks the name, emoji and accent colour inside a frame on a stand-in for the
site's page, the starter files, the desktop and who may frame the bot; then
it takes a hot backup, stops the bot, takes a cold backup through the
`backup` service, removes the stack and deletes the data directory, restores
through the `restore` service, starts again, and checks that a file and a
conversation from before the backup are back. The named tunnel is not run
there (it needs a real Cloudflare token); its definition is checked with
`docker compose config`.

## Layout

```
compose.yaml           the bot: bot, modelproxy, gate, proxy, volume-init (+ tunnel profile)
compose.datadir.yaml   optional: data in a host directory (DATA_DIR)
compose.cathedral.yaml optional: every service on the host network (hosts without container NAT)
compose.hosted.yaml    optional, on top of the above: named tunnel (profile "hosted"),
                       backup and restore one-shots
bot/                   OpenClaw image + desktop, browser, config renderer,
                       starter files (seed/), backup.sh and restore.sh, the chain
                       tool (tools/taoq.py)
modelproxy/            holds the model key, meters tokens, enforces the budget; also the
                       central gateway for many bots (one program, Python stdlib)
gate/                  password login with rate limiting, or sign-in by one-time token
                       from a site's page (Python stdlib)
proxy/                 Caddy: the one port, TLS, routes to chat and desktop
scripts/               smoke test, gateway chain test, hosted test and their test-only
                       scripted model
docs/                  CATHEDRAL.md (sandbox rooms), MODELPROXY.md (the model proxy's two roles),
                       TAOQ.md (the chain tool and the first conversation)
.github/workflows/     images.yml: CI publishes the four images to ghcr.io and packs the
                       room bundle (tags v*, manual)
UPSTREAM.md            pinned upstream, licences, what was changed
SECURITY-NOTES.md      what is disabled, what the agent can reach, gaps
NEXT.md                what is still missing; open questions
```

## Licence

MIT, see `LICENSE`. OpenClaw and the other components keep their own licences;
see `UPSTREAM.md`.
