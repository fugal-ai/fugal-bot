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
| Bundled skills (prompt packs that drive third-party CLIs) | `skills.allowBundled: []` (an empty list disables all bundled skills). The one skill that is on is ours: a read-only Bittensor reference in the image (`/opt/fugal-bot/skills`, `skills.load.extraDirs`), plain text, no commands of its own beyond `grep`/`sed` reads of its files | same |
| Self-written skills from conversations | `skills.workshop.autonomous.mode: "off"` | same |
| Update checks, auto-update, update-outcome reports | `update.checkOnStart: false`, `update.auto.enabled: false`, `OPENCLAW_NO_AUTO_UPDATE=1` | `render-config.mjs`, `bot/Dockerfile` |
| Telemetry and install counting | `telemetry.enabled: false`, `DO_NOT_TRACK=1`, `OPENCLAW_TELEMETRY_ENDPOINT` unreachable | same |
| Bonjour/mDNS discovery | `OPENCLAW_DISABLE_BONJOUR=1` | `bot/Dockerfile` |
| Tailscale exposure | `gateway.tailscale.mode: "off"` | `render-config.mjs` |
| Control UI extras that fetch the internet | `controlUi.communityInvite: false`, `controlUi.automaticallyFetchFavicons: false` | same |
| Gateway's own token/password auth | replaced by `trusted-proxy` mode: only our proxy, at one fixed address, may assert the user, and only as `owner` | same |
| Telegram groups | `groupPolicy: "disabled"`; DMs need pairing approval or an allowlist | same |
| Telegram owner | only the numeric ids of `TELEGRAM_ALLOWED_USER_IDS`, as `commands.ownerAllowFrom: ["telegram:<id>"]` (matches that Telegram user only; rewritten at every start, removed when Telegram is off or in pairing mode); a paired sender is never the owner | same |
| Tools that cannot work here (paired devices, voice and meeting capture, publishing to GitHub, the plugin installer, self-written skills, portals) | `tools.deny` (16 names; `TOOLS_DENY` changes the list) | same |
| Background model calls, when `BACKGROUND_MODEL_USE=off` | `plugins.entries.memory-core.config.dreaming.enabled: false`; OpenClaw then removes its "Memory Dreaming Promotion" job | same |

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
- **Secrets inside the container**: the Telegram token (if set) is in the
  gateway's environment. The agent's shell runs as the same user, so **a
  model that decides to (or is prompt-injected to) read it can**. It is never
  written to disk by us (the config holds a `{source:"env"}` reference).
  The **model API key is not in this container**; see "The model key" below.
- **Processes and privileges**: all Linux capabilities dropped,
  `no-new-privileges`, no root, 1024 pids, memory limit 2.5 GiB, 2 CPUs.
  Chromium runs with `--no-sandbox` (its own sandbox needs user namespaces we
  do not grant); the container is the boundary.
- **Network, outbound**: unrestricted. The browser and shell can reach the
  internet. They can also reach anything the host network can reach,
  including services on the host (via the Docker bridge gateway) and the
  host's LAN. OpenClaw's browser tool blocks private-network addresses by
  default (`ssrfPolicy`), but the shell does not. (`host.docker.internal`,
  for a model server on the host, is now only added to the model proxy.)
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
  Cloudflare. The model proxy talks to the bot (internal network) and to the
  model endpoint.

### The model key

`MODEL_BASE_URL` and `MODEL_API_KEY` are given only to the `modelproxy`
container (stdlib Python, uid 1002, read-only file system, no capabilities).
The bot container is configured with `http://modelproxy:9001/v1` and the
placeholder key `held-by-modelproxy`. The model proxy drops whatever
`Authorization` header it receives and adds the real one.

What this gives:

- The key is not in the agent container's environment, in any process there,
  in its config or its data directory, so the agent's shell cannot read it
  and a prompt injection cannot exfiltrate it. `scripts/smoke.sh` searches
  all of those for the key on every run.
- The model proxy does not hand the key out: it only connects to the one
  endpoint fixed at its start, follows no redirects, forwards only inference
  calls (`chat/completions`, `completions`, `responses`, `embeddings`,
  `models`; nothing like key or account management), and logs one line per
  request with the path, status, token counts and duration: never a body, a
  header or the key.
