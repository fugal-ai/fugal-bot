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

# OpenClaw v2026.11.7, release commit e31bbf056de321127988ec5c1a36ff1b59297de0,
# multi-arch index digest (amd64 manifest sha256:9d74311e...). MIT licence.
ARG OPENCLAW_IMAGE=ghcr.io/openclaw/openclaw@sha256:461cc655301409901f2df11d6ec9f029f9709e4607826e9780fd74f8e1d12a16
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

# The chain tool's libraries (tools/taoq.py: read-only Bittensor reports, no
# wallet, no keys). A virtual environment on the image's own Python 3.13
# (python3 is already here for websockify) with async-substrate-interface and
# its four dependencies, every file pinned by sha256 (tools/requirements.txt,
# made by `uv pip compile --generate-hashes`), wheels only. pip runs from its
# own wheel, checked against a pinned sha256, and is not kept: the agent gets
# no package installer. (Plain shell variables, not ARGs: a build argument
# would show up in the environment check at the end of this stage.)
COPY --chown=root:root tools/requirements.txt /opt/fugal-bot/tools/requirements.txt
RUN pip_whl=/tmp/pip-26.3-py3-none-any.whl \
 && pip_sha=f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13 \
 && venv=/opt/fugal-bot/tools/venv \
 && python3 -m venv --without-pip "$venv" \
 && curl -fsSL --retry 3 -o "$pip_whl" https://files.pythonhosted.org/packages/07/25/ee8c38ae72d66b844ce8eb9711ba905dba21ca1a8a0c960057c41cd23337/pip-26.3-py3-none-any.whl \
 && echo "$pip_sha  $pip_whl" | sha256sum -c - \
 && "$venv/bin/python3" "$pip_whl/pip" install --no-cache-dir --disable-pip-version-check --no-input \
      --require-hashes --only-binary :all: --no-deps -r /opt/fugal-bot/tools/requirements.txt \
 && rm -f "$pip_whl" \
 && find "$venv" -path '*/scalecodec/*' -name '*.c' -delete \
 && "$venv/bin/python3" -m compileall -q -j 0 --invalidation-mode unchecked-hash "$venv/lib" \
 && "$venv/bin/python3" -I -c 'import async_substrate_interface, scalecodec, websockets, xxhash' \
 && ! "$venv/bin/python3" -I -c 'import pip' 2>/dev/null

COPY --chown=root:root supervisord.conf /etc/fugal-bot/supervisord.conf
COPY --chown=root:root entrypoint.sh render-config.mjs seed-workspace.mjs seed-history.json healthcheck.sh start-gateway.sh backup.sh restore.sh /opt/fugal-bot/
COPY --chown=root:root seed/ /opt/fugal-bot/seed/
COPY --chown=root:root skills/ /opt/fugal-bot/skills/
COPY --chown=root:root tools/taoq.py /opt/fugal-bot/tools/taoq.py
COPY --chown=root:root openbox-autostart /etc/xdg/openbox/autostart
RUN chmod 0755 /opt/fugal-bot/*.sh /opt/fugal-bot/seed /etc/xdg/openbox/autostart /opt/fugal-bot/tools/taoq.py \
 && chmod 0644 /opt/fugal-bot/*.mjs /opt/fugal-bot/*.json /opt/fugal-bot/seed/* /etc/fugal-bot/supervisord.conf /opt/fugal-bot/tools/requirements.txt \
 && find /opt/fugal-bot/skills -type d -exec chmod 0755 {} + && find /opt/fugal-bot/skills -type f -exec chmod 0644 {} + \
 && ln -sf /opt/fugal-bot/tools/taoq.py /usr/local/bin/taoq \
 && TAOQ_ENDPOINTS=ws://127.0.0.1:9 taoq --version

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
# last stage does not know about. Build arguments of this stage show up in a
# RUN step's environment too when a value is passed (the images workflow
# passes GIT_REVISION and BUILD_DATE), so they are on the list of names that
# are not upstream's, as are the tracing variables (OTEL_*, BUILDKIT_*, TRACEPARENT, TRACESTATE) that the
# builder on a CI runner adds to every RUN step; anything else is printed
# before the build stops.
RUN env | grep -E '^(PATH|NODE_VERSION|YARN_VERSION|COREPACK_HOME|PLAYWRIGHT_BROWSERS_PATH|NODE_ENV)=' | sort > /etc/fugal-bot/upstream-env \
 && extra="$(env | cut -d= -f1 | grep -v -E '^(PATH|NODE_VERSION|YARN_VERSION|COREPACK_HOME|PLAYWRIGHT_BROWSERS_PATH|NODE_ENV|HOME|HOSTNAME|PWD|SHLVL|OLDPWD|_|GIT_REVISION|BUILD_DATE|SOURCE_DATE_EPOCH|DEBIAN_FRONTEND|OTEL_[A-Z0-9_]*|BUILDKIT_[A-Z0-9_]*|TRACEPARENT|TRACESTATE)$' || true)" \
 && if [ -n "$extra" ]; then echo "unexpected environment variables in the upstream image: $extra" >&2; exit 1; fi

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
# What this image is, for start-gateway.sh: `openclaw doctor --fix` runs once
# per data directory and image, not at every start (see that script). The
# OpenClaw version, the commit, and a hash of our own start files (a local
# build has no commit). A tiny layer of its own; the big one above stays.
RUN { echo "openclaw $(node -p 'require("/app/package.json").version')"; \
      echo "revision ${GIT_REVISION}"; \
      echo "files $(cat /opt/fugal-bot/*.sh /opt/fugal-bot/*.mjs /etc/fugal-bot/supervisord.conf | sha256sum | cut -c1-16)"; \
    } > /etc/fugal-bot/image-id \
 && chmod 0644 /etc/fugal-bot/image-id && cat /etc/fugal-bot/image-id
LABEL org.opencontainers.image.title="fugal-bot" \
      org.opencontainers.image.revision="${GIT_REVISION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.description="Fugal Bot: an OpenClaw agent with its own Linux desktop, shell and Chromium (OpenClaw v2026.11.7 + desktop layers)" \
      org.opencontainers.image.source="https://github.com/fugal-ai/fugal-bot" \
      org.opencontainers.image.url="https://github.com/fugal-ai/fugal-bot" \
      org.opencontainers.image.documentation="https://github.com/fugal-ai/fugal-bot" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="2026.11.7" \
      org.opencontainers.image.base.name="ghcr.io/openclaw/openclaw:2026.11.7" \
      org.opencontainers.image.base.digest="sha256:461cc655301409901f2df11d6ec9f029f9709e4607826e9780fd74f8e1d12a16"

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
