# Model proxy (stdlib Python, no packages): holds the model endpoint and API
# key so the agent container never does, and meters tokens. Same pinned
# python:3.13-alpine as the gate.
FROM python@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e
LABEL org.opencontainers.image.title="fugal-bot-modelproxy" \
      org.opencontainers.image.description="Fugal Bot model proxy: keeps the model API key away from the agent, meters tokens, enforces a budget (Python stdlib)" \
      org.opencontainers.image.source="https://github.com/fugal-ai/fugal-bot" \
      org.opencontainers.image.licenses="MIT"
COPY --chmod=0755 modelproxy.py /opt/modelproxy/modelproxy.py
# Its counters live in /data: the "modelproxy" sub-directory of the bot's one
# volume, owned by this user and not mounted into the agent container.
USER 1002:1002
ENTRYPOINT ["python3", "-I", "-u", "/opt/modelproxy/modelproxy.py"]
