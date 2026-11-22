"""Password gate for the bot (stdlib only, no dependencies).

Caddy sends every request here first (forward_auth). One shared password from
the environment (ACCESS_PASSWORD) unlocks a signed, HttpOnly session cookie.
Failed logins are rate-limited per client address and globally.

Endpoints (all under /_gate/):
  GET  /_gate/check   -> 200 + X-Gate-User: owner when the cookie is valid,
                         else 302 to the login page (browsers) or 401.
  GET  /_gate/login   -> login form.
  POST /_gate/login   -> checks the password, sets the cookie, redirects.
  GET  /_gate/logout  -> clears the cookie.
  GET  /_gate/health  -> 200 (no information).
"""

import hashlib
import hmac
import html
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PASSWORD = os.environ.get("ACCESS_PASSWORD", "")
BIND = os.environ.get("BIND_ADDRESS", "127.0.0.1").strip()
LOOPBACK = BIND in ("127.0.0.1", "localhost", "::1", "[::1]")
if not PASSWORD:
    sys.exit("[gate] ACCESS_PASSWORD is required; refusing to start")
if not LOOPBACK and len(PASSWORD) < 12:
    sys.exit("[gate] BIND_ADDRESS is not loopback: ACCESS_PASSWORD must be at least 12 characters")

COOKIE = "pb_session"
SESSION_SECONDS = int(os.environ.get("SESSION_HOURS", "168")) * 3600
# Derived from the password: changing the password logs every session out.
KEY = hashlib.sha256(b"fugal-bot-gate-v1\0" + PASSWORD.encode()).digest()

# Rate limits on failed logins.
PER_IP_FAILS, PER_IP_WINDOW = 5, 15 * 60
GLOBAL_FAILS, GLOBAL_WINDOW = 50, 15 * 60
_fails: dict[str, list[float]] = {}
_global: list[float] = []
_lock = threading.Lock()


def _prune(xs, window, now):
    while xs and xs[0] < now - window:
        xs.pop(0)


def limited(ip):
    now = time.time()
    with _lock:
        xs = _fails.setdefault(ip, [])
        _prune(xs, PER_IP_WINDOW, now)
        _prune(_global, GLOBAL_WINDOW, now)
        return len(xs) >= PER_IP_FAILS or len(_global) >= GLOBAL_FAILS


def record_fail(ip):
    now = time.time()
    with _lock:
        _fails.setdefault(ip, []).append(now)
        _global.append(now)
        if len(_fails) > 10000:  # bound memory
            _fails.clear()


def sign(exp):
    mac = hmac.new(KEY, str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{mac}"


def valid(token):
    try:
        exp_s, mac = token.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    good = hmac.new(KEY, exp_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, good) and exp > time.time()


def safe_next(n):
    # Only same-site relative paths.
    if not n or not n.startswith("/") or n.startswith("//") or "\\" in n:
        return "/"
    return n


# ---- login page (presentation only) ------------------------------------------
# Optional lines, shown only when set; every value is HTML-escaped. Nothing
# about the configuration (model, endpoint, ...) is shown unless set here.
def _opt(name):
    return html.escape(os.environ.get(name, "").strip(), quote=True)


HOST_LABEL, MODEL_LABEL, REPO_URL = _opt("HOST_LABEL"), _opt("MODEL_LABEL"), _opt("REPO_URL")
if REPO_URL and not REPO_URL.lower().startswith(("https://", "http://")):
    REPO_URL = ""

STYLE = """
:root{color-scheme:light dark;--bg:#f4f5f8;--card:#fff;--fg:#1d2330;--muted:#5b6475;--line:#d5d9e2;--accent:#3d63dd;--err:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#10141c;--card:#1b2130;--fg:#e3e7ef;--muted:#98a2b3;--line:#3a4356;--accent:#6f8ff0;--err:#ff8a80}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:1.25rem;
font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--fg);line-height:1.45}
main{width:100%;max-width:24rem;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:1.75rem}
h1{font-size:1.5rem;margin:0 0 .35rem}
p{margin:.25rem 0}
.lead{color:var(--muted);margin-bottom:1rem}
.meta{font-size:.875rem;color:var(--muted)}
.meta a{color:var(--accent)}
label{display:block;font-size:.875rem;margin-top:1rem}
input{width:100%;font:inherit;padding:.7rem;margin:.35rem 0 .9rem;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
button{width:100%;font:inherit;padding:.7rem;border-radius:8px;border:0;background:var(--accent);color:#fff;font-weight:600;cursor:pointer}
.err{color:var(--err);font-weight:600}
"""
CSP = "default-src 'none'; style-src 'sha256-{}'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'".format(
    __import__("base64").b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()
)

_meta = []
if HOST_LABEL:
    _meta.append(f'<p class="meta">Running on: {HOST_LABEL}</p>')
if MODEL_LABEL:
    _meta.append(f'<p class="meta">Model: {MODEL_LABEL}</p>')
if REPO_URL:
    _meta.append(f'<p class="meta"><a href="{REPO_URL}" rel="noopener noreferrer">Source code</a></p>')
META = "\n".join(_meta)

PAGE = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    "<title>Fugal Bot</title><style>" + STYLE.replace("{", "{{").replace("}", "}}") + "</style></head><body><main>"
    "<h1>Fugal Bot</h1>"
    '<p class="lead">Your own AI assistant with its own computer: a browser and a shell you can watch.</p>'
    + META.replace("{", "{{").replace("}", "}}") +
    '<form method="post" action="/_gate/login">{msg}'
    '<label for="pw">Access password</label>'
    '<input id="pw" type="password" name="password" autocomplete="current-password" autofocus required>'
    '<input type="hidden" name="next" value="{next}">'
    '<button type="submit">Open</button></form></main></body></html>'
)


