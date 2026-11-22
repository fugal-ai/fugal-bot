# private-bot: a bot with its own computer (stage 0)

One command starts ONE bot:

- an AI agent you chat with in a web page (OpenClaw's web chat / control UI);
- its own persistent Linux computer: a shell, a home directory with files, and
  a real Chromium browser on a desktop that you can **watch and take over** in
  a browser tab (noVNC, no client install);
- memory that survives restarts (conversations, the agent's memory files, its
  files and browser profile all live on one Docker volume);
- any OpenAI-compatible model endpoint, set in `.env`;
- optionally Telegram (paste a bot token, restart).

It is built on [OpenClaw](https://github.com/openclaw/openclaw) v2026.11.6
(MIT), pinned by image digest and commit; see `UPSTREAM.md`. What we removed or
switched off, and what is not protected yet, is in `SECURITY-NOTES.md`.

## Start

Needs only Docker (with the compose plugin). About 2.8 GB of RAM.

```bash
cp .env.example .env
# edit .env: set ACCESS_PASSWORD, and the model (MODEL_BASE_URL, MODEL_NAME, MODEL_API_KEY)
docker compose up -d --build
```

The first build downloads the upstream image and Chromium (a few minutes).

Open **http://127.0.0.1:8080/** and enter the access password.

What you will see:

- `/` — the bot's web chat (OpenClaw Control UI). Type in the chat box. The
  same UI has Settings, Channels, Automations, Agents, Sessions.
- `/desktop/` — the bot's screen: a terminal and, when the agent browses, its
  Chromium window. Click and type into it to take over; the agent and you share
  the same desktop.
- `/_gate/logout` — log out.

Check status: `docker compose ps` (all `healthy`), logs: `docker compose logs -f bot`.

## Put it on a public server (Ubuntu 22.04/24.04 with Docker)

Point a DNS A record at the server, then in `.env`:

```
BIND_ADDRESS=0.0.0.0
PORT=443
PUBLIC_HOSTNAME=bot.example.com
ACCESS_PASSWORD=<at least 12 characters; long and random>
```

`docker compose up -d --build`. Caddy obtains a Let's Encrypt certificate by
itself (TLS-ALPN challenge on 443 only; port 80 is not needed) and serves
everything on that one port. The proxy refuses to start on a non-loopback
address without a password of at least 12 characters, and refuses plain HTTP
on a public address unless `ALLOW_PUBLIC_HTTP=yes`. Failed logins are
rate-limited (5 per address per 15 minutes, 50 overall). Open
`https://bot.example.com/`.

## Point it at a model

Any OpenAI-compatible endpoint; three variables in `.env`, then
`docker compose up -d` (no rebuild needed):

| Variable | Meaning |
|---|---|
| `MODEL_BASE_URL` | e.g. `https://llm.chutes.ai/v1` |
| `MODEL_API_KEY` | bearer key (empty for local Ollama) |
| `MODEL_NAME` | the provider's model id |
| `MODEL_SUPPORTS_TOOLS` | `true` for real use; `false` only for models that reject tools |

`.env.example` has ready blocks for local Ollama and for **Chutes** (Bittensor
SN64; `zai-org/GLM-5.2-TEE` is a tool-capable model that runs in Intel TDX
with confidential GPUs). The key is read from the environment at runtime; it
is never written into the config file or the image.

The agent needs a model that can make tool calls to use its computer. The
local test models on the dev machine (Ollama `gemma3:4b`, `deepseek-r1:8b`)
cannot: Ollama rejects tool requests for both, so with them the bot only chats.

## Telegram

1. Create a bot with @BotFather, copy the token.
2. In `.env`: `TELEGRAM_BOT_TOKEN=<token>`. Optionally
   `TELEGRAM_ALLOWED_USER_IDS=<your numeric Telegram id>`.
3. `docker compose up -d`.
4. DM the bot. Without an allowlist, it replies with a pairing code; approve
   it in the web UI under **Settings → Channels → DM access requests**.

Long polling, outbound only; no webhook, no extra port. Groups are off.

## Schedules and named agents (built into OpenClaw)

- **Automations**: ask the bot in chat ("every weekday at 8:00 check the
  weather in Berlin and tell me"), or use the Automations page in the web UI.
  Results land in the chat (or Telegram). Needs a tool-capable model.
- **Named agents**: add agents (own name, persona, memory, workspace) in the
  web UI's Agents page; each keeps its own sessions and memory on the volume.
- **Approvals**: `EXEC_MODE=ask` makes the agent ask before shell commands
  that are not on its allowlist; you approve in the web UI (default `full`
  runs commands without asking, for the demo).

## Stop, data, wipe

- Stop: `docker compose down` (data kept). Start again: `docker compose up -d`.
- All data is in one Docker volume, `privatebot_botdata` (`<BOT_NAME>_botdata`):
  `home/` is the bot's home directory (`~/workspace`, `~/.openclaw` with config,
  sessions, memory and browser profile); `proxy/` holds the TLS certificate.
- Wipe everything: `docker compose down -v`.
- Back up: `docker run --rm -v privatebot_botdata:/v -v "$PWD":/out alpine tar czf /out/bot-backup.tgz -C /v .`
  (the archive contains secrets-free config but the full conversation history).

## Smoke test

`scripts/smoke.sh` builds and starts a separate copy (project `privatebot-smoke`,
port 18080, its own volume), checks the login gate, the web UI, a model reply,
a shell command + file + browser page made by the agent, the desktop endpoint,
then restarts and checks the file and the conversation are still there. It
makes no paid call: the tool steps use a test-only scripted brain
(`scripts/scripted-brain/`), because the local models cannot call tools.

## Layout

```
compose.yaml          the bot = this compose app (bot, gate, proxy, volume-init)
bot/                  upstream OpenClaw image + desktop, browser, our config renderer
gate/                 password login + rate limit (stdlib Python)
proxy/                Caddy: the one port, TLS, forwards to chat and desktop
scripts/smoke.sh      end-to-end test
UPSTREAM.md           pinned upstream commit/digest, licence, how to update
SECURITY-NOTES.md     what is removed/disabled, what the agent can reach, gaps
NEXT.md               what a competitor still needs
```
