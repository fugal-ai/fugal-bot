# Reverse proxy: the ONE published port. Caddy 2.11.9 (alpine), pinned by digest.
FROM caddy@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0
LABEL org.opencontainers.image.title="fugal-bot-proxy" \
      org.opencontainers.image.description="Fugal Bot reverse proxy: Caddy 2.11.9, the bot's one public port" \
      org.opencontainers.image.source="https://github.com/fugal-ai/fugal-bot" \
      org.opencontainers.image.licenses="MIT"
COPY --chmod=0755 entrypoint.sh /usr/local/bin/fugal-bot-proxy
COPY --chmod=0755 tunnel-url.sh /usr/local/bin/fugal-bot-tunnel-url
# Caddy keeps ACME account + certificates under XDG_DATA_HOME (=/data, the
# "proxy" sub-directory of the bot's one volume). Runs as an unprivileged user.
ENV XDG_DATA_HOME=/data XDG_CONFIG_HOME=/tmp/caddy-config
USER 1001:1001
ENTRYPOINT ["/usr/local/bin/fugal-bot-proxy"]
