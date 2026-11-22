#!/bin/sh
# Writes the Caddyfile for one of two modes, then runs Caddy (as non-root).
#   PUBLIC_HOSTNAME set   -> HTTPS with an automatic Let's Encrypt certificate
#                            (TLS-ALPN challenge on the one published port, 443).
#   PUBLIC_HOSTNAME empty -> plain HTTP (meant for 127.0.0.1 only).
set -eu
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
else
  SITE="http://:8443"
  TLS=""
  HSTS=""
fi

cat > /tmp/Caddyfile <<CADDY
{
	admin off
	persist_config off
	http_port 8080
	https_port 8443
	auto_https disable_redirects
}

# Local-only health endpoint for the container healthcheck (both modes).
http://127.0.0.1:8081 {
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
		reverse_proxy gate:9000
	}

	route {
		# Identity comes only from the gate, never from the client.
		request_header -X-Forwarded-User
		request_header -X-Gate-User
		forward_auth gate:9000 {
			uri /_gate/check
			copy_headers X-Gate-User>X-Forwarded-User
		}
		# The bot's desktop (noVNC + websocket), watch and take over.
		redir /desktop /desktop/ 308
		handle_path /desktop/* {
			reverse_proxy bot:6080
		}
		# Everything else: the agent's web chat / control UI.
		reverse_proxy bot:18789
	}
}
CADDY
caddy fmt --overwrite /tmp/Caddyfile >/dev/null 2>&1 || true
exec caddy run --config /tmp/Caddyfile --adapter caddyfile
