"""Access gate for the bot (stdlib only, no dependencies).

Caddy sends every request here first (forward_auth). A signed, HttpOnly
session cookie lets a request through. There are two ways to get that cookie,
chosen at start by GATE_SSO; only one is ever active.

GATE_SSO off (default; the self-hosted bot): one shared password from the
environment (ACCESS_PASSWORD). Failed logins are rate-limited per client
address and globally.
  GET  /_gate/check   -> 200 + X-Gate-User: owner when the cookie is valid,
                         else 302 to the login page (browsers) or 401.
  GET  /_gate/login   -> login form.
  POST /_gate/login   -> checks the password, sets the cookie, redirects.
  GET  /_gate/logout  -> clears the cookie.
  GET  /_gate/health  -> 200 (no information).

GATE_SSO=on (the hosted bot, shown in a frame on the page at GATE_LOGIN_URL):
there is no password login. The site that hosts the bot mints a one-time
token and the browser posts it here as a form, from that site's page.
  token   v1.<bot_id>.<exp>.<jti>.<mac>
          exp  Unix seconds; accepted while now < exp <= now + 120
          jti  32 lowercase hex characters; accepted once
          mac  base64url without padding of
               HMAC-SHA256(key, "v1.<bot_id>.<exp>.<jti>")
          key  SHA256(b"fugal-bot-gate-sso-v1\0" + ACCESS_PASSWORD)
  POST /_gate/sso     -> form fields token, next. Sets the cookie
                         __Host-pb_session and answers 303 to next (a path on
                         this host). The Origin header must be
                         GATE_PARENT_ORIGIN, or absent (not a browser).
  POST /_gate/logout  -> clears the cookie.
  POST /_gate/login   -> 403. The login form is not served.
  GET  /_gate/check   -> as above, but a browser without a session gets a
                         small page (401) instead of a redirect: inside a
                         frame it tells the page around it
                         ({type: "fugal-bot:auth-required"}, sent only to
                         GATE_PARENT_ORIGIN); as a top-level page it goes to
                         GATE_LOGIN_URL. Requests that carry the cookie but
                         were started by another origin are refused (403),
                         except plain navigations: see foreign_request().
  GET  /_gate/sso.js  -> the script of that page (no inline script).
"""

import base64
import hashlib
import hmac
import html
import json
import os
import re
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

# ---- single sign-on (GATE_SSO=on) ----------------------------------------------
_sso = os.environ.get("GATE_SSO", "").strip().lower()
if _sso not in ("", "off", "on"):
    # A typo must not quietly fall back to the password gate.
    sys.exit("[gate] GATE_SSO must be 'on' or 'off'; refusing to start")
SSO = _sso == "on"
BOT_ID = LOGIN_URL = PARENT_ORIGIN = ""
BOT_ID_RE = re.compile(r"b[a-z2-7]{10}\Z")
ORIGIN_RE = re.compile(r"https://[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?\Z")
if SSO:
    BOT_ID = os.environ.get("GATE_BOT_ID", "").strip()
    LOGIN_URL = os.environ.get("GATE_LOGIN_URL", "").strip()
    PARENT_ORIGIN = os.environ.get("GATE_PARENT_ORIGIN", "").strip().lower()
    # Nobody types this password, and a token seen on the wire could be used
    # to guess a short one offline.
    if len(PASSWORD) < 32:
        sys.exit("[gate] GATE_SSO=on: ACCESS_PASSWORD must be at least 32 random characters")
    if not BOT_ID_RE.match(BOT_ID):
        sys.exit("[gate] GATE_SSO=on: GATE_BOT_ID must be 'b' and 10 characters of a-z, 2-7")
    _u = urllib.parse.urlsplit(LOGIN_URL)
    if not re.match(r"https://[\x21-\x7e]+\Z", LOGIN_URL) or not ORIGIN_RE.match("https://" + _u.netloc.lower()):
        sys.exit("[gate] GATE_SSO=on: GATE_LOGIN_URL must be an https:// address (the page that shows the bot)")
    # The page that frames the bot and posts the token; by default the origin
    # of GATE_LOGIN_URL.
    PARENT_ORIGIN = PARENT_ORIGIN or "https://" + _u.netloc.lower()
    if PARENT_ORIGIN.endswith(":443"):
        PARENT_ORIGIN = PARENT_ORIGIN[:-4]   # browsers leave the default port out
    if not ORIGIN_RE.match(PARENT_ORIGIN):
        sys.exit("[gate] GATE_SSO=on: GATE_PARENT_ORIGIN must be an origin like https://example.com")

