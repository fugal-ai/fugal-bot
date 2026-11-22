# The bot: upstream OpenClaw (pinned release image) + its own Linux desktop
# (Xvfb, Openbox, xterm, Chromium, x11vnc, noVNC) in ONE container, so the
# agent's shell, files and browser are the same computer the owner watches.
#
# Upstream is NOT modified at source level. Everything we change lives in this
# directory (see ../UPSTREAM.md and ../SECURITY-NOTES.md):
#   - image layers below: extra apt packages, removal of the ClawHub skill,
#     our entrypoint / supervisor / config renderer;
#   - runtime config written by render-config.mjs (marketplace, installs,
#     updates and telemetry off; model + channels from the environment).

# OpenClaw v2026.11.6, release commit 06afb10d3272cc9deaf38ca6a3b7b839d5935638,
# multi-arch index digest (amd64 manifest sha256:ccb0d04f...). MIT licence.
ARG OPENCLAW_IMAGE=ghcr.io/openclaw/openclaw@sha256:65e229536a2698f0ab211c8a3d1c5c8e2685d19379d153e95f93ff76f22779cf
FROM ${OPENCLAW_IMAGE}


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

COPY --chown=root:root supervisord.conf /etc/fugal-bot/supervisord.conf
COPY --chown=root:root entrypoint.sh render-config.mjs healthcheck.sh start-gateway.sh /opt/fugal-bot/
COPY --chown=root:root openbox-autostart /etc/xdg/openbox/autostart
COPY --chown=root:root novnc-index.html /usr/share/novnc/index.html
RUN chmod 0755 /opt/fugal-bot/*.sh /etc/xdg/openbox/autostart \
 && chmod 0644 /opt/fugal-bot/render-config.mjs /etc/fugal-bot/supervisord.conf /usr/share/novnc/index.html

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
      org.opencontainers.image.base.name="ghcr.io/openclaw/openclaw:2026.11.6" \
      org.opencontainers.image.base.digest="sha256:65e229536a2698f0ab211c8a3d1c5c8e2685d19379d153e95f93ff76f22779cf"

USER node
WORKDIR /home/node

EXPOSE 18789 6080
HEALTHCHECK --interval=10s --timeout=8s --start-period=300s --retries=6 \
  CMD ["/opt/fugal-bot/healthcheck.sh"]

ENTRYPOINT ["tini", "-s", "--", "/opt/fugal-bot/entrypoint.sh"]
