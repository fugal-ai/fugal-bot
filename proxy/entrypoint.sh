#!/bin/sh
# Writes the Caddyfile for one of two modes, then runs Caddy (as non-root).
#   PUBLIC_HOSTNAME set   -> HTTPS with an automatic Let's Encrypt certificate
#                            (TLS-ALPN challenge on the one published port, 443).
#   PUBLIC_HOSTNAME empty -> plain HTTP (meant for 127.0.0.1 only).
# Addresses are overridable for the all-host-network layout (compose.cathedral.yaml).
set -eu
LISTEN_PORT="${PROXY_LISTEN_PORT:-8443}"
LISTEN_HOST="${PROXY_LISTEN_HOST:-}"
HEALTH_PORT="${PROXY_HEALTH_PORT:-8081}"
GATE="${GATE_UPSTREAM:-gate:9000}"
GATEWAY="${GATEWAY_UPSTREAM:-bot:18789}"
DESKTOP="${DESKTOP_UPSTREAM:-bot:6080}"
# Verified same-origin browser Origins are rewritten to this value; the bot's
# render-config.mjs always allows it. Same variable and default on both sides.
UI_ORIGIN="${CONTROL_UI_ORIGIN:-https://bot.invalid}"
BIND="${BIND_ADDRESS:-127.0.0.1}"
case "$BIND" in 127.0.0.1|localhost|::1|"[::1]") LOOPBACK=1 ;; *) LOOPBACK=0 ;; esac
if [ -z "${ACCESS_PASSWORD:-}" ]; then
  echo "[proxy] ACCESS_PASSWORD is required; refusing to start" >&2; exit 2
fi
if [ "$LOOPBACK" = 0 ] && [ "${#ACCESS_PASSWORD}" -lt 12 ]; then
  echo "[proxy] BIND_ADDRESS=$BIND is not loopback: ACCESS_PASSWORD must be at least 12 characters" >&2; exit 2
fi
if [ "$LOOPBACK" = 0 ] && [ -z "${PUBLIC_HOSTNAME:-}" ] && [ "${ALLOW_PUBLIC_HTTP:-}" != "yes" ]; then
  echo "[proxy] refusing plain HTTP on a non-loopback address; set PUBLIC_HOSTNAME (or ALLOW_PUBLIC_HTTP=yes)" >&2; exit 2
fi

if [ -n "${PUBLIC_HOSTNAME:-}" ]; then
  SITE="https://${PUBLIC_HOSTNAME}"
  TLS="tls ${ACME_EMAIL:-} {
		issuer acme {
			disable_http_challenge
		}
	}"
  HSTS='Strict-Transport-Security "max-age=31536000"'
  HTTPS_PORT="$LISTEN_PORT"
  HTTP_PORT=8448                     # unused (no port-80 challenge, no redirects)
else
  SITE="http://:${LISTEN_PORT}"
  TLS=""
  HSTS=""
  HTTP_PORT="$LISTEN_PORT"
  HTTPS_PORT=8449                    # unused; Caddy refuses http:// on its https_port
fi
DEFAULT_BIND=""
[ -n "$LISTEN_HOST" ] && DEFAULT_BIND="default_bind ${LISTEN_HOST}"

cat > /tmp/Caddyfile <<CADDY
{
	admin off
	persist_config off
	http_port ${HTTP_PORT}
	https_port ${HTTPS_PORT}
	${DEFAULT_BIND}
	auto_https disable_redirects
	servers {
		# The optional tunnel (compose profile "tunnel") is the only proxy in
		# front of us; client IP comes from Cloudflare's header via it.
		trusted_proxies static ${TUNNEL_IP:-10.213.58.10}/32
		client_ip_headers Cf-Connecting-Ip X-Forwarded-For
	}
}

# Local-only health endpoint for the container healthcheck (both modes).
http://127.0.0.1:${HEALTH_PORT} {
	bind 127.0.0.1
	respond /_proxy/health "ok" 200
}

${SITE} {
	${TLS}
	header {
		-Server
		X-Content-Type-Options nosniff
		Referrer-Policy no-referrer
		${HSTS}
	}
	log {
		output stdout
		format console
	}

	handle /_proxy/health {
		respond "ok" 200
	}
	handle /_gate/* {
		reverse_proxy ${GATE} {
			header_up X-Gate-Client-IP {client_ip}
		}
	}

	route {
		# Identity comes only from the gate, never from the client.
		request_header -X-Forwarded-User
		request_header -X-Gate-User
		forward_auth ${GATE} {
			uri /_gate/check
			header_up X-Gate-Client-IP {client_ip}
			copy_headers X-Gate-User>X-Forwarded-User
		}
		# Same-origin check is done HERE (the browser's Origin must equal the
		# address it is talking to), then the origin is normalised so the
		# gateway accepts it whatever public name we are reached by (local,
		# PUBLIC_HOSTNAME, or a random tunnel URL). Cross-site origins pass
		# through unchanged and the gateway rejects them.
		@sameorigin expression \`{http.request.header.Origin} == "https://" + {http.request.hostport} || {http.request.header.Origin} == "http://" + {http.request.hostport}\`
		request_header @sameorigin Origin "${UI_ORIGIN}"
		# The bot's desktop (noVNC + websocket), watch and take over.
		redir /desktop /desktop/ 308
		handle_path /desktop/* {
			reverse_proxy ${DESKTOP}
		}
		# Everything else: the agent's web chat / control UI.
		reverse_proxy ${GATEWAY}
	}
}
CADDY
caddy fmt --overwrite /tmp/Caddyfile >/dev/null 2>&1 || true
exec caddy run --config /tmp/Caddyfile --adapter caddyfile
