# Security notes (stage 0)

Stage 0 is a test version that runs on an ordinary host. **Whoever controls
the host can read everything** described below. The sealed stage (a TDX or
SEV-SNP confidential VM) is meant to change that; this package is shaped for
it, but provides none of that protection yet.

## 1. What we removed or disabled from OpenClaw, and how

OpenClaw has had many security advisories in 2026 and a marketplace (ClawHub)
that was used to distribute malicious skills. We keep the built-in tools
(shell, files, browser, memory, scheduler) and switch off everything that
fetches or runs third-party code or calls home.

| What | How | Where |
|---|---|---|
| ClawHub marketplace skill | file deleted from the image (`/app/skills/clawhub`) | `bot/Dockerfile` |
| ClawHub registry access | `OPENCLAW_CLAWHUB_URL=http://127.0.0.1:9/disabled` (unreachable) | `bot/Dockerfile` |
| Installing skills or plugins (ClawHub, uploads, git or local paths, skill dependency installers, plugin install/update) | `security.installPolicy.enabled: true` for `skill` and `plugin` with **no** policy command, which upstream documents as "installs are blocked" | `bot/render-config.mjs` |
| Uploaded skill archives | `skills.install.allowUploadedArchives: false` | same |
| Bundled skills (prompt packs that drive third-party CLIs) | `skills.allowBundled: []` (an empty list disables all bundled skills) | same |
| Self-written skills from conversations | `skills.workshop.autonomous.mode: "off"` | same |
| Update checks, auto-update, update-outcome reports | `update.checkOnStart: false`, `update.auto.enabled: false`, `OPENCLAW_NO_AUTO_UPDATE=1` | `render-config.mjs`, `bot/Dockerfile` |
| Telemetry and install counting | `telemetry.enabled: false`, `DO_NOT_TRACK=1`, `OPENCLAW_TELEMETRY_ENDPOINT` unreachable | same |
| Bonjour/mDNS discovery | `OPENCLAW_DISABLE_BONJOUR=1` | `bot/Dockerfile` |
| Tailscale exposure | `gateway.tailscale.mode: "off"` | `render-config.mjs` |
| Control UI extras that fetch the internet | `controlUi.communityInvite: false`, `controlUi.automaticallyFetchFavicons: false` | same |
| Gateway's own token/password auth | replaced by `trusted-proxy` mode: only our proxy, at one fixed address, may assert the user, and only as `owner` | same |
| Telegram groups | `groupPolicy: "disabled"`; DMs need pairing approval or an allowlist | same |

The config renderer runs at every start and forces these keys back, so a
change made in the web UI or by the agent does not survive a restart. It is
not a hard boundary while the bot runs: the agent runs as the same user that
owns the config file (see section 2).

Not removed (still in the image, inactive unless configured): about 88 bundled
plugins (channels, providers, tools) that ship with the upstream image. They
are first-party code, not marketplace downloads. Reducing them to an
allowlist (`plugins.allow`) is a follow-up; it needs a test pass because some
core features are implemented as bundled plugins.

## 2. What the agent can reach

The agent (OpenClaw gateway), its shell, its files and its browser all run in
one container, `bot`, as the unprivileged user `node` (uid 1000).

- **Files**: the container's file system, writable only where uid 1000 may
  write: its home `/home/node` (the persistent volume: workspace, sessions,
  memory, browser profile, OpenClaw config) and `/tmp`. Nothing from the host
  is mounted. No Docker socket.
- **Secrets inside the container**: the model API key and the Telegram token
  are in the gateway's environment. The agent's shell runs as the same user,
  so **a model that decides to (or is prompt-injected to) read them can**.
  They are never written to disk by us (the config holds `{source:"env"}`
  references).
- **Processes and privileges**: all Linux capabilities dropped,
  `no-new-privileges`, no root, 1024 pids, memory limit 2.5 GiB, 2 CPUs.
  Chromium runs with `--no-sandbox` (its own sandbox needs user namespaces we
  do not grant); the container is the boundary.
- **Network, outbound**: unrestricted. The browser and shell can reach the
  internet. They can also reach anything the host network can reach,
  including services on the host (via the Docker bridge gateway, and via
  `host.docker.internal`, which compose adds so the dev test can use a local
  model) and the host's LAN. OpenClaw's browser tool blocks private-network
  addresses by default (`ssrfPolicy`), but the shell does not.
- **The screen (VNC)**: x11vnc listens on 127.0.0.1:5900 inside the bot
  container only and requires a VNC password (VncAuth), generated at first
  start into `~/.vnc/password.txt` (mode 600, on the volume, never logged).
  OpenClaw's Desktop panel reads it from that file; the `/desktop/` page
  receives it in the URL fragment, which browsers do not send to servers, so
  it does not appear in proxy logs. Anyone past the access password can view
  and control the screen, by design. VNC passwords are limited to 8
  characters and VncAuth is weak; the real protection is that the port is
  loopback-only and the pages sit behind the access password.
