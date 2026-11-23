# What a functional competitor still needs after stage 0

Must-have list (from the product brief) against what this package does today.
Sizes: S = under a day, M = a few days, L = a week or more.

| Must-have | Stage 0 status | Plan | Size |
|---|---|---|---|
| Named persistent bots with memory | One bot per compose app; OpenClaw already supports several named agents per gateway, each with its own memory and sessions, and memory survives restarts. The name, emoji and accent colour come from `.env` and show in the web chat; with `SEED_WORKSPACE=1` a new bot starts with house rules and a first-conversation script (`bot/seed/`) | Run one compose app (one sealed VM) per bot; a front door maps users to bots. Run the first conversation with a real model and tune the script; an avatar image | M |
| Persistent Linux computer, viewable and takeable | Done for one bot: shell, files, Chromium on a desktop, noVNC behind the password | Clipboard and file upload/download in the desktop page; resize; audio not needed | S |
| Web chat + a phone channel | Web chat done; Telegram wired (token in `.env`, pairing approval in the web UI) but not yet tested against the live Bot API | Test with a real token; add a short "connect Telegram" page | S |
| Routines: schedules and event triggers | Built into OpenClaw (Automations: cron, webhooks), left enabled; needs a tool-capable model | Document examples; surface run results in the chat; test with the hosted model | S |
| Tool access: MCP and API connectors | OpenClaw supports MCP servers by config; none configured; installs are blocked | Curated, reviewed connector list (Google, Slack, Notion via MCP) added by config, not marketplace | M |
| Approvals for consequential actions | OpenClaw has exec approvals (`EXEC_MODE=ask`) and approval UI; off by default here. The seeded house rules tell the agent to ask before messages to others, posts, purchases and deletions: an instruction to the model, not an enforced gate | Default to `ask` for public demos; enforced approvals for outbound messages and purchases; expiry | M |
| Secrets path the model never sees | Done for the model key only: it lives in the `modelproxy` service, which adds it to each request; the agent has a placeholder (checked by the smoke test). Not done: the Telegram token is still in the agent container's environment; per-site logins | Give the Telegram token the same treatment; per-site logins via a vault and a "secure form" that fills the browser without the model seeing values | L |
| Model endpoint setting + usage metering | Endpoint setting done (`MODEL_*`). The model proxy counts tokens per UTC day and in total and enforces hard caps in tokens or dollars (`BUDGET_*`); when a cap is reached the user is told why and the next message works as soon as it is raised (`docs/MODELPROXY.md`). For many bots on one upstream account the same program runs as a central gateway: per-bot tokens, daily allowances, model lists, a cap across all bots. No usage page yet (counters: `GET /usage` inside the model proxy, `GET /admin/usage` on a gateway) | A usage page behind the password gate; billing | S |

Also missing for a hosted product: accounts and a front door (many bots, one
login), upgrades without data loss, abuse handling, and terms. Backups have
their two halves in the bot (`bot/backup.sh`, `bot/restore.sh`: encrypted to
a per-bot public key, uploaded to a presigned URL); the part that decides
when, creates the URLs, holds the private keys and prunes old objects belongs
to whoever operates the bots and is not in this repository.

Identity, first run and backups, known limits:

- The first-conversation script (`bot/seed/BOOTSTRAP.md`) has only run
  against the scripted test model, which ignores it. That OpenClaw v2026.11.6
  keeps the file and puts it in front of the model as "Bootstrap Pending" is
  checked; how a real model carries the conversation, sets up the schedule
  and whether it deletes the file afterwards is not. The script names
  `automations` as the scheduling tool, which is this OpenClaw version's
  name for it.
- `BOT_DISPLAY_NAME` renames the agent, not the product: the page title reads
  "<name> — OpenClaw" and OpenClaw's own wording and logo stay. A full
  rename needs a rebuilt Control UI.
- The model values in `.env.example` come from direct calls to Chutes. None
  of them has run through the whole bot, and `MODEL_INPUT=text,image` has not
  been tried with a real image model.
- Backups were measured on a small new bot (14 MB). Time and scratch space
  for a bot with months of history and a large browser profile are unknown;
  a hot backup needs about twice the data's size in `BACKUP_TMPDIR`.
- A hot backup copies the browser's own databases while the browser may be
  writing them (OpenClaw's export does the same). Cookies restored from a hot
  backup can therefore be stale or, rarely, unreadable; a cold backup is the
  clean one.
- Nothing rotates or deletes old backups here, and nothing checks a backup by
  restoring it on a schedule.
- The two desktop fixes for sandbox rooms (`x11vnc -threads`, the websocket
  ping) were measured on an ordinary Docker host only. `x11vnc -threads` ran
  with two viewers at once for 45 s without a crash and passes the smoke
  test in both layouts; it has not had hours of use.
- Start time and image size (room measurements in `docs/CATHEDRAL.md`): a
  start in a room takes 5 to 10 minutes and nothing here shortens it. The
  bot image is 1.06 GB compressed and 2.8 GB unpacked. Largest parts that
  this package does not use, as candidates for a smaller image:
  `@openai/codex` (a native binary, 404 MB unpacked), `@trycua/cua-driver`
  (48 MB), `/app/docs` (38 MB, though the agent is told it can read them).
  Deleting them in a later layer would not help, because the upstream layers
  are still pulled and unpacked; it needs our own build of the image from
  selected files, and a test pass to see that OpenClaw starts without them.
  The rest is Chromium (381 MB) and the libraries it needs.
- OpenClaw keeps a "private update capture" of its databases at every start
  in `~/.openclaw.update-captures` (3 to 4 MB each in a room, 12 MB after
  four starts). Whether OpenClaw ever prunes it was not checked; if it does
  not, one restart a day adds about 1.3 GB a year. It is left out of backups (as OpenClaw leaves it out
  of its own); deleting old captures at start needs a look at what upstream
  expects of them.
- If the allowance warning in the house rules is to work with a central
  gateway, the bot needs a way to read its remaining allowance: the in-room
  model proxy's `/usage` shows a limit only when the cap is set locally.

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