# With SSO the bot lives under the same registrable domain as other bots and
# the site itself. The __Host- prefix makes browsers accept the cookie only
# from this exact host, over HTTPS, with Path=/ and no Domain: a sibling host
# can neither receive it nor plant one of its own.
COOKIE = "__Host-pb_session" if SSO else "pb_session"
SESSION_SECONDS = int(os.environ.get("SESSION_HOURS", "168")) * 3600
# Derived from the password: changing the password logs every session out.
KEY = hashlib.sha256(b"fugal-bot-gate-v1\0" + PASSWORD.encode()).digest()
SSO_KEY = hashlib.sha256(b"fugal-bot-gate-sso-v1\0" + PASSWORD.encode()).digest()
SSO_TOKEN_RE = re.compile(r"(v1\.(b[a-z2-7]{10})\.([0-9]{1,12})\.([0-9a-f]{32}))\.([A-Za-z0-9_-]{43})\Z")
SSO_TOKEN_LIFE = 60      # what the minting side uses
SSO_MAX_AHEAD = 120      # the furthest expiry accepted (life + clock difference)
START = time.time()
_used: dict[str, int] = {}   # jti -> exp, of tokens already accepted

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


def log(line):
    sys.stderr.write("[gate] %s\n" % line)


def sso_check(token, now=None):
    """Returns "" when the token is good (and marks it used), else the reason
    for the log. The caller tells the client nothing about which check failed."""
    now = time.time() if now is None else now
    m = SSO_TOKEN_RE.match(token)
    if not m:
        return "malformed"
    signed, bot_id, exp_s, jti, mac = m.groups()
    good = base64.urlsafe_b64encode(hmac.new(SSO_KEY, signed.encode("ascii"), hashlib.sha256).digest()).rstrip(b"=")
    if not hmac.compare_digest(mac.encode("ascii"), good):
        return "bad mac"
    if not hmac.compare_digest(bot_id, BOT_ID):
        return "other bot"
    exp = int(exp_s)
    if exp <= now:
        return "expired"
    if exp > now + SSO_MAX_AHEAD:
        return "expires too far ahead"
    # The used-token list is in memory, so a restart forgets it. A token made
    # before this process started could have been used with the previous one:
    # refuse those (they expire within SSO_TOKEN_LIFE of the start).
    if exp < int(START) + SSO_TOKEN_LIFE:
        return "made before this gate started"
    with _lock:
        for k in [k for k, e in _used.items() if e <= now]:
            del _used[k]
        if jti in _used:
            return "used before"
        if len(_used) >= 10000:   # bound memory; never forget a live token
            return "too many sign-ins at once"
        _used[jti] = exp
    return ""


def safe_next(n):
    # Only a path on this host. Browsers drop tabs and newlines from an
    # address and read a backslash as "/", so "/<tab>/host" would leave the
    # site: only visible ASCII, no backslash, never "//" in front.
    if not n or len(n) > 2048 or not re.match(r"/(?!/)[\x21-\x7e]*\Z", n) or "\\" in n:
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
_style_hash = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()
CSP = f"default-src 'none'; style-src 'sha256-{_style_hash}'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"

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