- **Network, inbound**: none to the bot directly. The bot's ports (gateway
  18789, noVNC 6080) are only on the internal `front` network shared with the
  proxy. The only published port is the proxy's.
- **Other containers**: the gate has no network except to the proxy (internal
  network, no egress). The optional tunnel only talks to the proxy and
  Cloudflare.

**All-host-network layout (`compose.cathedral.yaml`)**: on hosts without
container NAT, every service shares the host's network namespace and listens
on `127.0.0.1` only. The gateway then has to accept the proxy's identity
header from `127.0.0.1` (`allowLoopback`), which means any process in that
namespace, including the agent's own shell, could present it. In practice the
agent already runs inside the gateway with the same rights; the change matters
if anything else runs on that host. The rate limiter also trusts the
`Cf-Connecting-Ip` header from loopback (the tunnel), so a local process could
spread its attempts over fake addresses; the global limit still applies.

Access control: every request through the proxy needs a valid session cookie
from the password gate, including the desktop and its WebSocket, and the
OpenAI-compatible API. Failed logins: 5 per client address per 15 minutes, 50
in total per 15 minutes. The browser `Origin` is checked at the proxy (must
equal the address the browser is talking to) and again by the gateway.

## 3. What is NOT protected yet

- **The host sees everything**: the volume (conversations, memory, files,
  browser cookies and logins), the environment (model key, Telegram token,
  access password), process memory, the screen. Disks are not encrypted by us.
- **The model endpoint sees every prompt**: the whole conversation, tool
  results, and page contents the agent reads go to `MODEL_BASE_URL`. Chutes'
  `-TEE` models run in TDX with confidential GPUs, but this package does not
  verify their attestation, so that is the provider's claim, not a check we
  make.
- **Telegram sees every Telegram message** (bot chats are not end-to-end
  encrypted). Telegram's bot developer terms also apply.
- **Cloudflare sees all traffic** when the optional quick tunnel is on (TLS
  ends at Cloudflare).
- **Plain HTTP** on 127.0.0.1 in local mode; on a public address use
  `PUBLIC_HOSTNAME` (automatic HTTPS) or the tunnel.
- **No approvals by default** (`EXEC_MODE=full`): the agent runs shell commands
  without asking. `EXEC_MODE=ask` turns on OpenClaw's approval flow.
- **Prompt injection**: any web page or message the agent reads can try to
  steer it. With full shell access and unrestricted egress, a successful
  injection can exfiltrate whatever the bot can read (see section 2).
- **One shared password**, no accounts, no 2FA; the session cookie lasts 7
  days (`SESSION_HOURS`).
- **Supply chain**: images are pinned by digest, but the Debian packages added
  in `bot/Dockerfile` come from the live Debian mirror at build time (not
  pinned to a snapshot), and we use upstream's release image rather than our
  own source build (see `UPSTREAM.md`).
- **Background model use**: OpenClaw's memory plugin schedules a "dreaming"
  (memory consolidation) job; with a paid model this spends tokens without a
  user message.

## 4. What the sealed stage (TDX VM with dstack) needs from this package

The package already is one compose app, one data directory, one published
port, secrets only from the environment, digest-pinned images. Still needed:

1. **Volume encryption**: the data volume (or `DATA_DIR`) on an encrypted disk
   whose key is released only to the attested VM (dstack KMS or the local key
   provider), plus an app-level encrypted backup, because a dstack disk cannot
   yet be moved to another host.
2. **Secrets entered inside the box**: the model key, Telegram token and access
   password must arrive encrypted to the attested VM (dstack's encrypted
   environment, or a first-run page served over attested TLS), never via a
   host-readable `.env`. Ideally the model key moves out of the agent's
   container into a small egress proxy that adds it to requests, so the agent
   never sees it.
3. **TLS terminating inside the box**: Caddy (already in the package) with a
   certificate whose key never leaves the VM, plus attestation evidence that
   binds the certificate to the measured compose (dstack-ingress style), so a
   user can check they are talking to the sealed bot. Not the quick tunnel,
   which ends TLS at Cloudflare.
4. **Egress allowlist**: no outbound calls except the model endpoint, the chat
   platform (api.telegram.org), Let's Encrypt, and what the user's tasks need;
   enforced by a network policy or an egress proxy, not by the agent.
5. **Reproducible images**: rebuild OpenClaw from its pinned commit, pin Debian
   packages to a snapshot date, publish the image digests, so the measured
   compose hash means something to a third party.
6. **Remove dev conveniences**: drop `host.docker.internal`, keep
   `ENABLE_OPENAI_API` off unless needed, and make the config read-only to the
   agent (separate users for gateway and shell).