class Handler(BaseHTTPRequestHandler):
    server_version = "gate"
    sys_version = ""

    def log_message(self, fmt, *args):  # never log bodies or cookies
        sys.stderr.write("[gate] %s %s\n" % (self.client_ip(), fmt % args))

    def client_ip(self):
        # Set by Caddy from its own view of the client ({client_ip}: the TCP
        # peer, or Cloudflare's Cf-Connecting-Ip when it came via the tunnel).
        # Only Caddy can reach this service, so the header is trustworthy.
        return self.headers.get("X-Gate-Client-IP", "").strip() or self.client_address[0]

    def cookie_ok(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and valid(v):
                return True
        return False

    def secure(self):
        return self.headers.get("X-Forwarded-Proto", "") == "https"

    def send(self, code, body=b"", headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def form(self, code=200, msg="", nxt="/"):
        body = PAGE.format(msg=msg, next=html.escape(safe_next(nxt), quote=True)).encode()
        self.send(code, body, [("Content-Type", "text/html; charset=utf-8"),
                               ("Content-Security-Policy", CSP),
                               ("X-Frame-Options", "DENY")])

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/_gate/health":
            return self.send(200, b"ok")
        if url.path == "/_gate/check":
            if self.cookie_ok():
                return self.send(200, headers=[("X-Gate-User", "owner")])
            orig = self.headers.get("X-Forwarded-Uri", "/")
            accept = self.headers.get("Accept", "")
            upgrade = self.headers.get("Upgrade", "")
            if "text/html" in accept and not upgrade:
                loc = "/_gate/login?next=" + urllib.parse.quote(safe_next(orig), safe="")
                return self.send(302, headers=[("Location", loc)])
            return self.send(401, b"login required", [("Content-Type", "text/plain")])
        if url.path == "/_gate/login":
            q = urllib.parse.parse_qs(url.query)
            return self.form(nxt=q.get("next", ["/"])[0])
        if url.path == "/_gate/logout":
            c = f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
            return self.send(302, headers=[("Set-Cookie", c), ("Location", "/_gate/login")])
        return self.send(404, b"not found")

    do_HEAD = do_GET

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/_gate/login":
            return self.send(404, b"not found")
        ip = self.client_ip()
        if limited(ip):
            return self.form(429, '<p class="err">Too many attempts. Try again in 15 minutes.</p>')
        n = min(int(self.headers.get("Content-Length", "0") or 0), 4096)
        data = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        given = data.get("password", [""])[0]
        nxt = safe_next(data.get("next", ["/"])[0])
        ok = hmac.compare_digest(
            hashlib.sha256(given.encode()).digest(), hashlib.sha256(PASSWORD.encode()).digest()
        )
        if not ok:
            record_fail(ip)
            time.sleep(0.5)
            return self.form(401, '<p class="err">Wrong password.</p>', nxt)
        exp = int(time.time()) + SESSION_SECONDS
        c = f"{COOKIE}={sign(exp)}; Path=/; Max-Age={SESSION_SECONDS}; HttpOnly; SameSite=Lax"
        if self.secure():
            c += "; Secure"
        return self.send(303, headers=[("Set-Cookie", c), ("Location", nxt)])


if __name__ == "__main__":
    bind = os.environ.get("GATE_LISTEN_HOST", "0.0.0.0")
    port = int(os.environ.get("GATE_LISTEN_PORT", "9000"))
    srv = ThreadingHTTPServer((bind, port), Handler)
    srv.daemon_threads = True
    print(f"[gate] listening on {bind}:{port}", flush=True)
    srv.serve_forever()
