# Upstream and third-party components

Fugal Bot is a thin packaging layer. It contains no upstream source code; it
pins upstream images by digest and adds configuration and small scripts.

## OpenClaw (the agent)

| | |
|---|---|
| Project | OpenClaw, https://github.com/openclaw/openclaw |
| Licence | MIT, "Copyright (c) 2026 OpenClaw Foundation" (full text: `upstream/OPENCLAW-LICENSE`) |
| Release | `v2026.11.6` (stable, published 2026-11-19) |
| Release commit | `06afb10d3272cc9deaf38ca6a3b7b839d5935638` (tag object `e780fed30f3a44ce38d61d26bc3cf8acc0b8066d`) |
| Image | `ghcr.io/openclaw/openclaw@sha256:65e229536a2698f0ab211c8a3d1c5c8e2685d19379d153e95f93ff76f22779cf` (index; linux/amd64 manifest `sha256:ccb0d04f14edd04c201b85bcee4612e7c084158a3d8fafa77a3c78554587edca`) |
| Checked | `openclaw --version` in the image prints `OpenClaw 2026.11.6 (06afb10)`; `/app/dist/build-info.json` records commit `06afb10d…5638` |
| npm tarball of the same release | `openclaw@2026.11.6`, integrity `sha512-4MGjAMv8QlVrul3ZbCu8KjM7GhXfLdLzf0IrsO5QZ8LC3oYkduqs2rZiZpPDmTRpT6kXYke/OVaWrB+YmhBHLw==` |

### Why this release

- Newest stable release on 2026-11-22. `v2026.11.10-beta.1` is a prerelease;
  it adds agent templates installed from ClawHub packages and automatic update
  schedules, both of which we would switch off anyway.
- GitHub security advisories for the repository (the 100 most recent, read
  2026-11-22): the newest, GHSA-pw3x-g8v8-fvj2 ("delegation could skip
  approval for restricted agents", high, published 2026-11-19), is fixed in
  2026.10.33 and 2026.11.4, both older than 2026.11.6. Every other advisory in
  that set names a patched version of 2026.9.3 or earlier. None lists 2026.11.6
  as affected.

### What we changed (all outside upstream's code)

Image layers (`bot/Dockerfile`):
- added Debian packages for the bot's desktop: Xvfb, Openbox, xterm, x11vnc,
  noVNC + websockify, Chromium, fonts, supervisor, xdotool;
- deleted the bundled `clawhub` skill (`/app/skills/clawhub`), which teaches
  the agent to search for and install third-party skills;
- added our scripts: `entrypoint.sh`, `start-gateway.sh`, `render-config.mjs`,
  `healthcheck.sh`, `supervisord.conf`, Openbox autostart, noVNC landing page;
- environment: `OPENCLAW_NO_AUTO_UPDATE=1`, `DO_NOT_TRACK=1`,
  `OPENCLAW_TELEMETRY_ENDPOINT` and `OPENCLAW_CLAWHUB_URL` pointed at an
  unreachable local address, `OPENCLAW_DISABLE_BONJOUR=1`.

Configuration written at every start (`bot/render-config.mjs`; full list and
reasons in `SECURITY-NOTES.md`): all skill and plugin installs blocked, bundled
skills off, skill self-learning off, update checks and auto-update off,
telemetry off, gateway auth delegated to our proxy, model endpoint and Telegram
from the environment.

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

1. Read the release notes and the security advisories since `v2026.11.6`.
2. `docker buildx imagetools inspect ghcr.io/openclaw/openclaw:<version>`; put
   the new index digest in `bot/Dockerfile` and the commit in this file.
3. Start the bot and read `docker compose logs bot` for config errors; run
   `scripts/smoke.sh`.

## Other images and packages

| Component | Used for | Pinned as | Licence |
|---|---|---|---|
| Caddy 2.11.9 (`caddy:2.11.9-alpine`) | reverse proxy, TLS | `caddy@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0` | Apache-2.0 |
| Python 3.13 (`python:3.13-alpine`) | password gate (stdlib only), test brain | `python@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e` | PSF-2.0 |
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

The Debian packages are installed with `apt-get` during the build and are not
yet pinned to a snapshot date (see `SECURITY-NOTES.md`, gaps). Their licence
texts are inside the built image under `/usr/share/doc/<package>/copyright`.
The Cloudflare quick tunnel is a Cloudflare service with its own terms; it is
off by default.
