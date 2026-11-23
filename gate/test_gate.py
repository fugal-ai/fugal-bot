"""Unit tests for the gate (stdlib only, 127.0.0.1 only).

    python3 -I gate/test_gate.py

The gate reads its settings when it is loaded, so the program is loaded twice
here: once with GATE_SSO=on and once without. One test verifies a token made
by the controller's own minting function, so the two sides cannot drift: it
looks for the controller's repository next to this one (or at
FUGAL_BOT_CONTROLLER) and is skipped when it is not there.
"""

import base64
import hashlib
import hmac
import http.client
import importlib.util
import os
import secrets
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer

sys.dont_write_bytecode = True   # no __pycache__ beside the program
HERE = os.path.dirname(os.path.abspath(__file__))
GATE_PY = os.path.join(HERE, "gate.py")

PASSWORD = secrets.token_urlsafe(32)      # throwaway, as the controller makes them
BOT_ID = "bk4zq2m7xn3"
OTHER_BOT = "baaaaaaaaaa"
PARENT = "https://site.example"
LOGIN_URL = "https://site.example/bot"
SSO_ENV = {"ACCESS_PASSWORD": PASSWORD, "GATE_SSO": "on", "GATE_BOT_ID": BOT_ID,
           "GATE_LOGIN_URL": LOGIN_URL, "GATE_PARENT_ORIGIN": PARENT, "SESSION_HOURS": "24"}
PW_ENV = {"ACCESS_PASSWORD": "a-password-for-the-test"}
GATE_VARS = ("ACCESS_PASSWORD", "BIND_ADDRESS", "SESSION_HOURS", "GATE_SSO", "GATE_BOT_ID", "GATE_LOGIN_URL",
             "GATE_PARENT_ORIGIN", "HOST_LABEL", "MODEL_LABEL", "REPO_URL")


def load_gate(name, env):
    saved = {k: os.environ.pop(k, None) for k in GATE_VARS}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(name, GATE_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k in GATE_VARS:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]
    mod.LOGS = []
    mod.log = mod.LOGS.append
    return mod


def mint(bot_id=BOT_ID, password=PASSWORD, ttl=60, now=None, jti=None):
    """The token format, written out again here on purpose (not the gate's code)."""
    exp = int(time.time() if now is None else now) + ttl
    signed = f"v1.{bot_id}.{exp}.{jti or secrets.token_hex(16)}"
    key = hashlib.sha256(b"fugal-bot-gate-sso-v1\0" + password.encode()).digest()
    mac = hmac.new(key, signed.encode("ascii"), hashlib.sha256).digest()
    return signed + "." + base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


class GateCase(unittest.TestCase):
    ENV = {}
    NAME = "gate"

    @classmethod
    def setUpClass(cls):
        cls.gate = load_gate(cls.NAME, cls.ENV)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), cls.gate.Handler)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        with self.gate._lock:
            self.gate._fails.clear()
            self.gate._global.clear()
        self.gate.LOGS.clear()

    def req(self, method, path, form=None, headers=None, ip="198.51.100.1"):
        h = {"X-Gate-Client-IP": ip, "Host": "bot.example"}
        h.update(headers or {})
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        try:
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read().decode("utf-8", "replace")
        finally:
            c.close()

    def check(self, cookie=None, headers=None):
        h = dict(headers or {})
        if cookie:
            h["Cookie"] = cookie
        return self.req("GET", "/_gate/check", headers=h)


