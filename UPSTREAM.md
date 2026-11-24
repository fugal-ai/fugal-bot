# Upstream and third-party components

Fugal Bot is a thin packaging layer. It contains no upstream source code; it
pins upstream images by digest and adds configuration and small scripts.

## OpenClaw (the agent)

| | |
|---|---|
| Project | OpenClaw, https://github.com/openclaw/openclaw |
| Licence | MIT, "Copyright (c) 2026 OpenClaw Foundation" (full text: `upstream/OPENCLAW-LICENSE`) |
| Release | `v2026.11.7` (stable, published 2026-11-23) |
| Release commit | `e31bbf056de321127988ec5c1a36ff1b59297de0` (tag object `de3c8f72b0a6d4fab04bf2f88c0f331d8410447a`) |
| Image | `ghcr.io/openclaw/openclaw@sha256:461cc655301409901f2df11d6ec9f029f9709e4607826e9780fd74f8e1d12a16` (index; linux/amd64 manifest `sha256:9d74311eac69edf8f80c1349a12cceabbaf5241d36d62bdc3524e6feb56d982b`, linux/arm64 `sha256:fb0f19c9a2573cde08d944b950be32cfae416c145d28430fda10aae74d26739e`) |
| Checked (2026-11-23) | the image's config labels name version `2026.11.7` and revision `e31bbf05…7de0`; its environment is the same six variables as before; the licence file is unchanged |
| npm tarball of the same release | `openclaw@2026.11.7`, integrity `sha512-6x49d02jEj8jjQAEkSZpyX8BCNnJLLCV3IHUTxQuN+OEJepo7RODk8JHURivefy+WZMqzvsyp+p8DlZUQnub6w==` |

### Why this release

- Newest stable release on 2026-11-23 (replaces v2026.11.6). Its notes add
  fixes only for us (conversation recovery across restarts, automations,
  dictation while microphone permission is pending, faster page switching);
  the breaking changes are to the plugin SDK, which we do not use. Config
  schema: the one change in `src/config/zod-schema*` is the default of
  `tools.exec.notifyOnExitEmptySuccess` (now true). `v2026.11.10-beta.*` are
  prereleases (agent templates from ClawHub, automatic update schedules, both
  of which we would switch off anyway).
