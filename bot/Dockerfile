# The bot: upstream OpenClaw (pinned release image) + its own Linux desktop
# (Xvfb, Openbox, xterm, Chromium, x11vnc, noVNC) in ONE container, so the
# agent's shell, files and browser are the same computer the owner watches.
#
# Upstream is NOT modified at source level. Everything we change lives in this
# directory (see ../UPSTREAM.md and ../SECURITY-NOTES.md):
#   - the build below: extra apt packages, removal of the ClawHub skill and
#     of the Codex program's binary, our entrypoint / supervisor / config
#     renderer; the result is copied into a fresh image as one layer;
#   - runtime config written by render-config.mjs (marketplace, installs,
#     updates and telemetry off; model + channels from the environment).

# OpenClaw v2026.11.6, release commit 06afb10d3272cc9deaf38ca6a3b7b839d5935638,
# multi-arch index digest (amd64 manifest sha256:ccb0d04f...). MIT licence.
ARG OPENCLAW_IMAGE=ghcr.io/openclaw/openclaw@sha256:65e229536a2698f0ab211c8a3d1c5c8e2685d19379d153e95f93ff76f22779cf
FROM ${OPENCLAW_IMAGE} AS prep

USER root

# Desktop + browser. Debian trixie packages (the upstream base is
# node:24.22.1-trixie-slim). NOTE: apt is not yet pinned to a snapshot date;
# stage 1 must pin it via snapshot.debian.org for a bit-reproducible build.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      xvfb x11vnc novnc python3-websockify openbox xterm x11-utils x11-xserver-utils xdotool \
      chromium fonts-dejavu-core fonts-liberation supervisor procps less nano file \
 && rm -rf /var/lib/apt/lists/*

# Remove the bundled ClawHub marketplace skill (it teaches the agent to search
# and install third-party skills). Installs are also blocked by config.
RUN rm -rf /app/skills/clawhub \
 && install -d -m 1777 /tmp/.X11-unix

# Backups (backup.sh / restore.sh): age encrypts the archive to a public key,
# curl moves it. Debian trixie packages like the ones above, under the same
# not-yet-pinned apt note. curl is already in the upstream image; naming it
# here keeps it if upstream drops it. A layer of its own, after the large
# desktop layer, so that one stays cached.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends age curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && age --version && curl --version | head -1

COPY --chown=root:root supervisord.conf /etc/fugal-bot/supervisord.conf
COPY --chown=root:root entrypoint.sh render-config.mjs seed-workspace.mjs healthcheck.sh start-gateway.sh backup.sh restore.sh /opt/fugal-bot/
COPY --chown=root:root seed/ /opt/fugal-bot/seed/
COPY --chown=root:root openbox-autostart /etc/xdg/openbox/autostart
RUN chmod 0755 /opt/fugal-bot/*.sh /opt/fugal-bot/seed /etc/xdg/openbox/autostart \
 && chmod 0644 /opt/fugal-bot/*.mjs /opt/fugal-bot/seed/* /etc/fugal-bot/supervisord.conf

# Left out of the image: the Codex command-line program's binary (404 MB in
# one platform package, linux-x64 or linux-arm64). It is the runtime behind
# OpenClaw's "codex" agent harness for OpenAI subscription logins. This bot
# reaches its model as an OpenAI-compatible endpoint through the model proxy
# and never starts it. The harness's own code stays in place; using it would
# fail with a missing-package error. The build stops if the package is not
# where it is expected, so an upstream change here gets looked at.
RUN n=0; for d in /app/node_modules/.pnpm/@openai+codex@*-linux-*; do \
      if [ -d "$d" ]; then rm -rf "$d" && n=$((n + 1)); fi; \
    done; test "$n" = 1

# The upstream image's environment, written down for the check in the last
# stage. The second line stops the build if upstream sets a variable that the
# last stage does not know about.
RUN env | grep -E '^(PATH|NODE_VERSION|YARN_VERSION|COREPACK_HOME|PLAYWRIGHT_BROWSERS_PATH|NODE_ENV)=' | sort > /etc/fugal-bot/upstream-env \
 && test "$(env | grep -c -v -E '^(PATH|NODE_VERSION|YARN_VERSION|COREPACK_HOME|PLAYWRIGHT_BROWSERS_PATH|NODE_ENV|HOME|HOSTNAME|PWD|SHLVL|OLDPWD|_)=')" = 0

# ---- the image -------------------------------------------------------------------
# The prepared file system as ONE layer. Deleting a file in a later layer of
# the same image only hides it: the layer below is still downloaded and
# unpacked. Copied into a fresh image, what was deleted above is really gone,
# and a host unpacks 3 layers instead of 55 (in a sandbox room unpacking the
# image is most of the 5 to 8 minutes a pull takes, at every daily restart).
# FROM scratch inherits nothing, so everything the upstream image set is set
# again here: its environment (checked by the RUN below against the copy made
# above), the user, the version label.
FROM scratch
COPY --from=prep / /
ENV PATH=/home/node/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    NODE_VERSION=24.22.1 \
    YARN_VERSION=1.22.22 \
    COREPACK_HOME=/usr/local/share/corepack \
    PLAYWRIGHT_BROWSERS_PATH=/home/node/.cache/ms-playwright \
    NODE_ENV=production
RUN env | grep -E '^(PATH|NODE_VERSION|YARN_VERSION|COREPACK_HOME|PLAYWRIGHT_BROWSERS_PATH|NODE_ENV)=' | sort | diff - /etc/fugal-bot/upstream-env

# Everything the bot owns lives under /home/node, which is the ONE persistent
# volume (mounted by compose). Nothing secret is baked into this image.
ENV HOME=/home/node \
    OPENCLAW_HOME=/home/node \
    OPENCLAW_STATE_DIR=/home/node/.openclaw \
    OPENCLAW_CONFIG_PATH=/home/node/.openclaw/openclaw.json \
    OPENCLAW_CONFIG_DIR=/home/node/.openclaw \
    OPENCLAW_WORKSPACE_DIR=/home/node/workspace \
    DISPLAY=:1 \
    OPENCLAW_NO_AUTO_UPDATE=1 \
    DO_NOT_TRACK=1 \
    OPENCLAW_TELEMETRY_ENDPOINT=http://127.0.0.1:9/disabled \
    OPENCLAW_CLAWHUB_URL=http://127.0.0.1:9/disabled \
    OPENCLAW_DISABLE_BONJOUR=1 \
    TZ=UTC

# Labels last, so changing them never invalidates the slow layers above.
# The upstream image carries its own revision/created labels; replace them so
# they describe this image. Pass both when publishing, e.g.
#   --build-arg GIT_REVISION=$(git rev-parse HEAD) --build-arg BUILD_DATE=$(date -u +%FT%TZ)
ARG GIT_REVISION=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.title="fugal-bot" \
      org.opencontainers.image.revision="${GIT_REVISION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.description="Fugal Bot: an OpenClaw agent with its own Linux desktop, shell and Chromium (OpenClaw v2026.11.6 + desktop layers)" \
      org.opencontainers.image.source="https://github.com/fugal-ai/fugal-bot" \
      org.opencontainers.image.url="https://github.com/fugal-ai/fugal-bot" \
      org.opencontainers.image.documentation="https://github.com/fugal-ai/fugal-bot" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="2026.11.6" \
      org.opencontainers.image.base.name="ghcr.io/openclaw/openclaw:2026.11.6" \
      org.opencontainers.image.base.digest="sha256:65e229536a2698f0ab211c8a3d1c5c8e2685d19379d153e95f93ff76f22779cf"

USER node
WORKDIR /home/node

EXPOSE 18789 6080
# A long start period: on a slow host (a sandbox room reading a freshly
# unpacked image) a start has taken up to 621 s, and with the earlier 300 s
# the container was reported "unhealthy" on its way to healthy. A check that
# passes inside the period still marks it healthy at once, so a fast host
# loses nothing.
HEALTHCHECK --interval=10s --timeout=8s --start-period=900s --retries=6 \
  CMD ["/opt/fugal-bot/healthcheck.sh"]

ENTRYPOINT ["tini", "-s", "--", "/opt/fugal-bot/entrypoint.sh"]
