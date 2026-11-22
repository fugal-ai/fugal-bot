# Upstream: OpenClaw

| | |
|---|---|
| Project | OpenClaw, https://github.com/openclaw/openclaw |
| Licence | MIT, Copyright (c) 2026 OpenClaw Foundation (copy: `upstream/OPENCLAW-LICENSE`) |
| Release | `v2026.11.6` (stable, published 2026-11-19) |
| Release commit | `06afb10d3272cc9deaf38ca6a3b7b839d5935638` (tag object `e780fed30f3a44ce38d61d26bc3cf8acc0b8066d`) |
| Image used | `ghcr.io/openclaw/openclaw@sha256:65e229536a2698f0ab211c8a3d1c5c8e2685d19379d153e95f93ff76f22779cf` (index; linux/amd64 manifest `sha256:ccb0d04f14edd04c201b85bcee4612e7c084158a3d8fafa77a3c78554587edca`) |
| Verified | `openclaw --version` in the image prints `OpenClaw 2026.11.6 (06afb10)`; `/app/dist/build-info.json` records commit `06afb10d…5638` |
| npm tarball (same release) | `openclaw@2026.11.6`, integrity `sha512-4MGjAMv8QlVrul3ZbCu8KjM7GhXfLdLzf0IrsO5QZ8LC3oYkduqs2rZiZpPDmTRpT6kXYke/OVaWrB+YmhBHLw==` |

## Why this release

- Newest stable release on 2026-11-22. `v2026.11.10-beta.1` is a prerelease and
  adds agent templates installed from ClawHub packages and automatic update
  schedules, both of which we would disable anyway.
- GitHub security advisories for the repository (read via the API on
  2026-11-22, 100 most recent): the newest, GHSA-pw3x-g8v8-fvj2 ("delegation
  could skip approval for restricted agents", high, published 2026-11-19), is
  fixed in 2026.10.33 and 2026.11.4, both older than 2026.11.6. Every other
  advisory in the set lists a patched version of 2026.9.3 or earlier. No
  advisory listed 2026.11.6 as vulnerable.

## How our changes are kept separate

We do not patch upstream source. The upstream release is consumed as its
official image, pinned by digest, and everything we change is in this
repository:

- `bot/Dockerfile`: layers on top of the upstream image (desktop and browser
  packages, removal of the bundled `clawhub` skill, our scripts).
- `bot/render-config.mjs`: the managed part of OpenClaw's config, written at
  every start (model endpoint, auth, channels, and everything we disable).
- `bot/entrypoint.sh`, `bot/start-gateway.sh`, `bot/supervisord.conf`: process
  layout (upstream's own `docker-entrypoint.mjs` still starts the gateway).

Why the image rather than a source build: upstream's Dockerfile builds with an
8 GB Node heap and runs install scripts for about a dozen native dependencies
(`allowBuilds` in `pnpm-workspace.yaml`), which we would have had to read
first and which does not fit this machine's memory budget. The release image
is built by upstream's release workflow from the commit above. Stage 1 should
rebuild from source at that commit (or verify upstream's provenance
attestation) so the image is reproducible by us, not only pinned.

## Updating

1. Read the release notes and the security advisories since `v2026.11.6`.
2. `docker buildx imagetools inspect ghcr.io/openclaw/openclaw:<version>`;
   put the new index digest in `bot/Dockerfile`, and the commit here.
3. Check that the config keys in `bot/render-config.mjs` still validate
   (`docker compose logs bot` shows config errors at start) and run
   `scripts/smoke.sh`.