- GitHub security advisories for the repository (read 2026-11-23): the
  newest, GHSA-pw3x-g8v8-fvj2 ("delegation could skip approval for restricted
  agents", high, published 2026-11-19), is fixed in 2026.10.33 and 2026.11.4.
  Every other advisory names a patched version of 2026.9.3 or earlier. None
  lists 2026.11.7 as affected.

### What we changed (all outside upstream's code)

Image layers (`bot/Dockerfile`):
- added Debian packages for the bot's desktop: Xvfb, Openbox, xterm, x11vnc,
  noVNC + websockify, Chromium, fonts, supervisor, xdotool;
- deleted the bundled `clawhub` skill (`/app/skills/clawhub`), which teaches
  the agent to search for and install third-party skills;
- added the Debian package `age` (1.2.1) for encrypting backups, and named
  `curl` (already in the upstream image) so it stays;
- added our scripts: `entrypoint.sh`, `start-gateway.sh`, `render-config.mjs`,
  `seed-workspace.mjs`, `backup.sh`, `restore.sh`, `healthcheck.sh`,
  `supervisord.conf`, Openbox autostart, noVNC landing page; and the starter
  workspace files in `seed/` (`IDENTITY.md`, `SOUL.md`, `USER.md`, `AGENTS.md`,
  `BOOTSTRAP.md`), which are ours, not copies of upstream's templates;
- the gateway is started without upstream's container entrypoint
  (`/app/docker-entrypoint.mjs`), which runs `openclaw doctor --fix` at every
  container start: `start-gateway.sh` runs the same doctor command (with the
  same activation-marker variable) only at the first start of an image on a
  data directory, recorded in `~/.openclaw/fugal-doctor.ok` against
  `/etc/fugal-bot/image-id` (OpenClaw version, commit, hash of our start
  files, written at build). A new data directory is marked at once (nothing
  to repair). In a sandbox room doctor was 111-160 s of every start;
- our own reference skill (`/opt/fugal-bot/skills/bittensor`, read-only),
  enabled with `skills.load.extraDirs`; bundled skills stay off;
- environment: `OPENCLAW_NO_AUTO_UPDATE=1`, `DO_NOT_TRACK=1`,
  `OPENCLAW_TELEMETRY_ENDPOINT` and `OPENCLAW_CLAWHUB_URL` pointed at an
  unreachable local address, `OPENCLAW_DISABLE_BONJOUR=1`;
- left out the binary of the Codex command-line program
  (`node_modules/.pnpm/@openai+codex@<version>-linux-<arch>`, 404 MB). It is
  the runtime behind OpenClaw's `codex` agent harness, which signs in with an
  OpenAI subscription. This package reaches its model as an OpenAI-compatible
  endpoint through the model proxy and never starts it; the harness's own
  code is untouched, and choosing it would fail with a missing-package error;
- the image is one layer of files: a last build stage copies the prepared
  file system into an empty image (`FROM scratch`, `COPY --from`), so the
  deleted files are not shipped at all and a host unpacks 3 layers instead
  of 55. Measured locally: 800 MB compressed instead of 1,058 MB, 2.53 GB
  unpacked instead of 2.94 GB; apart from the Codex binary and one small
  file of ours, every path, mode, owner and link is the same as before.
  That stage sets again what the upstream image set (environment, user,
  version label), and the build stops if upstream's environment differs from
  the copy in `bot/Dockerfile`. The price: a new version of our image shares
  no layer with the previous one or with upstream's, so an update downloads
  all of it.

Configuration written at every start (`bot/render-config.mjs`; full list and
reasons in `SECURITY-NOTES.md`): all skill and plugin installs blocked, bundled
skills off, skill self-learning off, update checks and auto-update off,
telemetry off, gateway auth delegated to our proxy, model endpoint and Telegram
from the environment. Optional, from the environment: the agent's name and
emoji (`agents.entries.<id>.identity`), the accent colour (`ui.seamColor`),
the model's input kinds, extra request fields
(`agents.defaults.models["bot/<model>"].params.extra_body`), dreaming off
(`plugins.entries.memory-core.config.dreaming.enabled`), and a `tools.deny`
list. All of these are upstream's documented config keys.

Upstream behaviour we rely on and checked on v2026.11.6 (re-checked by the smoke test on each upgrade) (the smoke test
checks each again, so an upgrade that changes one fails it):

- A workspace that already holds the five files, with a `BOOTSTRAP.md` that
  differs from upstream's, keeps all of them. The custom `BOOTSTRAP.md` stays
  "pending" (it is put into the system prompt) until it is deleted, and is
  not recreated afterwards.
- `openclaw backup create --verify --json` works while the gateway runs
  (SQLite stores) and lays files out under `payload/posix/<source path>`.
- With dreaming disabled, the "Memory Dreaming Promotion" job is not
  scheduled.

Process layout: supervisord (non-root) runs the desktop and the gateway; the
gateway is still started through upstream's own `docker-entrypoint.mjs`.

### Why the release image instead of a source build

Upstream's Dockerfile builds with an 8 GB Node heap and runs install scripts
for about a dozen native dependencies (`allowBuilds` in `pnpm-workspace.yaml`)
that we have not reviewed. The release image is built by upstream's release
workflow from the commit above. Before a sealed release we should rebuild from
source at that commit (or verify upstream's build provenance) so that the image
is reproducible by us, not only pinned.

### Updating

1. Read the release notes and the security advisories since `v2026.11.7`.
2. `docker buildx imagetools inspect ghcr.io/openclaw/openclaw:<version>`; put
   the new index digest in `bot/Dockerfile` and the commit in this file. In
   the last stage of `bot/Dockerfile`, bring the copied environment
   (`NODE_VERSION` and the others) and the version label up to date; the
   build stops and shows the difference if the environment does not match,
   or if the Codex package is no longer where it is removed.
3. Start the bot and read `docker compose logs bot` for config errors; run
   `scripts/smoke.sh`.

## Other images and packages

| Component | Used for | Pinned as | Licence |
|---|---|---|---|
| Caddy 2.11.9 (`caddy:2.11.9-alpine`) | reverse proxy, TLS | `caddy@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0` | Apache-2.0 |
| Caddy 2.11.9 release archive (only for `compose.cathedral.yaml`, `proxy/Dockerfile.nocap`) | same proxy, on hosts that cannot store file capabilities | SHA-512 of `caddy_2.11.9_linux_{amd64,arm64}.tar.gz` checked in the Dockerfile | Apache-2.0 |
| Python 3.13 (`python:3.13-alpine`) | password gate and model proxy (both stdlib only), test brain | `python@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e` | PSF-2.0 |
| cloudflared 2026.11.1 (optional `tunnel` profile) | outbound tunnel | `cloudflare/cloudflared@sha256:e613aadbc2d9511785d2e67a86b0c8ac18dd63a3f9af0d06faf04cbfdcb35f2d` | Apache-2.0 |
| Node.js 24 + Debian trixie (inside the OpenClaw image) | runtime | via the OpenClaw digest | MIT (Node.js); Debian packages under their own licences |
| noVNC | desktop in the browser | Debian trixie package | MPL-2.0 |
| websockify | WebSocket bridge for noVNC | Debian trixie package | LGPL-3.0 |
| x11vnc | VNC server | Debian trixie package | GPL-2.0 |
| Xvfb (X.Org server) | virtual screen | Debian trixie package | MIT/X11 |
| Openbox | window manager | Debian trixie package | GPL-2.0 |
| xterm | terminal | Debian trixie package | MIT/X11 |
| Chromium | the bot's browser | Debian trixie package | BSD-3-Clause and others (see the package) |
| supervisor | process manager | Debian trixie package | BSD-style (Repoze) |
| age 1.2.1 | encrypting backups to a public key | Debian trixie package | BSD-3-Clause |
| curl | uploading and downloading backups | in the OpenClaw image (Debian trixie package) | curl licence (MIT-style) |
| async-substrate-interface 2.2.1, cyscale 0.8.0, websockets 17.4, xxhash 4.1.0, aiosqlite 0.22.1 | the chain tool `taoq` (a virtual environment on the image's Python 3.13) | wheels pinned by sha256 in `bot/tools/requirements.txt`; pip 26.3 runs from its wheel (sha256 in `bot/Dockerfile`) and is not kept | MIT, Apache-2.0, BSD-3-Clause, BSD-2-Clause, MIT |

The Debian packages are installed with `apt-get` during the build and are not
yet pinned to a snapshot date (see `SECURITY-NOTES.md`, gaps). Their licence
texts are inside the built image under `/usr/share/doc/<package>/copyright`.
The Cloudflare quick tunnel is a Cloudflare service with its own terms; it is
off by default.