class SsoOn(GateCase):
    ENV = SSO_ENV
    NAME = "gate_sso"

    def sso(self, token, nxt="/", origin=PARENT, ip="198.51.100.1"):
        return self.req("POST", "/_gate/sso", {"token": token, "next": nxt},
                        {"Origin": origin} if origin is not None else {}, ip=ip)

    def sign_in(self):
        code, h, _ = self.sso(mint())
        self.assertEqual(code, 303)
        return h["set-cookie"].split(";")[0]

    # ---- the token ---------------------------------------------------------------
    def test_valid_token_opens_a_session(self):
        code, h, _ = self.sso(mint())
        self.assertEqual(code, 303)
        self.assertEqual(h["location"], "/")
        cookie = h["set-cookie"].split(";")[0]
        code, h, _ = self.check(cookie)
        self.assertEqual((code, h.get("x-gate-user")), (200, "owner"))

    def test_cookie_attributes(self):
        _, h, _ = self.sso(mint())
        name, _, value = h["set-cookie"].split(";")[0].partition("=")
        attrs = [a.strip() for a in h["set-cookie"].split(";")[1:]]
        self.assertEqual(name, "__Host-pb_session")
        self.assertTrue(value)
        # What the __Host- prefix demands (Secure, Path=/, no Domain), plus
        # HttpOnly, Strict and the configured lifetime.
        self.assertEqual(sorted(attrs), sorted(["Path=/", "Max-Age=86400", "Secure", "HttpOnly", "SameSite=Strict"]))
        self.assertNotIn("domain", h["set-cookie"].lower())
        self.assertEqual(h["cache-control"], "no-store")

    def test_token_works_once(self):
        t = mint()
        self.assertEqual(self.sso(t)[0], 303)
        code, h, _ = self.sso(t)
        self.assertEqual(code, 403)
        self.assertNotIn("set-cookie", h)
        self.assertEqual(self.sso(t, origin=None)[0], 403)

    def test_expired_token(self):
        for ttl in (-1, 0, -3600):
            code, h, _ = self.sso(mint(ttl=ttl))
            self.assertEqual(code, 403, ttl)
            self.assertNotIn("set-cookie", h)

    def test_token_dated_too_far_ahead(self):
        self.assertEqual(self.sso(mint(ttl=125))[0], 403)
        self.assertEqual(self.sso(mint(ttl=86400))[0], 403)
        self.assertEqual(self.sso(mint(ttl=115))[0], 303)   # inside the 120 s window

    def test_token_for_another_bot(self):
        code, h, _ = self.sso(mint(bot_id=OTHER_BOT))       # same key, other id
        self.assertEqual(code, 403)
        self.assertNotIn("set-cookie", h)
        self.assertEqual(self.sso(mint(bot_id=OTHER_BOT, password=secrets.token_urlsafe(32)))[0], 403)

    def test_bad_mac(self):
        t = mint()
        head, mac = t.rsplit(".", 1)
        flipped = head + "." + ("A" if mac[0] != "A" else "B") + mac[1:]
        self.assertEqual(self.sso(flipped)[0], 403)
        self.assertEqual(self.sso(mint(password=secrets.token_urlsafe(32)))[0], 403)
        self.assertEqual(self.sso(mint(password=""))[0], 403)
        # A changed field under the old mac.
        v, bot, exp, jti, mac = t.split(".")
        self.assertEqual(self.sso(".".join([v, bot, str(int(exp) + 1), jti, mac]))[0], 403)
        self.assertEqual(self.sso(".".join([v, bot, exp, secrets.token_hex(16), mac]))[0], 403)
        self.assertEqual(self.sso(t)[0], 303)               # the untouched one still works

    def test_malformed_tokens(self):
        good = mint()
        v, bot, exp, jti, mac = good.split(".")
        bad = ["", "v1", "x", good + ".x", good + "=", " " + good, good + "\n", good.replace("v1.", "v2.", 1),
               ".".join([v, bot, exp, jti]), ".".join([v, bot, exp, jti.upper(), mac]),
               ".".join([v, bot, exp, jti[:-1], mac]), ".".join([v, bot, "1e9", jti, mac]),
               ".".join([v, bot, "-" + exp, jti, mac]), ".".join([v, "B" + bot[1:], exp, jti, mac]),
               ".".join([v, bot, exp, jti, mac[:-1]]), ".".join([v, bot, exp, jti, mac + "A"]),
               "v1.." + exp + "." + jti + "." + mac, good.replace(".", ":")]
        for t in bad:
            code, h, _ = self.sso(t, ip="198.51.100." + str(10 + len(t) % 200))
            self.assertEqual(code, 403, repr(t))
            self.assertNotIn("set-cookie", h)
        code, _, _ = self.req("POST", "/_gate/sso", {"next": "/"}, {"Origin": PARENT})   # no token field
        self.assertEqual(code, 403)
        self.assertEqual(self.sso(good)[0], 303)

    def test_token_made_before_the_gate_started_is_refused(self):
        # A restart forgets which tokens were used; tokens that could have been
        # used with the previous process are refused instead.
        real = self.gate.START
        try:
            t = mint()
            self.gate.START = time.time() + 5          # as if the gate started after it was made
            self.assertEqual(self.sso(t)[0], 403)
            self.gate.START = real
            self.assertEqual(self.sso(t)[0], 303)      # refusing it did not use it up
        finally:
            self.gate.START = real

    def test_token_minted_by_the_controller(self):
        root = os.environ.get("FUGAL_BOT_CONTROLLER") or os.path.join(HERE, "..", "..", "fugal-bot-controller")
        path = os.path.join(root, "keyholder", "tokens.py")
        if not os.path.isfile(path):
            self.skipTest("the controller's repository is not next to this one (set FUGAL_BOT_CONTROLLER)")
        spec = importlib.util.spec_from_file_location("controller_tokens", path)
        tokens = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tokens)
        token, exp = tokens.mint_sso_token(BOT_ID, PASSWORD)
        self.assertEqual(exp - int(time.time()), self.gate.SSO_TOKEN_LIFE)
        self.assertEqual(self.sso(token)[0], 303)
        self.assertEqual(self.sso(token)[0], 403)
        self.assertEqual(self.sso(tokens.mint_sso_token(OTHER_BOT, PASSWORD)[0])[0], 403)
        self.assertEqual(self.sso(tokens.mint_sso_token(BOT_ID, tokens.new_access_password())[0])[0], 403)
        self.assertTrue(self.gate.BOT_ID_RE.match(tokens.new_bot_id()))
        self.assertGreaterEqual(len(tokens.new_access_password()), 32)   # the gate's minimum with SSO

    # ---- who may post the token ----------------------------------------------------
    def test_origin_of_the_post(self):
        self.assertEqual(self.sso(mint(), origin=PARENT)[0], 303)
        self.assertEqual(self.sso(mint(), origin=None)[0], 303)       # not a browser
        t = mint()
        for origin in ("null", "https://evil.example", "https://bot.example", PARENT + ".evil.example",
                       "http://site.example", PARENT + "/", "https://sub.site.example", ""):
            code, h, _ = self.sso(t, origin=origin)
            self.assertEqual(code, 403, origin)
            self.assertNotIn("set-cookie", h)
        self.assertEqual(self.sso(t)[0], 303)                         # the refusals did not use it up

    def test_password_login_is_refused(self):
        code, h, body = self.req("POST", "/_gate/login", {"password": PASSWORD, "next": "/"})
        self.assertEqual(code, 403)
        self.assertNotIn("set-cookie", h)
        self.assertIn("switched off", body)
        code, h, body = self.req("GET", "/_gate/login")
        self.assertEqual(code, 401)
        self.assertNotIn('name="password"', body)
        self.assertNotIn("<form", body)

    def test_a_password_mode_cookie_is_not_a_session(self):
        value = self.gate.sign(int(time.time()) + 3600)
        self.assertEqual(self.check("__Host-pb_session=" + value)[0], 200)   # control
        self.assertEqual(self.check("pb_session=" + value)[0], 401)
        self.assertEqual(self.check("__Host-pb_session=" + self.gate.sign(int(time.time()) - 1))[0], 401)
        self.assertEqual(self.check("__Host-pb_session=0.abc")[0], 401)

    # ---- where it sends the browser afterwards ---------------------------------------
    def test_next_is_a_path_on_this_host_only(self):
        for nxt in ("/", "/desktop/", "/chat?session=a%20b&x=1", "/a/b.html#frag"):
            self.assertEqual(self.sso(mint(), nxt)[1]["location"], nxt, nxt)
        for nxt in ("", "https://evil.example/", "http://evil.example", "//evil.example", "///evil.example",
                    "/\\evil.example", "\\\\evil.example", "/\t/evil.example", "/\n/evil.example", "/ /evil.example",
                    "/\r\nSet-Cookie: x=1", "javascript:alert(1)", "evil.example", "desktop/", "/a\\b",
                    "/∕∕evil.example", "/x" * 2000):
            code, h, _ = self.sso(mint(), nxt)
            self.assertEqual((code, h["location"]), (303, "/"), repr(nxt))
            self.assertEqual(len([k for k in h if k == "set-cookie"]), 1)

    # ---- without a session -----------------------------------------------------------
    def test_browser_without_a_session_gets_the_signing_in_page(self):
        code, h, body = self.check(headers={"Accept": "text/html,application/xhtml+xml", "X-Forwarded-Uri": "/chat"})
        self.assertEqual(code, 401)
        self.assertNotIn("location", h)                 # no redirect, framed or not
        self.assertNotIn("x-gate-user", h)
        self.assertIn("Signing you in", body)
        self.assertIn('data-gate="required"', body)
        self.assertIn('<script src="/_gate/sso.js" defer></script>', body)
        self.assertEqual(body.count("<script"), 1)      # no inline script
        self.assertNotIn("<form", body)
        csp = h["content-security-policy"]
        self.assertIn(f"frame-ancestors 'self' {PARENT};", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'self';", csp)
        self.assertNotIn("unsafe", csp)
        self.assertNotIn("x-frame-options", h)
        style = body.split("<style>")[1].split("</style>")[0]
        self.assertIn("'sha256-" + base64.b64encode(hashlib.sha256(style.encode()).digest()).decode() + "'", csp)

    def test_the_page_script(self):
        code, h, js = self.req("GET", "/_gate/sso.js")
        self.assertEqual(code, 200)
        self.assertTrue(h["content-type"].startswith("text/javascript"))
        self.assertIn('window.parent.postMessage({ type: "fugal-bot:auth-" + state }, "https://site.example")', js)
        self.assertIn('window.location.replace("https://site.example/bot")', js)
        self.assertNotIn('"*"', js)                     # never to any origin
        self.assertNotIn(PASSWORD, js)

    def test_other_requests_without_a_session_get_401(self):
        code, h, body = self.check(headers={"Accept": "application/json"})
        self.assertEqual((code, body), (401, "login required"))
        self.assertNotIn("x-gate-user", h)
        self.assertEqual(self.check()[0], 401)
        code, _, body = self.check(headers={"Accept": "text/html", "Upgrade": "websocket"})
        self.assertEqual((code, body), (401, "login required"))

    # ---- requests started by other origins, with the cookie -----------------------------
    def test_requests_from_other_origins_are_refused(self):
        c = self.sign_in()
        own, sibling = "https://bot.example", "https://b-other.example"
        ok = [
            {},                                                                  # not a browser
            {"X-Forwarded-Method": "POST"},
            {"X-Forwarded-Method": "POST", "Origin": own, "Sec-Fetch-Site": "same-origin"},
            {"X-Forwarded-Method": "POST", "Origin": own},                       # browser without Sec-Fetch-*
            {"X-Forwarded-Method": "POST", "Origin": "null", "Sec-Fetch-Site": "same-origin"},
            {"Upgrade": "websocket", "Origin": own},
            {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"},            # typed address, bookmark
            {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate"},       # the frame being opened
            {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate", "X-Forwarded-Method": "HEAD"},
            {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "X-Forwarded-Method": "DELETE"},
        ]
        refused = [
            {"X-Forwarded-Method": "POST", "Origin": sibling},
            {"X-Forwarded-Method": "POST", "Origin": PARENT},
            {"X-Forwarded-Method": "POST", "Origin": "null"},
            {"X-Forwarded-Method": "PUT", "Origin": sibling},
            {"X-Forwarded-Method": "DELETE", "Origin": "https://evil.example"},
            {"Upgrade": "websocket", "Origin": sibling},
            {"Sec-WebSocket-Key": "c21va2UtdGVzdC1rZXkxMg==", "Origin": PARENT},
            {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "no-cors"},        # <img>, <script>, fetch
            {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "cors", "Origin": sibling},
            {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"},
            {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate", "X-Forwarded-Method": "POST"},
            {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "websocket", "Upgrade": "websocket", "Origin": sibling},
            {"Sec-Fetch-Site": "same-site", "Origin": own, "X-Forwarded-Method": "POST"},   # the browser's word wins
        ]
        for h in ok:
            code, rh, _ = self.check(c, h)
            self.assertEqual((code, rh.get("x-gate-user")), (200, "owner"), h)
        for h in refused:
            code, rh, _ = self.check(c, h)
            self.assertEqual(code, 403, h)
            self.assertNotIn("x-gate-user", rh)

    # ---- sign-out ------------------------------------------------------------------------
    def test_logout_clears_the_cookie(self):
        for origin in (PARENT, "https://bot.example", None):
            code, h, body = self.req("POST", "/_gate/logout", {}, {"Origin": origin} if origin else {})
            self.assertEqual(code, 200, origin)
            self.assertEqual(h["set-cookie"], "__Host-pb_session=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict")
            self.assertIn("Signed out", body)
            self.assertNotIn("<script", body)           # it must not ask to be signed in again
            self.assertIn(f"frame-ancestors 'self' {PARENT}", h["content-security-policy"])
        for origin in ("https://evil.example", "null", "https://b-other.example"):
            code, h, _ = self.req("POST", "/_gate/logout", {}, {"Origin": origin})
            self.assertEqual(code, 403, origin)
            self.assertNotIn("set-cookie", h)
        code, h, _ = self.req("GET", "/_gate/logout")
        self.assertEqual((code, h.get("allow")), (405, "POST"))
        self.assertNotIn("set-cookie", h)

    # ---- the rest ----------------------------------------------------------------------
    def test_failed_attempts_are_rate_limited(self):
        ip = "203.0.113.7"
        for _ in range(5):
            self.assertEqual(self.sso("v1.nonsense", ip=ip)[0], 403)
        code, h, body = self.sso("v1.nonsense", ip=ip)
        self.assertEqual(code, 429)
        self.assertNotIn("set-cookie", h)
        self.assertEqual(self.sso(mint(ttl=-5), ip=ip)[0], 429)
        self.assertEqual(self.sso("v1.nonsense", ip="203.0.113.8")[0], 403)   # another address is not affected
        # A good token is still accepted: the limit cannot be used to lock the owner out.
        self.assertEqual(self.sso(mint(), ip=ip)[0], 303)
        # The total across all addresses is limited too.
        for i in range(60):
            self.sso("v1.nonsense", ip=f"192.0.2.{i}")
        self.assertEqual(self.sso("v1.nonsense", ip="192.0.2.200")[0], 429)
        self.assertEqual(self.sso(mint(), ip="192.0.2.201")[0], 303)

    def test_failure_page(self):
        code, h, body = self.sso(mint(ttl=-1))
        self.assertEqual(code, 403)
        self.assertIn('data-gate="failed"', body)
        self.assertNotIn("expired", self.gate.SSO_PAGES["failed"].decode().split("did not work")[0])
        self.assertIn(f"frame-ancestors 'self' {PARENT}", h["content-security-policy"])
        self.assertNotIn("x-frame-options", h)

    def test_get_and_unknown_paths(self):
        self.assertEqual(self.req("GET", "/_gate/sso?token=" + mint())[0], 405)
        self.assertEqual(self.req("GET", "/_gate/health")[0:3:2], (200, "ok"))
        self.assertEqual(self.req("GET", "/_gate/nothing")[0], 404)
        self.assertEqual(self.req("POST", "/_gate/nothing", {})[0], 404)

    def test_nothing_secret_is_logged(self):
        t = mint()
        self.req("GET", "/_gate/sso?token=" + t)
        _, h, _ = self.sso(t)
        cookie = h["set-cookie"].split(";")[0].split("=", 1)[1]
        self.sso(t)                                     # refused: a log line with the reason
        self.sso(mint(ttl=-1))
        self.check("__Host-pb_session=" + cookie)
        logs = "\n".join(self.gate.LOGS)
        self.assertIn("sso refused: used before", logs)
        self.assertIn("sso refused: expired", logs)
        for secret in (t, t.rsplit(".", 1)[1], cookie, PASSWORD):
            self.assertNotIn(secret, logs)


class SsoOff(GateCase):
    """The password gate of the self-hosted bot: as it was before SSO existed."""
    ENV = PW_ENV
    NAME = "gate_password"
    PW = PW_ENV["ACCESS_PASSWORD"]

    def login(self, password=None, nxt="/", headers=None):
        return self.req("POST", "/_gate/login", {"password": self.PW if password is None else password, "next": nxt}, headers)

    def test_browser_without_a_session_is_sent_to_the_login_form(self):
        code, h, _ = self.check(headers={"Accept": "text/html", "X-Forwarded-Uri": "/chat?a=1"})
        self.assertEqual((code, h["location"]), (302, "/_gate/login?next=%2Fchat%3Fa%3D1"))
        code, h, body = self.req("GET", "/_gate/login?next=%2Fchat")
        self.assertEqual(code, 200)
        self.assertIn('<form method="post" action="/_gate/login">', body)
        self.assertIn('name="password"', body)
        self.assertIn('name="next" value="/chat"', body)
        self.assertIn("frame-ancestors 'none'", h["content-security-policy"])
        self.assertNotIn("script-src", h["content-security-policy"])
        self.assertEqual(h["x-frame-options"], "DENY")
        self.assertEqual(self.check(headers={"Accept": "application/json"})[0], 401)
        self.assertEqual(self.check(headers={"Accept": "text/html", "Upgrade": "websocket"})[0], 401)

    def test_password_login_and_the_cookie_as_before(self):
        code, h, body = self.login("wrong")
        self.assertEqual(code, 401)
        self.assertIn("Wrong password.", body)
        code, h, _ = self.login(nxt="/desktop/")
        self.assertEqual((code, h["location"]), (303, "/desktop/"))
        value = h["set-cookie"].split(";")[0]
        self.assertTrue(value.startswith("pb_session="))
        self.assertEqual(h["set-cookie"], value + "; Path=/; Max-Age=604800; HttpOnly; SameSite=Lax")
        code, h, _ = self.check(value)
        self.assertEqual((code, h.get("x-gate-user")), (200, "owner"))
        # Behind HTTPS the cookie is marked Secure, as before.
        _, h, _ = self.login(headers={"X-Forwarded-Proto": "https"})
        self.assertTrue(h["set-cookie"].endswith("; HttpOnly; SameSite=Lax; Secure"))
        # The SSO cookie name means nothing here.
        self.assertEqual(self.check("__Host-" + value)[0], 401)

    def test_no_origin_rule_in_password_mode(self):
        value = self.login()[1]["set-cookie"].split(";")[0]
        for h in ({"X-Forwarded-Method": "POST", "Origin": "https://elsewhere.example"},
                  {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "no-cors"}):
            self.assertEqual(self.check(value, h)[0], 200)

    def test_sso_endpoints_do_not_exist(self):
        token = mint(password=self.PW)
        self.assertEqual(self.req("POST", "/_gate/sso", {"token": token, "next": "/"}, {"Origin": PARENT})[0], 404)
        self.assertEqual(self.req("GET", "/_gate/sso.js")[0], 404)
        self.assertEqual(self.req("GET", "/_gate/sso")[0], 404)
        self.assertEqual(self.req("POST", "/_gate/logout", {})[0], 404)

    def test_logout_as_before(self):
        code, h, _ = self.req("GET", "/_gate/logout")
        self.assertEqual((code, h["location"]), (302, "/_gate/login"))
        self.assertEqual(h["set-cookie"], "pb_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")

    def test_failed_logins_are_rate_limited_as_before(self):
        real, self.gate.time.sleep = self.gate.time.sleep, lambda s: None   # the half-second pause per failure
        try:
            for _ in range(5):
                self.assertEqual(self.login("wrong")[0], 401)
            self.assertEqual(self.login("wrong")[0], 429)
            self.assertEqual(self.login()[0], 429)       # here the limit also stops the right password
        finally:
            self.gate.time.sleep = real

    def test_next_after_login(self):
        self.assertEqual(self.login(nxt="//evil.example")[1]["location"], "/")
        self.assertEqual(self.login(nxt="/\t/evil.example")[1]["location"], "/")
        self.assertEqual(self.login(nxt="/a?b=c")[1]["location"], "/a?b=c")


class Startup(unittest.TestCase):
    """Settings that must stop the gate from starting."""

    def run_gate(self, env):
        full = {k: v for k, v in os.environ.items() if k not in GATE_VARS}
        full.update(env)
        full["GATE_LISTEN_HOST"], full["GATE_LISTEN_PORT"] = "127.0.0.1", "0"
        p = subprocess.Popen([sys.executable, "-I", GATE_PY], env=full, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = p.communicate(timeout=1.5)
            return p.returncode, out + err
        except subprocess.TimeoutExpired:   # it is serving
            p.kill()
            out, err = p.communicate()
            return None, out + err

    def test_good_settings_start(self):
        self.assertIsNone(self.run_gate(SSO_ENV)[0])
        self.assertIsNone(self.run_gate(PW_ENV)[0])
        without = {k: v for k, v in SSO_ENV.items() if k != "GATE_PARENT_ORIGIN"}
        self.assertIsNone(self.run_gate(without)[0])
        self.assertIsNone(self.run_gate({**PW_ENV, "GATE_SSO": "off"})[0])

    def test_bad_settings_refuse_to_start(self):
        cases = [
            ({"GATE_SSO": "true"}, "GATE_SSO"),                       # a typo must not mean "password gate"
            ({"GATE_BOT_ID": ""}, "GATE_BOT_ID"),
            ({"GATE_BOT_ID": "bK4ZQ2M7XN3"}, "GATE_BOT_ID"),
            ({"GATE_LOGIN_URL": ""}, "GATE_LOGIN_URL"),
            ({"GATE_LOGIN_URL": "http://site.example/bot"}, "GATE_LOGIN_URL"),
            ({"GATE_LOGIN_URL": "https://site.example/bot page"}, "GATE_LOGIN_URL"),
            ({"GATE_PARENT_ORIGIN": "https://site.example/"}, "GATE_PARENT_ORIGIN"),
            ({"GATE_PARENT_ORIGIN": "https://*.example"}, "GATE_PARENT_ORIGIN"),
            ({"GATE_PARENT_ORIGIN": "http://site.example"}, "GATE_PARENT_ORIGIN"),
            ({"GATE_PARENT_ORIGIN": "https://site.example https://evil.example"}, "GATE_PARENT_ORIGIN"),
            ({"ACCESS_PASSWORD": "short-password-123"}, "ACCESS_PASSWORD"),
            ({"ACCESS_PASSWORD": ""}, "ACCESS_PASSWORD"),
        ]
        for change, word in cases:
            code, out = self.run_gate({**SSO_ENV, **change})
            self.assertNotIn(code, (None, 0), change)
            self.assertIn(word, out, change)

    def test_parent_origin_defaults_to_the_login_page(self):
        g = load_gate("gate_default_parent", {k: v for k, v in SSO_ENV.items() if k != "GATE_PARENT_ORIGIN"} |
                      {"GATE_LOGIN_URL": "https://Site.Example:443/bot?x=1"})
        self.assertEqual(g.PARENT_ORIGIN, "https://site.example")
        self.assertIn("frame-ancestors 'self' https://site.example;", g.SSO_CSP)


if __name__ == "__main__":
    unittest.main(verbosity=2)