# ---- pages shown instead of the login form when GATE_SSO=on --------------------
# No inline script: the one script is a file of this service (/_gate/sso.js),
# so the policy needs neither a hash nor a nonce for it. Inside a frame the
# script tells the page around the bot what happened and that page signs the
# frame in again; the message goes to GATE_PARENT_ORIGIN and nowhere else. As
# a top-level page it goes to GATE_LOGIN_URL. Nothing here redirects a frame.
SSO_CSP = (f"default-src 'none'; script-src 'self'; style-src 'sha256-{_style_hash}'; form-action 'none'; "
           f"frame-ancestors 'self' {PARENT_ORIGIN}; base-uri 'none'")
SSO_JS = """(function () {
  "use strict";
  var state = document.documentElement.getAttribute("data-gate");
  if (state !== "required" && state !== "failed") return;
  if (window.parent !== window) {
    try { window.parent.postMessage({ type: "fugal-bot:auth-" + state }, %s); } catch (e) { /* stay as shown */ }
    return;
  }
  if (state === "required") window.location.replace(%s);
})();
""" % (json.dumps(PARENT_ORIGIN), json.dumps(LOGIN_URL))


def _sso_page(state, lead, more):
    script = '<script src="/_gate/sso.js" defer></script>' if state else ""
    return (
        f'<!doctype html><html lang="en" data-gate="{state}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Fugal Bot</title><style>{STYLE}</style>{script}</head><body><main>"
        f'<h1>Fugal Bot</h1><p class="lead">{lead}</p><p class="meta">{more}</p></main></body></html>'
    ).encode()


