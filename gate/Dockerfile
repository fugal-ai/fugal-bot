# Password gate (stdlib Python, no packages). python:3.13-alpine pinned by digest.
FROM python@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e
LABEL org.opencontainers.image.title="fugal-bot-gate" \
      org.opencontainers.image.description="Fugal Bot password gate: shared-password login with rate limiting (Python stdlib)" \
      org.opencontainers.image.source="https://github.com/fugal-ai/fugal-bot" \
      org.opencontainers.image.licenses="MIT"
COPY --chmod=0755 gate.py /opt/gate/gate.py
USER 65534:65534
ENTRYPOINT ["python3", "-I", "/opt/gate/gate.py"]