- Spending is bounded if you set a budget (`BUDGET_*`): the counters and the
  caps live in the model proxy and in `modelproxy/` on the volume, which is
  owned by its own user and not mounted into the agent container.

- With a budget, a call that arrives after a cap is reached is answered by
  the model proxy itself and tells the user why (`docs/MODELPROXY.md`).

What it does not give:

- The agent can still **use** the key through the model proxy: any process in
  the agent container can make inference calls with it, up to the budget.
  Without a budget, a hijacked agent can spend without limit.
- The agent can read the usage counters (`GET /usage`); they hold numbers
  only.
- The host still sees the key (`.env`, the model proxy's environment).
- A key that was used with an earlier version of this package was in the
  agent's environment then. Treat it as exposed and replace it.
- Token counts come from the endpoint's own `usage` field. If an endpoint
  sends none, the model proxy counts a rough estimate (and marks it), so a
  cap in tokens or dollars is then approximate. A cap is checked before a
  call, so the last call overshoots it by its own size.

Who can reach the model proxy, by layout:

- **Default layout**: only the bot. The two share an internal Docker network
  (`modelnet`) that nothing else is attached to; the model proxy publishes no
  port; the public proxy, the gate and the tunnel have no route to it.
- **All-host-network layout (`compose.cathedral.yaml`)**: the model proxy
  listens on `127.0.0.1:9001` in the one shared network namespace, so **every
  process on that host can call it**, not only the agent: the other services,
  and anything else running there. It still is not routed by the public
  proxy, so it is not reachable from the internet. What still separates the
  agent from the key in this layout is the container boundary alone, without
  any network separation: the key is in the environment of a process in
  another container (separate mount and process namespaces, a different user
  id, no capabilities on either side, no Docker socket), and the model
  proxy's protocol has no call that returns it. The agent cannot capture
  traffic either (no `CAP_NET_RAW`), and the key leaves the model proxy only
  inside TLS to the endpoint (with a plain `http://` endpoint it would cross
  the loopback interface unencrypted; the model proxy logs a note at start).
  A kernel or sandbox escape from the agent container would defeat this, as
  it would defeat the default layout.

With a central gateway (the same program with `MODELPROXY_MODE=gateway`, run
by whoever hosts many bots; `docs/MODELPROXY.md`), the bot's model proxy is
given the gateway's address and a per-bot token instead of a provider key:

- Nothing in the bot's compose application is then worth more than that one
  bot: the token can spend that bot's daily allowance on the models on its
  list and nothing else, and the gateway can switch it off or replace it
  without touching the bot. The provider key exists only in the gateway.
- The gateway stores the SHA-256 of each token, not the token. Its admin API
  (the tenant table, the usage report) is on a second listener that must not
  be published; the main listener answers `/admin/*` with 404. Anyone who can
  reach the admin listener can replace the tenant table, so keep it on a
  private network, and set `MODELPROXY_ADMIN_TOKEN` if other containers share
  that network.
- Whoever runs the gateway can read every request and reply that passes
  through it (it ends the bots' TLS and opens its own to the provider), and
  sees which bot sent what and when. The program logs and stores only the
  bot id, path, status, token counts and duration per request.
- A bot's allowance is checked before a call and the cost is known after it:
  a bot can overshoot by the cost of the calls it has in progress (at most
  `MODELPROXY_TENANT_CONCURRENCY`, default 4).

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

### Hosted mode: sign-in by one-time token (`GATE_SSO=on`)

For a bot that a site runs for its owner and shows in a frame on its own page
(`compose.hosted.yaml`). The self-hosted bot does not use it, and with
`GATE_SSO` off the gate and the proxy behave as described above.

**What changes**

- There is no password login. `POST /_gate/login` answers 403 and the form is
  not served. `ACCESS_PASSWORD` (the gate demands 32 or more characters) is
  never typed: it is the secret from which the token key is derived, and the
  site's controller holds a copy.
- The site mints a token `v1.<bot_id>.<exp>.<jti>.<mac>` (HMAC-SHA256 under
  SHA-256 of `fugal-bot-gate-sso-v1\0` + `ACCESS_PASSWORD`) and the site's
  page posts it as a form into the frame, to `POST /_gate/sso`. The gate
  checks the format, the mac (constant time), that the id is `GATE_BOT_ID`,
  that it expires within the next 120 seconds (the minting side uses 60), and
  that it was not used before. A token is good for this one bot only.
- The post is accepted only when its `Origin` header is `GATE_PARENT_ORIGIN`,
  or absent. Browsers always name the origin of a cross-origin form post, so
  a request without the header comes from a program, not from a browser
  somebody is being tricked in; such a client needs a valid token like anyone
  else. `Origin: null` is refused, because any site can produce it. This
  check is not what protects the bot (the token is): it stops another site
  from signing a visitor's browser in to a bot that is not theirs.
- The session cookie is `__Host-pb_session`: `Secure`, `HttpOnly`, `Path=/`,
  no `Domain`, `SameSite=Strict`, lifetime `SESSION_HOURS`.
- A browser without a session is not redirected. It gets a small page
  (status 401, no inline script, policy `default-src 'none'; script-src
  'self'`): inside a frame it sends `{type: "fugal-bot:auth-required"}` to
  the page around it, addressed to `GATE_PARENT_ORIGIN` only, and that page
  posts a new token; opened directly it goes to `GATE_LOGIN_URL`. Requests
  that are not page loads, and websockets, get a plain 401.
- `POST /_gate/logout` clears the cookie (from the site's page when the user
  signs out there). It removes the cookie from that browser; it does not
  cancel the session: a copy of the cookie stays valid until it expires or
  `ACCESS_PASSWORD` changes. Open websockets stay open until the frame is
  closed.
- The proxy sends `Content-Security-Policy: frame-ancestors 'self'
  <GATE_PARENT_ORIGIN>` on every reply, rewrites the agent's own
  `frame-ancestors 'none'` to the same, and removes its `X-Frame-Options:
  DENY`. So that one origin may frame the bot and no other can.

**Replay and gate restarts.** The list of used tokens is in the gate's
memory. A restart of the gate forgets it, so a token that was used just
before the restart could be used a second time during the rest of its 60
seconds. To close most of that, the gate refuses every token that expires
less than 60 seconds after its own start, which is every token made before
it started, as long as the two clocks agree. With a clock difference of `d`
seconds, a token made in the last `d` seconds before the restart can still
be replayed. Whoever replays one needs to have seen it: it travels in the
body of an HTTPS request (never in an address), through Cloudflare and the
room's loopback interface.

**Failed attempts** are counted like failed logins (5 per address and 50 in
total per 15 minutes; beyond that the answer is 429). Unlike the password
form, a valid token is still accepted while the limit is reached. The mac is
256 bits, so more attempts do not help a guesser, and this way nobody can
lock the owner out by sending junk.

**Why `__Host-`, and the same-site caveat.** Hosted bots live under the
site's own domain (`b-<random>.<site domain>`). To a browser, the site, your
bot and every other owner's bot are then the *same site*, although they are
different origins. Consequences, and what is done about each:

- *Cookies can be planted across sibling hosts.* Any host under the domain
  may set a cookie with `Domain=<site domain>`, which the browser then sends
  to every other host under it. A cookie whose name starts with `__Host-` is
  only accepted when it was set by the exact host, over HTTPS, with `Path=/`
  and without `Domain`. So another bot (or anything else under the domain)
  cannot give your browser a session cookie for your bot, and yours is never
  sent to them. In hosted mode the gate looks at `__Host-pb_session` and at
  no other name.
- *`SameSite` does not separate the bots.* `SameSite=Strict` keeps the cookie
  off requests started by other sites, and it still works inside the frame
  because the framing page is same-site (this is why `None` is not needed,
  and it would be worse: sent from everywhere, and dropped by browsers that
  block third-party cookies). But a page on another bot's host, or on the
  site itself, is same-site, so the browser attaches your cookie to requests
  that page makes to your bot. The gate therefore refuses, in hosted mode,
  every request that carries the cookie but was started by another origin,
  unless it is a plain page load (which is how the frame is opened): no API
  call, no form post, no websocket. It decides by `Sec-Fetch-Site`, which
  the browser sets and a page cannot forge, and by `Origin` for browsers
  that do not send it. Framing by a sibling is stopped separately, by
  `frame-ancestors`.
- *What remains.* A page under the domain can still send a browser to your
  bot (a page load, with your session). Scripts on your bot's own
  origin, which the agent controls (it can publish pages there), run with
  your session on that bot: that was always so. Such a script can also send
  same-site requests to the site and to other bots with whatever cookies the
  browser holds for them, so the site's own API must check `Origin` on
  everything that changes state and use `__Host-` cookies too, and it must
  check the origin and source of every message its page receives from the
  frame. The token key is per bot, so nothing in one room opens another bot.

**What a shell inside the bot can and cannot do to the gate (all-host-network
layout).** There the gate listens on `127.0.0.1:9000` in the network
namespace the agent shares.

- It cannot read `ACCESS_PASSWORD` or the keys derived from it: they are in
  the environment of the gate's and the proxy's containers (other mount and
  process namespaces, other user ids, no capabilities, no Docker socket). So
  it cannot mint tokens or session cookies, for this bot or any other.
- It cannot see tokens or cookies in transit: it has no `CAP_NET_RAW`, and
  the gate logs neither (not even the query string).
- It can call the gate directly, with any `X-Gate-Client-IP` it likes. That
  lets it use up the failed-attempt limit for any address. In hosted mode
  this locks nobody out (see above); with the password gate it can.
- It can ask the gate whether a cookie is valid, which gains it nothing: it
  already reaches the gateway directly on loopback, with the owner's rights.
- If the gate's process is ever down, the agent can bind `127.0.0.1:9000`
  itself and answer the proxy in the gate's place. It would then receive the
  tokens and cookies that browsers send, and could let anyone in. That gives
  an attacker who already controls the agent nothing it did not have (the
  bot's data and the agent's rights), and nothing outside this bot. The same
  holds for the other loopback ports.
- The tunnel's token is in the tunnel container's environment only, never on
  a command line. cloudflared's metrics port is open to every local process,
  the agent's shell included. It serves counters, readiness, the tunnel's
  hostname, id and ingress rule, and Go profiling pages. In a real room the
  token and its secret part were searched for in what 15 of those pages
  return (metrics, config, the `/diag/` pages, variables, goroutine and heap
  dumps): not found. That was one cloudflared version (2026.11.1), not an
  audit of every page.
- `compose.hosted.yaml` binds that port to loopback (`--metrics
  127.0.0.1:2000`). This is needed: without the flag, cloudflared in a
  container listens on **all** interfaces (`[::]:20241`), which on the host
  network means every address the room has.

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
  injection can exfiltrate whatever the bot can read (see section 2): the
  conversations, files, browser logins and the Telegram token, but not the
  model key. It can also spend the inference allowance, up to the budget.
- **One shared password**, no accounts, no 2FA; the session cookie lasts 7
  days (`SESSION_HOURS`).
- **Hosted mode trusts the site completely.** Whoever holds the bot's
  `ACCESS_PASSWORD` (the site's controller) can mint a sign-in for it at any
  time, and whoever controls the page at `GATE_PARENT_ORIGIN` can frame the
  bot and act in it as the signed-in owner. The user's protection is the
  site's own sign-in, not anything in this package.
- **Supply chain**: images are pinned by digest, but the Debian packages added
  in `bot/Dockerfile` come from the live Debian mirror at build time (not
  pinned to a snapshot), and we use upstream's release image rather than our
  own source build (see `UPSTREAM.md`).
- **Background model use**: OpenClaw's memory plugin schedules a "dreaming"
  (memory consolidation) job; with a paid model this spends tokens without a
  user message. It goes through the model proxy like every other call, so it
  is counted and a budget bounds it. `BACKGROUND_MODEL_USE=off` switches it
  off (it was the only job v2026.11.6 schedules by itself; the smoke test
  checks that none is left). Schedules the user or the agent creates still
  run and still spend.
- **The house rules are instructions, not controls.** With `SEED_WORKSPACE=1`
  the agent's `AGENTS.md` tells it to ask before sending messages to others,
  posting, buying or deleting, and never to handle wallet secrets. A model
  can ignore or be talked out of instructions. OpenClaw enforces approval
  only for shell commands (`EXEC_MODE=ask`); it has no approval gate for what
  the agent does in its browser.
- **The Telegram token is still in the agent container** (its environment),
  readable by the agent. Only the model key has been moved out.

## 4. Backups: what one contains, who holds the key

`bot/backup.sh` (started by the operator; nothing in the bot starts it) makes
one archive of the bot's home directory, encrypts it with age to the public
key in `BACKUP_AGE_RECIPIENT`, and uploads it to a presigned URL.

What a backup contains:

- everything that makes the bot this bot: conversations, memory files, the
  workspace, schedules, OpenClaw's config, pairing approvals;
- the browser profile: **cookies and saved logins of every site the bot is
  signed in to**, history, site storage;
- anything the agent or the user saved in the home directory, which can
  include secrets someone pasted into a chat or a file.

What it does not contain: the model API key or the per-bot gateway token
(they are in the model proxy's environment), the access password, the
Telegram token, the tunnel token (all environment only), the model proxy's
usage counters, the proxy's TLS state, browser and package caches, logs, and
the VNC password.

Who can read one:

- Only the holder of the age **private** key. The bot is given the public key
  and nothing else, so neither the agent nor anyone who takes over the
  container can decrypt a backup, old or new, and the storage provider sees
  ciphertext and sizes only.
- In a hosted setup the operator's controller creates one key pair per bot
  and keeps the private key in its own key store, never in the room. That
  protects backups from the storage provider and from the room's host; it
  does not protect them from the operator, who holds the key. A self-hoster
  holds their own key (`age-keygen`); losing it loses the backups.
- For a restore the private key has to be in the room for one command
  (`restore.sh <identity file>`). It is read by age only, never copied into
  the data directory, and should sit somewhere that is wiped with the room.
  While it is there, the room's host can read it, and with it every backup
  of that bot. A restore on a host you do not trust exposes the history.
- The two one-shot services of the hosted overlay (`compose.hosted.yaml`)
  keep that shape. `backup` gets the home directory read-only and, of all
  the settings, only the public key. `restore` gets the identity as a
  read-only file for that one run, and no other secret. Both run as the
  bot's unprivileged user without any capability, on the host network (the
  only one with a way out in a room).

What encryption to a public key does not give:

- **No proof of origin.** Anyone who knows the public key can produce a file
  that decrypts. A backup is the agent's own data directory, so it was
  already whatever the agent made of it; `restore.sh` unpacks it inside the
  data directory only, as the bot's unprivileged user, refuses member names
  that leave it, and executes nothing from it. The SHA-256 that `backup.sh`
  prints lets the operator check that the object in storage is the one that
  was uploaded.
- **The presigned URL is a secret for its lifetime.** It arrives on standard
  input and is passed to curl in a private file, never on a command line and
  never in output or error messages. During a hot backup the agent runs as
  the same user in the same container, so a hijacked agent could in
  principle read that file while the upload runs. The URL lets its holder
  write one object (that bot's backup), nothing else; keep its lifetime
  short. A cold backup has no agent running.
- **A hot backup is not one instant.** OpenClaw snapshots each of its
  databases consistently, but the config, the databases and the plain files
  are captured one after another, and the browser's own databases are copied
  as they are while the browser may be writing. A cold backup, taken with
  the bot stopped, has neither limit.

## 5. What the sealed stage (TDX VM with dstack) needs from this package

The package already is one compose app, one data directory, one published
port, secrets only from the environment, digest-pinned images. Still needed:

1. **Volume encryption**: the data volume (or `DATA_DIR`) on an encrypted disk
   whose key is released only to the attested VM (dstack KMS or the local key
   provider), plus an app-level encrypted backup, because a dstack disk cannot
   yet be moved to another host.
2. **Secrets entered inside the box**: the model key, Telegram token and access
   password must arrive encrypted to the attested VM (dstack's encrypted
   environment, or a first-run page served over attested TLS), never via a
   host-readable `.env`. The model key is already outside the agent's
   container (the model proxy adds it to requests); the Telegram token needs
   the same treatment.
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
6. **Remove dev conveniences**: drop `host.docker.internal` (now only on the
   model proxy), keep
   `ENABLE_OPENAI_API` off unless needed, and make the config read-only to the
   agent (separate users for gateway and shell).
