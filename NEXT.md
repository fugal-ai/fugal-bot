# What a functional competitor still needs after stage 0

Must-have list (from the product brief) against what this package does today.
Sizes: S = under a day, M = a few days, L = a week or more.

| Must-have | Stage 0 status | Plan | Size |
|---|---|---|---|
| Named persistent bots with memory | One bot per compose app; OpenClaw already supports several named agents per gateway, each with its own memory and sessions, and memory survives restarts | Run one compose app (one sealed VM) per bot; expose bot name and persona in `.env`; a front door maps users to bots | M |
| Persistent Linux computer, viewable and takeable | Done for one bot: shell, files, Chromium on a desktop, noVNC behind the password | Clipboard and file upload/download in the desktop page; resize; audio not needed | S |
| Web chat + a phone channel | Web chat done; Telegram wired (token in `.env`, pairing approval in the web UI) but not yet tested against the live Bot API | Test with a real token; add a short "connect Telegram" page | S |
| Routines: schedules and event triggers | Built into OpenClaw (Automations: cron, webhooks), left enabled; needs a tool-capable model | Document examples; surface run results in the chat; test with the hosted model | S |
| Tool access: MCP and API connectors | OpenClaw supports MCP servers by config; none configured; installs are blocked | Curated, reviewed connector list (Google, Slack, Notion via MCP) added by config, not marketplace | M |
| Approvals for consequential actions | OpenClaw has exec approvals (`EXEC_MODE=ask`) and approval UI; off by default here | Default to `ask` for public demos; approvals for outbound messages and purchases; expiry | M |
| Secrets path the model never sees | Not done: keys are in the agent container's environment | Move model key into an egress proxy container; per-site logins via a vault and a "secure form" that fills the browser without the model seeing values | L |
| Model endpoint setting + usage metering | Endpoint setting done (`MODEL_*`); OpenClaw records token usage per session, no limits or billing | Per-bot budget and hard cap at the egress proxy; usage page | M |

Also missing for a hosted product: accounts and a front door (many bots, one
login), backups, upgrades without data loss, abuse handling, and terms.

## Open questions for the sealed stage

1. Disk key and migration: dstack's local key provider cannot move a disk to
   another host; KMS mode ties release to the compose hash and host device.
   Who runs the KMS for independent hosts, and is "restore from an encrypted
   app-level backup" the migration story?
2. Does a desktop + Chromium + the gateway fit a 2 vCPU / 4 GB TD
   comfortably? Stage 0 measured about 1.0 to 1.5 GB RSS for the gateway at
   start plus the browser; needs measuring under load inside a TD.
3. How does a user verify the bot they talk to is the sealed one (attested
   TLS evidence in the browser, a verifier page)?
4. How do the model key and per-site logins enter the box so the host cannot
   read them, and the agent cannot either?
5. Egress policy: who decides the allowlist for "what the user's tasks need",
   and how is it changed without the host seeing the traffic?
6. Private inference: Chutes' TEE models publish attestation evidence but no
   finished client verifier; do we verify per request ourselves?
7. The quick tunnel ends TLS at Cloudflare; for hosts without an inbound
   address (e.g. sandbox rooms), what replaces it inside the seal?