_login_link = '<a href="{0}" target="_top" rel="noopener noreferrer">{0}</a>'.format(html.escape(LOGIN_URL, quote=True))
SSO_PAGES = {
    "required": _sso_page("required", "Signing you in\u2026", "If nothing happens, open " + _login_link + "."),
    "failed": _sso_page("failed", "This sign-in did not work.",
                        "It may have expired or been used already. Open the bot again from " + _login_link + "."),
    "out": _sso_page("", "Signed out.", "Open the bot again from " + _login_link + "."),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "gate"
    sys_version = ""

    def log_message(self, fmt, *args):  # never log bodies or cookies
        log("%s %s" % (self.client_ip(), fmt % args))

    def log_request(self, code="-", size="-"):
        # Without the query string: a token sent there by mistake stays out.
        line = (self.requestline or "").split("?", 1)[0]
        self.log_message('"%s" %s', line, str(code))

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

    # ---- GATE_SSO=on ----------------------------------------------------------
    def sso_page(self, code, name, headers=()):
        self.send(code, SSO_PAGES[name], [("Content-Type", "text/html; charset=utf-8"),
                                          ("Content-Security-Policy", SSO_CSP), *headers])

    def read_form(self):
        # Always read the body (bounded) before answering, also when refusing.
        try:
            n = max(0, min(int(self.headers.get("Content-Length", "0") or 0), 8192))
        except ValueError:
            n = 0
        return urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))

    def own_origin(self, origin):
        host = self.headers.get("Host", "")
        return bool(host) and origin in ("https://" + host, "http://" + host)

    def foreign_request(self):
        """True when a request that carries the session cookie was started by
        another origin and is not a plain navigation.

        SameSite does not help against the site that frames the bot or against
        other bots: they share this host's registrable domain, so the browser
        sends them the cookie. Their pages may navigate to the bot (that is
        how the frame is opened); they may not call its API, post to it or
        open its websockets. Sec-Fetch-Site is set by the browser and cannot
        be forged by a page; browsers without it are judged by Origin."""
        h = self.headers
        websocket = h.get("Upgrade") or h.get("Sec-WebSocket-Key")
        safe = h.get("X-Forwarded-Method", "GET").upper() in ("GET", "HEAD") and not websocket
        site = h.get("Sec-Fetch-Site", "").strip().lower()
        if site in ("same-origin", "none"):
            return False
        if site:
            return not (safe and h.get("Sec-Fetch-Mode", "").strip().lower() == "navigate")
        origin = h.get("Origin")
        return origin is not None and not safe and not self.own_origin(origin)

    def cookie_header(self, value, max_age):
        # Strict: the page that frames the bot is same-site with it (both under
        # one registrable domain), so the cookie is still sent inside the
        # frame, and no other site's request ever carries it.
        return f"{COOKIE}={value}; Path=/; Max-Age={max_age}; Secure; HttpOnly; SameSite=Strict"

    def sso(self):
        data = self.read_form()
        ip = self.client_ip()
        origin = self.headers.get("Origin")
        # A browser always names the origin of a cross-origin POST, so a
        # missing header means a client that is not a browser: nobody's
        # browser is being tricked, and the token decides. "null" (a sandboxed
        # or referrer-less page) is refused: any site can produce it.
        if origin is not None and origin != PARENT_ORIGIN:
            log("%s sso refused: wrong origin" % ip)
            return self.sso_page(403, "failed")
        why = sso_check(data.get("token", [""])[0])
        if why:
            # The limit keeps noise down; it cannot lock the owner out, because
            # a good token is accepted above whatever the count (guessing a
            # 256-bit MAC is not helped by more attempts).
            if limited(ip):
                return self.sso_page(429, "failed")
            record_fail(ip)
            log("%s sso refused: %s" % (ip, why))
            return self.sso_page(403, "failed")
        exp = int(time.time()) + SESSION_SECONDS
        nxt = safe_next(data.get("next", ["/"])[0])
        return self.send(303, headers=[("Set-Cookie", self.cookie_header(sign(exp), SESSION_SECONDS)),
                                       ("Location", nxt)])

    def sso_logout(self):
        self.read_form()
        origin = self.headers.get("Origin")
        if origin is not None and origin != PARENT_ORIGIN and not self.own_origin(origin):
            return self.send(403, b"wrong origin\n", [("Content-Type", "text/plain")])
        return self.sso_page(200, "out", [("Set-Cookie", self.cookie_header("", 0))])

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/_gate/health":
            return self.send(200, b"ok")
        if url.path == "/_gate/check":
            if self.cookie_ok():
                if SSO and self.foreign_request():
                    return self.send(403, b"cross-origin request refused\n", [("Content-Type", "text/plain")])
                return self.send(200, headers=[("X-Gate-User", "owner")])
            orig = self.headers.get("X-Forwarded-Uri", "/")
            accept = self.headers.get("Accept", "")
            upgrade = self.headers.get("Upgrade", "")
            if "text/html" in accept and not upgrade:
                if SSO:
                    return self.sso_page(401, "required")
                loc = "/_gate/login?next=" + urllib.parse.quote(safe_next(orig), safe="")
                return self.send(302, headers=[("Location", loc)])
            return self.send(401, b"login required", [("Content-Type", "text/plain")])
        if SSO:
            if url.path == "/_gate/sso.js":
                return self.send(200, SSO_JS.encode(), [("Content-Type", "text/javascript; charset=utf-8")])
            if url.path == "/_gate/login":
                return self.sso_page(401, "required")
            if url.path in ("/_gate/sso", "/_gate/logout"):
                return self.send(405, b"POST only\n", [("Content-Type", "text/plain"), ("Allow", "POST")])
            return self.send(404, b"not found")
        if url.path == "/_gate/login":
            q = urllib.parse.parse_qs(url.query)
            return self.form(nxt=q.get("next", ["/"])[0])
        if url.path == "/_gate/logout":
            c = f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
            return self.send(302, headers=[("Set-Cookie", c), ("Location", "/_gate/login")])
        return self.send(404, b"not found")

    do_HEAD = do_GET

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if SSO:
            if path == "/_gate/sso":
                return self.sso()
            if path == "/_gate/logout":
                return self.sso_logout()
            self.read_form()
            if path == "/_gate/login":
                return self.send(403, b"Password sign-in is switched off for this bot. Open it from the site that hosts it.\n",
                                 [("Content-Type", "text/plain; charset=utf-8")])
            return self.send(404, b"not found")
        if path != "/_gate/login":
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
