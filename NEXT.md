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
| Secrets path the model never sees | Done for the model key only: it lives in the `modelproxy` service, which adds it to each request; the agent has a placeholder (checked by the smoke test). Not done: the Telegram token is still in the agent container's environment; per-site logins | Give the Telegram token the same treatment; per-site logins via a vault and a "secure form" that fills the browser without the model seeing values | L |
| Model endpoint setting + usage metering | Endpoint setting done (`MODEL_*`). The model proxy counts tokens per UTC day and in total and enforces hard caps in tokens or dollars (`BUDGET_*`); when a cap is reached the user is told why and the next message works as soon as it is raised (`docs/MODELPROXY.md`). For many bots on one upstream account the same program runs as a central gateway: per-bot tokens, daily allowances, model lists, a cap across all bots. No usage page yet (counters: `GET /usage` inside the model proxy, `GET /admin/usage` on a gateway) | A usage page behind the password gate; billing | S |

Also missing for a hosted product: accounts and a front door (many bots, one
login), backups, upgrades without data loss, abuse handling, and terms.

Model proxy, known limits (details in `docs/MODELPROXY.md` and
`SECURITY-NOTES.md`):

- The exhausted-allowance reply was chosen by measurement against OpenClaw
  v2026.11.6 only. An OpenClaw upgrade can change how it treats that reply;
  the last step of `scripts/smoke.sh` catches it. What Telegram shows for a
  refused message has not been looked at.
- In the all-host-network layout every local process can call the model
  proxy on `127.0.0.1:9001`.
- No run against a real provider yet (no paid calls were made): the TLS path
  to the upstream is covered by reading the code only.
- The gateway role has unit tests and a Docker chain test with the scripted
  model. It has not run behind a public reverse proxy or under load, and the
  arm64 image is built by CI but was not run.

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
   read them, and the agent cannot either? (The agent half is solved for the
   model key by the model proxy; with a central gateway the box holds only a
   per-bot token, which the host can still read.)
5. Egress policy: who decides the allowlist for "what the user's tasks need",
   and how is it changed without the host seeing the traffic?
6. Private inference: Chutes' TEE models publish attestation evidence but no
   finished client verifier; do we verify per request ourselves?
7. The quick tunnel ends TLS at Cloudflare; for hosts without an inbound
   address (e.g. sandbox rooms), what replaces it inside the seal?
