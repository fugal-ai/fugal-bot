"""Model proxy for the bot (stdlib only, no dependencies). One program, two roles
(MODELPROXY_MODE):

room (default)
    Runs next to one bot. The agent container talks to this service instead of
    the model provider. This process alone holds the provider's base URL and
    API key (MODEL_BASE_URL, MODEL_API_KEY); the agent is configured with this
    proxy's internal address and a placeholder key. Whatever Authorization
    header a client sends is dropped and replaced here.

gateway
    Runs once, centrally, in front of one upstream account that many bots
    share. Each bot ("tenant") has its own token, `fbk_<bot_id>_<43 chars>`;
    only the SHA-256 of a token is stored here. Every tenant has a daily
    allowance in US dollars (UTC day) and a list of model ids it may use, and
    there is one daily cap across all tenants. The tenant table is replaced
    as a whole through an admin API on a second listener
    (MODELPROXY_ADMIN_LISTEN_HOST / _PORT) that must never be published:
        PUT /admin/tenants   {"tenants":[{"id","token_sha256","usd_per_day","models":[..],"enabled"}],
                              "prices":{model:[in,out] or [in,out,cached_in] USD per million tokens},
                              "platform_usd_per_day":N}
        GET /admin/tenants   the table as stored
        GET /admin/usage     per-tenant and platform usage, today and recent days
    A room-mode proxy can point at a gateway (MODEL_BASE_URL = the gateway,
    MODEL_API_KEY = the bot's token) like at any other endpoint.

Endpoints on the main listener (both roles):
  POST /v1/chat/completions, /v1/completions, /v1/responses, /v1/embeddings
  GET  /v1/models, /v1/models/<id>   room: passed through; gateway: the tenant's own list
  GET  /usage                        room only: token counters, limits, what is left (JSON)
  GET  /healthz                      -> 200
Anything else is refused: the key can only be used for inference calls.

Metering: prompt and completion tokens are taken from the upstream `usage`
field of every successful response, and so are the cached prompt tokens when the
upstream says how many (see usage_tokens), and added to per-day (UTC) counters kept
in MODELPROXY_DATA_DIR (room: usage.json, with a running total; gateway:
tenants.json and one gateway-usage-<day>.json per day). Files are replaced
atomically. On streams the proxy asks the upstream for usage
(`stream_options.include_usage`) when the client did not, and then removes
the extra usage-only chunk again so the client sees exactly the stream it
asked for.

Budget, room: BUDGET_TOKENS_PER_DAY, BUDGET_TOKENS_TOTAL, BUDGET_USD_PER_DAY,
BUDGET_USD_TOTAL (need BUDGET_PRICES), BUDGET_REQUESTS_PER_MINUTE, all
optional. Budget, gateway: from the tenant table. A request that arrives after
an allowance is used up is not sent upstream; how it is answered is set by
MODELPROXY_EXHAUSTED_REPLY (see EXHAUSTED_REPLIES).

Upstream 429 / 503: retried here, waiting as the upstream's Retry-After says,
as long as nothing has been sent to the client (MODELPROXY_UPSTREAM_RETRIES,
MODELPROXY_RETRY_MAX_WAIT; on by default in gateway mode only).

Never logged or stored: request bodies, response bodies, headers, the
upstream key, tenant tokens.
"""

import collections
import datetime
import email.utils
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import signal
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
              "te", "trailer", "trailers", "transfer-encoding", "upgrade"}
# Not forwarded upstream: hop-by-hop headers, and every credential a client
# may send (the agent only has a placeholder; the real key is added here).
DROP_REQUEST = HOP_BY_HOP | {"host", "content-length", "authorization", "accept-encoding", "expect",
                             "cookie", "x-api-key", "api-key"}
DROP_RESPONSE = HOP_BY_HOP | {"content-length"}
# Gateway mode passes on only these: a tenant's other headers (its address as
# added by a reverse proxy, cookies, ...) are not the upstream's business, and
# the shared upstream account's headers (organisation, rate-limit state,
# cookies) are not the tenant's.
GATEWAY_REQUEST_HEADERS = {"content-type", "accept", "user-agent"}
GATEWAY_RESPONSE_HEADERS = {"content-type", "cache-control", "date", "x-request-id"}
# Upstream statuses that say something about the shared upstream account, not
# about the tenant's request. A tenant gets a plain 503 instead (see forward()).
GATEWAY_MASKED_STATUS = (401, 402, 403, 429, 503)
RETRY_STATUS = (429, 503)
POST_ROUTES = ("chat/completions", "completions", "responses", "embeddings")
STREAM_USAGE_ROUTES = ("chat/completions", "completions")
PATH_OK = re.compile(r"[A-Za-z0-9._:@+-]+(/[A-Za-z0-9._:@+-]+)*\Z")
QUERY_OK = re.compile(r"[A-Za-z0-9._~=&%+:,@/-]*\Z")
KEEP_JSON_BYTES = 8 * 1024 * 1024   # non-streamed replies are read for `usage` up to this size
MAX_EVENT_BYTES = 1024 * 1024       # a held-back partial SSE event is released past this size
KEEP_DAYS = 400
KEEP_GATEWAY_DAYS = 35              # gateway: one usage file per UTC day
MAX_ADMIN_BODY = 8 * 1024 * 1024
TOKEN = re.compile(r"fbk_([A-Za-z0-9]{1,40})_([A-Za-z0-9_-]{43})\Z")
TENANT_ID = re.compile(r"[A-Za-z0-9]{1,40}\Z")
SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
# How a request is answered once an allowance is used up (MODELPROXY_EXHAUSTED_REPLY):
#   429  HTTP 429, error type/code insufficient_quota (what OpenAI sends)
#   402  HTTP 402, error type/code insufficient_quota
#   400  HTTP 400, error type invalid_request_error, code allowance_exhausted
#   200  a normal assistant message carrying the notice (chat completions only;
#        other endpoints get the 400 form). Costs nothing, is not counted.
EXHAUSTED_REPLIES = ("429", "402", "400", "200")
# 400 is the default because of how OpenClaw (v2026.11.6) treats each form
# (measured; see docs/MODELPROXY.md): 429 and 402 with insufficient_quota count
# as a billing failure and switch the provider off for ten minutes, even after
# the allowance is back; a 200 is taken for a real answer, also by context
# compaction, which then stores the notice as the conversation summary. A 400
# is shown to the user, costs one request, and the next call goes through.
DEFAULT_EXHAUSTED_REPLY = "400"
# What the user is told. Deliberately without any punctuation: OpenClaw's web
# chat shows an error text with a backslash before every punctuation mark.
# And without words agent frameworks read as "retry soon" or as a billing
# failure of the key (rate limit, quota, exceeded, credits, ...).
USED_UP_TODAY = "This bot has used up its inference allowance for today and it renews at midnight UTC"
USED_UP_TOTAL = "This bot has used up its whole inference allowance and the owner of the bot can raise it"
USED_UP_PLATFORM = "The inference allowance shared by all bots is used up for today and it renews at midnight UTC"


def log(msg):
    sys.stderr.write("[modelproxy] %s\n" % msg)
    sys.stderr.flush()


class ConfigError(Exception):
    pass


def _count(env, name, default):
    """A whole number of zero or more, where 0 is a real value (not "no limit")."""
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number") from None
    if v < 0:
        raise ConfigError(f"{name} must be zero or more")
    return v


def _number(env, name, kind):
    raw = (env.get(name) or "").strip()
    if not raw:
        return None
    try:
        v = kind(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None
    if not math.isfinite(v) or v < 0:
        raise ConfigError(f"{name} must be zero or more")
    return v or None  # empty or 0 = no limit


class Config:
    """Everything the proxy reads from the environment."""

    def __init__(self, env):
        base = (env.get("MODEL_BASE_URL") or "").strip().rstrip("/")
        try:
            u = urllib.parse.urlsplit(base)
            port = u.port
        except ValueError:
            raise ConfigError("MODEL_BASE_URL is not a valid URL") from None
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ConfigError("MODEL_BASE_URL is required (an http:// or https:// OpenAI-compatible base URL)")
        if u.username or u.password or u.query or u.fragment:
            raise ConfigError("MODEL_BASE_URL must not contain credentials, a query or a fragment")
        self.scheme, self.host, self.port, self.base_path = u.scheme, u.hostname, port, u.path.rstrip("/")
        self.key = (env.get("MODEL_API_KEY") or "").strip()
        if any(ord(c) < 33 or ord(c) > 126 for c in self.key):
            raise ConfigError("MODEL_API_KEY contains characters that cannot go into a header")

        self.mode = (env.get("MODELPROXY_MODE") or "room").strip().lower()
        if self.mode not in ("room", "gateway"):
            raise ConfigError("MODELPROXY_MODE must be 'room' or 'gateway'")
        self.gateway = self.mode == "gateway"
        self.listen_host = (env.get("MODELPROXY_LISTEN_HOST") or "0.0.0.0").strip()
        self.listen_port = int(_number(env, "MODELPROXY_LISTEN_PORT", int) or 9001)
        self.data_dir = (env.get("MODELPROXY_DATA_DIR") or "/data").strip()
        self.max_body = int((_number(env, "MODELPROXY_MAX_BODY_MB", float) or 32) * 1024 * 1024)
        self.upstream_timeout = _number(env, "MODELPROXY_UPSTREAM_TIMEOUT", float) or 600.0
        self.stream_usage = (env.get("MODELPROXY_STREAM_USAGE") or "true").strip().lower() != "false"
        self.exhausted_reply = (env.get("MODELPROXY_EXHAUSTED_REPLY") or DEFAULT_EXHAUSTED_REPLY).strip()
        if self.exhausted_reply not in EXHAUSTED_REPLIES:
            raise ConfigError("MODELPROXY_EXHAUSTED_REPLY must be one of: " + ", ".join(EXHAUSTED_REPLIES))

        # Upstream 429 / 503: how often to try again, and the longest total wait (seconds).
        self.retries = _count(env, "MODELPROXY_UPSTREAM_RETRIES", 3 if self.gateway else 0)
        self.retry_max_wait = _number(env, "MODELPROXY_RETRY_MAX_WAIT", float) or 30.0
        self.retry_backoff = 1.0      # first wait when the upstream names none; doubles each time
        self.sleep = time.sleep

        # Gateway mode only. The admin listener is for the private network
        # between the gateway and its controller; never publish it.
        self.admin_host = (env.get("MODELPROXY_ADMIN_LISTEN_HOST") or "127.0.0.1").strip()
        self.admin_port = _count(env, "MODELPROXY_ADMIN_LISTEN_PORT", 9002)
        self.admin_token = (env.get("MODELPROXY_ADMIN_TOKEN") or "").strip()   # optional second lock
        self.tenant_concurrency = _count(env, "MODELPROXY_TENANT_CONCURRENCY", 4) or None
        if self.gateway and self.admin_port == self.listen_port and self.admin_port != 0:
            raise ConfigError("MODELPROXY_ADMIN_LISTEN_PORT must differ from MODELPROXY_LISTEN_PORT")

        self.tokens_per_day = _number(env, "BUDGET_TOKENS_PER_DAY", int)
        self.tokens_total = _number(env, "BUDGET_TOKENS_TOTAL", int)
        self.usd_per_day = _number(env, "BUDGET_USD_PER_DAY", float)
        self.usd_total = _number(env, "BUDGET_USD_TOTAL", float)
        self.requests_per_minute = _number(env, "BUDGET_REQUESTS_PER_MINUTE", int)
        self.prices = self._prices((env.get("BUDGET_PRICES") or "").strip())
        if (self.usd_per_day or self.usd_total) and not self.prices:
            raise ConfigError("BUDGET_USD_PER_DAY / BUDGET_USD_TOTAL need BUDGET_PRICES (USD per million tokens)")
        if self.gateway and (any(self.limits().values()) or self.prices):
            raise ConfigError("BUDGET_* settings are for room mode; a gateway takes allowances and prices "
                              "from the tenant table (PUT /admin/tenants)")

    @staticmethod
    def _prices(raw):
        """{"model-id": [usd per 1M input, per 1M output(, per 1M cached input)], "*": [...]}"""
        if not raw:
            return {}
        bad = ConfigError('BUDGET_PRICES must be JSON like {"model-id": [0.5, 1.5], "*": [1, 3, 0.1]} '
                          "(USD per million input and output tokens, optionally a third number for cached input tokens)")
        try:
            table = json.loads(raw)
        except ValueError:
            raise bad from None
        out = price_table(table)
        if not out:
            raise bad
        return out

    def price(self, model):
        return price_of(self.prices, model)

    def connect(self):
        if self.scheme == "https":
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=30, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
        conn.connect()
        conn.sock.settimeout(self.upstream_timeout)
        return conn

    def limits(self):
        return {"tokens_per_day": self.tokens_per_day, "tokens_total": self.tokens_total,
                "usd_per_day": self.usd_per_day, "usd_total": self.usd_total,
                "requests_per_minute": self.requests_per_minute}


def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0


def price_table(table):
    """A checked copy of {"model-id": [in, out]} or {"model-id": [in, out, cached_in]}
    (USD per million tokens), or None if malformed. A row keeps the number of
    values it was given, so a stored table is written back as it was read."""
    if not isinstance(table, dict):
        return None
    out = {}
    for model, pair in table.items():
        if not (isinstance(model, str) and model and isinstance(pair, list) and len(pair) in (2, 3)
                and all(_is_number(x) for x in pair)):
            return None
        out[model] = tuple(float(x) for x in pair)
    return out


def price_of(prices, model):
    """(input, output, cached input) USD per million tokens for a model: its own
    row, else "*", else the dearest row (an unlisted model must never be free
    under a USD cap). A row without a cached price bills cached tokens as input."""
    if not prices:
        return (0.0, 0.0, 0.0)
    if model in prices:
        row = prices[model]
    elif "*" in prices:
        row = prices["*"]
    else:
        row = max(prices.values(), key=lambda p: p[0] + p[1])
    return (row[0], row[1], row[2] if len(row) > 2 else row[0])


def cost_usd(price, prompt, completion, cached=0):
    """USD for one call. `price` is (input, output, cached input) per million
    tokens; cached tokens are part of `prompt`, and never more than it."""
    cached = min(max(cached, 0), max(prompt, 0))
    pin, pout, pcached = price
    return max(0.0, ((max(prompt, 0) - cached) * pin + cached * pcached + max(completion, 0) * pout) / 1e6)


def write_json(path, obj):
    """Replace a file as a whole: readers and a crash see the old or the new content, never a mix."""
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def utc_day(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")


def _zero():
    return {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "cached_prompt_tokens": 0, "usd": 0.0, "estimated_requests": 0}


class Meter:
    """Per-day (UTC) and total counters, kept in one small JSON file."""

    def __init__(self, cfg, clock=time.time):
        self.cfg, self.clock = cfg, clock
        self.path = os.path.join(cfg.data_dir, "usage.json")
        self.lock = threading.Lock()
        self.total, self.days = _zero(), {}
        self.rejected = {"budget": 0, "rate_limit": 0}   # since this start
        self.recent = collections.deque()                # request times, for the per-minute limit
        self._load()
        self._save()  # fails now, not later, if the data directory is not writable

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            total = {**_zero(), **{k: v for k, v in data["total"].items() if k in _zero()}}
            days = {str(d): {**_zero(), **{k: v for k, v in c.items() if k in _zero()}}
                    for d, c in data.get("days", {}).items()}
            for row in [total, *days.values()]:
                if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for v in row.values()):
                    raise ValueError("bad counter")
            self.total, self.days = total, days
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            keep = f"{self.path}.unreadable-{int(self.clock())}"
            try:
                os.replace(self.path, keep)
            except OSError:
                pass
            log(f"usage file was unreadable; kept a copy as {os.path.basename(keep)} and started from zero")

    def _save(self):
        for d in sorted(self.days)[:-KEEP_DAYS]:
            del self.days[d]
        write_json(self.path, {"version": 1, "total": self.total, "days": self.days})

    def today(self):
        return utc_day(self.clock())

    def _exceeded(self):
        """(limit name, message for the user) for the first cap that is reached, else None. Lock held."""
        c, t, d = self.cfg, self.total, self.days.get(self.today(), _zero())
        if c.tokens_total and t["total_tokens"] >= c.tokens_total:
            return "tokens_total", USED_UP_TOTAL
        if c.usd_total and t["usd"] >= c.usd_total:
            return "usd_total", USED_UP_TOTAL
        if c.tokens_per_day and d["total_tokens"] >= c.tokens_per_day:
            return "tokens_per_day", USED_UP_TODAY
        if c.usd_per_day and d["usd"] >= c.usd_per_day:
            return "usd_per_day", USED_UP_TODAY
        return None

    def admit(self):
        """Called before a request goes upstream. None = go ahead;
        ("exhausted", message) = an allowance is used up;
        ("error", http status, error type, message, extra headers) otherwise."""
        with self.lock:
            over = self._exceeded()
            if over:
                self.rejected["budget"] += 1
                return ("exhausted", over[1])
            rpm = self.cfg.requests_per_minute
            if rpm:
                now = self.clock()
                while self.recent and self.recent[0] <= now - 60:
                    self.recent.popleft()
                if len(self.recent) >= rpm:
                    self.rejected["rate_limit"] += 1
                    wait = max(1, math.ceil(self.recent[0] + 60 - now))
                    return ("error", 429, "rate_limit_exceeded",
                            f"Rate limit reached: this bot may make {rpm} model requests per minute. "
                            f"Please try again in {wait}s.", [("Retry-After", str(wait))])
                self.recent.append(now)
        return None

    def record(self, model, prompt, completion, estimated=False, cached=0):
        cached = min(cached, prompt)
        usd = cost_usd(self.cfg.price(model), prompt, completion, cached)
        with self.lock:
            for row in (self.total, self.days.setdefault(self.today(), _zero())):
                row["requests"] += 1
                row["prompt_tokens"] += prompt
                row["cached_prompt_tokens"] += cached
                row["completion_tokens"] += completion
                row["total_tokens"] += prompt + completion
                row["usd"] = round(row["usd"] + usd, 8)
                row["estimated_requests"] += 1 if estimated else 0
            try:
                self._save()
            except OSError as e:
                log(f"could not write the usage file ({type(e).__name__}); counting in memory")

    def snapshot(self):
        with self.lock:
            c, today = self.cfg, self.today()
            day, over = self.days.get(today, _zero()), self._exceeded()

            def left(limit, used):
                return None if not limit else max(0, round(limit - used, 8))

            return {
                "today": today,
                "day": dict(day),
                "total": dict(self.total),
                "days": {d: dict(self.days[d]) for d in sorted(self.days)[-31:]},
                "limits": c.limits(),
                "remaining": {"tokens_per_day": left(c.tokens_per_day, day["total_tokens"]),
                              "tokens_total": left(c.tokens_total, self.total["total_tokens"]),
                              "usd_per_day": left(c.usd_per_day, day["usd"]),
                              "usd_total": left(c.usd_total, self.total["usd"])},
                "exceeded": over[0] if over else None,
                "priced": bool(c.prices),
                "rejected_since_start": dict(self.rejected),
            }


class StateError(Exception):
    """A tenant table that cannot be accepted; the message says why."""


def parse_state(obj):
    """Checks a tenant table (the body of PUT /admin/tenants) and returns it in
    the form kept in memory. All or nothing: any fault raises StateError."""
    if not isinstance(obj, dict):
        raise StateError("the body must be a JSON object")
    unknown = set(obj) - {"tenants", "prices", "platform_usd_per_day"}
    if unknown:
        raise StateError("unknown field: " + sorted(unknown)[0])
    for field in ("tenants", "prices", "platform_usd_per_day"):
        if field not in obj:
            raise StateError(f"'{field}' is required (this call replaces the whole table)")
    prices = price_table(obj["prices"])
    if prices is None:
        raise StateError("'prices' must be an object like {\"model-id\": [0.5, 1.5]} or "
                         "{\"model-id\": [0.5, 1.5, 0.05]} (USD per million input, output and, "
                         "optionally, cached input tokens)")
    cap = obj["platform_usd_per_day"]
    if cap is not None and not _is_number(cap):
        raise StateError("'platform_usd_per_day' must be a number of zero or more, or null for no cap")
    if not isinstance(obj["tenants"], list):
        raise StateError("'tenants' must be a list")
    tenants = {}
    for i, t in enumerate(obj["tenants"]):
        where = f"tenants[{i}]"
        if not isinstance(t, dict):
            raise StateError(f"{where} must be an object")
        unknown = set(t) - {"id", "token_sha256", "usd_per_day", "models", "enabled"}
        if unknown:
            raise StateError(f"{where}: unknown field: " + sorted(unknown)[0])
        tid = t.get("id")
        if not (isinstance(tid, str) and TENANT_ID.match(tid)):
            raise StateError(f"{where}: 'id' must be 1 to 40 letters and digits")
        if tid in tenants:
            raise StateError(f"{where}: id '{tid}' appears twice")
        digest = t.get("token_sha256")
        if not (isinstance(digest, str) and SHA256_HEX.match(digest.lower())):
            raise StateError(f"{where}: 'token_sha256' must be the 64 hex digits of the token's SHA-256")
        if not _is_number(t.get("usd_per_day")):
            raise StateError(f"{where}: 'usd_per_day' must be a number of zero or more")
        models = t.get("models")
        if not (isinstance(models, list) and all(isinstance(m, str) and m and m != "*" for m in models)):
            raise StateError(f"{where}: 'models' must be a list of model ids")
        for m in models:
            if m not in prices and "*" not in prices:
                raise StateError(f"{where}: model '{m}' has no row in 'prices' (and there is no \"*\" row)")
        enabled = t.get("enabled", True)
        if not isinstance(enabled, bool):
            raise StateError(f"{where}: 'enabled' must be true or false")
        tenants[tid] = {"token_sha256": digest.lower(), "usd_per_day": float(t["usd_per_day"]),
                        "models": tuple(dict.fromkeys(models)), "enabled": enabled}
    return {"tenants": tenants, "prices": prices, "platform_usd_per_day": None if cap is None else float(cap)}


def export_state(state):
    """The tenant table in the shape PUT /admin/tenants takes."""
    return {"tenants": [{"id": tid, "token_sha256": t["token_sha256"], "usd_per_day": t["usd_per_day"],
                         "models": list(t["models"]), "enabled": t["enabled"]}
                        for tid, t in sorted(state["tenants"].items())],
            "prices": {m: list(p) for m, p in sorted(state["prices"].items())},
            "platform_usd_per_day": state["platform_usd_per_day"]}


class Gateway:
    """Gateway mode: the tenant table, and usage per tenant and for the whole
    platform per UTC day. Everything is behind one lock; the table is swapped
    as a whole, so a request sees the old table or the new one, never a mix."""

    def __init__(self, cfg, clock=time.time):
        self.cfg, self.clock = cfg, clock
        self.table_path = os.path.join(cfg.data_dir, "tenants.json")
        self.lock = threading.Lock()
        self.state = {"tenants": {}, "prices": {}, "platform_usd_per_day": None}
        self.days = {}        # "YYYY-MM-DD" -> {"platform": counters, "tenants": {id: counters}}
        self.in_flight = collections.Counter()
        self.rejected = collections.Counter()   # since this start
        self.loaded_table = self._load_table()
        self._load_days()
        self._save_day(self.today())  # fails now, not later, if the data directory is not writable

    # ---- files -----------------------------------------------------------------
    def _aside(self, path, what):
        keep = f"{path}.unreadable-{int(self.clock())}"
        try:
            os.replace(path, keep)
        except OSError:
            pass
        log(f"{what} was unreadable; kept a copy as {os.path.basename(keep)}")

    def _load_table(self):
        try:
            with open(self.table_path, encoding="utf-8") as f:
                self.state = parse_state(json.load(f))
            return True
        except FileNotFoundError:
            return False
        except (OSError, ValueError, StateError):
            self._aside(self.table_path, "the tenant table")
            return False

    def _day_path(self, day):
        return os.path.join(self.cfg.data_dir, f"gateway-usage-{day}.json")

    def _load_days(self):
        names = sorted(n for n in os.listdir(self.cfg.data_dir)
                       if re.fullmatch(r"gateway-usage-\d{4}-\d{2}-\d{2}\.json", n))
        for name in names[-KEEP_GATEWAY_DAYS:]:
            path, day = os.path.join(self.cfg.data_dir, name), name[14:24]
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                rows = {"platform": {k: data["platform"].get(k, v) for k, v in _zero().items()},
                        "tenants": {str(t): {k: c.get(k, v) for k, v in _zero().items()}
                                    for t, c in data["tenants"].items()}}
                for row in [rows["platform"], *rows["tenants"].values()]:
                    if not all(_is_number(v) for v in row.values()):
                        raise ValueError("bad counter")
                self.days[day] = rows
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                self._aside(path, f"the usage file for {day}")

    def _save_day(self, day):
        rows = self.days.setdefault(day, {"platform": _zero(), "tenants": {}})
        write_json(self._day_path(day), {"version": 1, "day": day, **rows})
        for old in sorted(self.days)[:-KEEP_GATEWAY_DAYS]:
            del self.days[old]
            try:
                os.remove(self._day_path(old))
            except OSError:
                pass

    def today(self):
        return utc_day(self.clock())

    # ---- the tenant table ------------------------------------------------------
    def replace(self, obj):
        """Checks and stores a new table, then puts it into force. Raises
        StateError (nothing changed) or OSError (nothing changed either: a
        table that could not be stored is not used)."""
        state = parse_state(obj)
        with self.lock:
            write_json(self.table_path, export_state(state))
            self.state = state
        on = sum(1 for t in state["tenants"].values() if t["enabled"])
        cap = state["platform_usd_per_day"]
        log(f"tenant table replaced: {len(state['tenants'])} tenants ({on} enabled), {len(state['prices'])} prices, "
            f"platform cap {'none' if cap is None else f'{cap:g} USD a day'}")
        return {"ok": True, "tenants": len(state["tenants"]), "enabled": on}

    def export(self):
        with self.lock:
            return export_state(self.state)

    def authenticate(self, header):
        """(tenant id, tenant) for an `Authorization: Bearer fbk_...` header, else None."""
        scheme, _, token = (header or "").strip().partition(" ")
        m = TOKEN.match(token.strip()) if scheme.lower() == "bearer" else None
        with self.lock:
            tenant = self.state["tenants"].get(m.group(1)) if m else None
            if tenant is None or not hmac.compare_digest(
                    hashlib.sha256(m.group(0).encode()).hexdigest(), tenant["token_sha256"]):
                self.rejected["auth"] += 1
                return None
            return m.group(1), tenant

    # ---- allowances ------------------------------------------------------------
    def _used(self, tid):
        day = self.days.get(self.today())
        if not day:
            return 0.0, 0.0
        return day["tenants"].get(tid, _zero())["usd"], day["platform"]["usd"]

    def admit(self, tid, tenant):
        """Called before a request goes upstream; see Meter.admit for the result.
        A call that is let through holds one of the tenant's slots until done()."""
        with self.lock:
            used, platform_used = self._used(tid)
            if used >= tenant["usd_per_day"]:
                self.rejected["tenant_allowance"] += 1
                return ("exhausted", USED_UP_TODAY)
            cap = self.state["platform_usd_per_day"]
            if cap is not None and platform_used >= cap:
                self.rejected["platform_allowance"] += 1
                return ("exhausted", USED_UP_PLATFORM)
            limit = self.cfg.tenant_concurrency
            if limit and self.in_flight[tid] >= limit:
                self.rejected["busy"] += 1
                return ("error", 429, "rate_limit_exceeded",
                        f"This bot already has {limit} model requests in progress. Please try again in 2s.",
                        [("Retry-After", "2")])
            self.in_flight[tid] += 1
        return None

    def count(self, reason):
        with self.lock:
            self.rejected[reason] += 1

    def done(self, tid):
        with self.lock:
            self.in_flight[tid] -= 1
            if self.in_flight[tid] <= 0:
                del self.in_flight[tid]

    def record(self, tid, model, prompt, completion, estimated=False, cached=0):
        cached = min(cached, prompt)
        with self.lock:
            usd = cost_usd(price_of(self.state["prices"], model), prompt, completion, cached)
            today = self.today()
            day = self.days.setdefault(today, {"platform": _zero(), "tenants": {}})
            for row in (day["platform"], day["tenants"].setdefault(tid, _zero())):
                row["requests"] += 1
                row["prompt_tokens"] += prompt
                row["cached_prompt_tokens"] += cached
                row["completion_tokens"] += completion
                row["total_tokens"] += prompt + completion
                row["usd"] = round(row["usd"] + usd, 8)
                row["estimated_requests"] += 1 if estimated else 0
            try:
                self._save_day(today)
            except OSError as e:
                log(f"could not write the usage file ({type(e).__name__}); counting in memory")

    def usage(self, ndays=7):
        """What GET /admin/usage returns: today and the last `ndays` days with
        any use, for the platform and per tenant (tenants removed from the
        table stay listed while they have use in that window)."""
        with self.lock:
            today, state = self.today(), self.state
            recent = sorted(self.days)[-max(1, min(ndays, KEEP_GATEWAY_DAYS)):]
            now = self.days.get(today, {"platform": _zero(), "tenants": {}})

            def left(limit, used):
                return None if limit is None else max(0.0, round(limit - used, 8))

            cap = state["platform_usd_per_day"]
            tenants = {}
            for tid in [*state["tenants"], *(t for d in recent for t in self.days[d]["tenants"])]:
                if tid in tenants:
                    continue
                t, used = state["tenants"].get(tid), now["tenants"].get(tid, _zero())
                limit = t["usd_per_day"] if t else None
                tenants[tid] = {
                    "in_table": t is not None, "enabled": bool(t and t["enabled"]), "usd_per_day": limit,
                    "models": list(t["models"]) if t else [], "day": dict(used),
                    "remaining_usd": left(limit, used["usd"]),
                    "exceeded": limit is not None and used["usd"] >= limit,
                    "in_flight": self.in_flight.get(tid, 0),
                    "days": {d: dict(self.days[d]["tenants"][tid]) for d in recent if tid in self.days[d]["tenants"]}}
            return {"today": today,
                    "platform": {"usd_per_day": cap, "day": dict(now["platform"]),
                                 "remaining_usd": left(cap, now["platform"]["usd"]),
                                 "exceeded": cap is not None and now["platform"]["usd"] >= cap,
                                 "days": {d: dict(self.days[d]["platform"]) for d in recent}},
                    "tenants": tenants,
                    "rejected_since_start": dict(self.rejected)}


def usage_tokens(obj):
    """(prompt, completion, cached prompt) from an OpenAI-style object, or None.
    Understands Chat Completions / Embeddings (`usage.prompt_tokens`,
    `completion_tokens`) and the Responses API (`usage.input_tokens`,
    `output_tokens`, also nested under `response` in its final stream event).
    Cached prompt tokens (part of the prompt, never more than it) are read from
    the first of these that is present: `prompt_tokens_details.cached_tokens`
    (OpenAI, Chutes, vLLM), `input_tokens_details.cached_tokens` (Responses
    API), `prompt_cache_hit_tokens` (DeepSeek), `cache_read_input_tokens`
    (Anthropic style), top-level `cached_tokens`; else 0."""
    if not isinstance(obj, dict):
        return None
    u = obj.get("usage")
    if not isinstance(u, dict) and isinstance(obj.get("response"), dict):
        u = obj["response"].get("usage")
    if not isinstance(u, dict):
        return None

    def n(*names):
        for name in names:
            v = u.get(name)
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                return v
        return None

    def cached_count():
        for key in ("prompt_tokens_details", "input_tokens_details"):
            details = u.get(key)
            v = details.get("cached_tokens") if isinstance(details, dict) else None
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                return v
        return n("prompt_cache_hit_tokens", "cache_read_input_tokens", "cached_tokens") or 0

    prompt, completion, total = n("prompt_tokens", "input_tokens"), n("completion_tokens", "output_tokens"), n("total_tokens")
    if prompt is None and completion is None:
        if total is None:
            return None
        return total, 0, 0
    if prompt is None:
        prompt = max(0, total - completion) if total is not None else 0
    if completion is None:
        completion = max(0, total - prompt) if total is not None else 0
    return prompt, completion, min(cached_count(), prompt)


def retry_after(resp):
    """Seconds the upstream asks us to wait (`retry-after-ms`, or `Retry-After`
    as seconds or an HTTP date), or None if it does not say."""
    try:
        ms = resp.getheader("retry-after-ms")
        if ms is not None:
            return max(0.0, float(ms) / 1000)
        raw = (resp.getheader("Retry-After") or "").strip()
        if not raw:
            return None
        if re.fullmatch(r"\d+(\.\d+)?", raw):
            return float(raw)
        when = email.utils.parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        return max(0.0, when.timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return None


class SSEScanner:
    """Reads a server-sent-event stream as it passes through and remembers the
    last `usage` it saw. With strip=True (the proxy added
    stream_options.include_usage itself) the usage-only chunk (`"choices": []`)
    is removed; for that, bytes are released one complete event at a time.
    With strip=False every byte is released at once, unchanged."""

    def __init__(self, strip):
        self.strip, self.buf = strip, b""
        self.usage, self.chunks = None, 0

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            a, b = self.buf.find(b"\n\n"), self.buf.find(b"\r\n\r\n")
            ends = [i + n for i, n in ((a, 2), (b, 4)) if i >= 0]
            if not ends:
                break
            end = min(ends)
            event, self.buf = self.buf[:end], self.buf[end:]
            if self._keep(event):
                out.append(event)
        if len(self.buf) > MAX_EVENT_BYTES:  # not event-shaped: stop holding it
            if self.strip:
                out.append(self.buf)
            self.buf = b""
        return b"".join(out) if self.strip else data

    def finish(self):
        tail, self.buf = self.buf, b""
        if not tail:
            return b""
        keep = self._keep(tail)
        return tail if (self.strip and keep) else b""

    def _keep(self, event):
        keep = True
        for line in event.splitlines():
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            self.chunks += 1
            if b'"usage"' not in payload:
                continue
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            got = usage_tokens(obj)
            if got:
                self.usage = got
                if self.strip and obj.get("choices") == []:
                    keep = False
                    self.chunks -= 1
        return keep


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "modelproxy"
    sys_version = ""
    timeout = 900  # seconds a client connection may sit idle or blocked
    cfg = None    # set by make_server
    meter = None  # room mode
    gw = None     # gateway mode

    def log_message(self, fmt, *args):  # the default request log is replaced by one line in forward()
        pass

    # ---- small replies -------------------------------------------------------
    def send_json(self, code, obj, headers=()):
        body = json.dumps(obj).encode()
        self.send_response_only(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_api_error(self, status, kind, message, headers=(), code=None):
        """An error in the shape OpenAI clients expect."""
        self.send_json(status, {"error": {"message": message, "type": kind, "param": None, "code": code or kind}},
                       headers)

    def send_exhausted(self, rest, req, message):
        """The reply to a request that arrives after an allowance is used up,
        in the configured form (see EXHAUSTED_REPLIES)."""
        mode = self.cfg.exhausted_reply
        if mode == "200" and rest == "chat/completions" and isinstance(req, dict):
            return self.send_notice(req, message)
        if mode == "429":
            return self.send_api_error(429, "insufficient_quota", message)
        if mode == "402":
            return self.send_api_error(402, "insufficient_quota", message)
        return self.send_api_error(400, "invalid_request_error", message, code="allowance_exhausted")

    def send_notice(self, req, text):
        """A chat completion whose assistant message is `text`, made here (no
        upstream call, no tokens), streamed if the request asked for a stream."""
        now = int(time.time())
        base = {"id": f"chatcmpl-notice-{now}", "created": now,
                "model": req.get("model") if isinstance(req.get("model"), str) else "notice"}
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if req.get("stream") is not True:
            return self.send_json(200, {**base, "object": "chat.completion", "usage": usage, "choices": [
                {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]})
        base["object"] = "chat.completion.chunk"
        events = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                        "finish_reason": None}]},
                  {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
        opts = req.get("stream_options")
        if isinstance(opts, dict) and opts.get("include_usage") is True:
            events.append({**base, "choices": [], "usage": usage})
        body = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events) + b"data: [DONE]\n\n"
        self.send_response_only(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def route(self):
        """The part after /v1/ if the path is well-formed, else None; and the query."""
        path, _, query = self.path.partition("?")
        if not path.startswith("/v1/") or not QUERY_OK.match(query):
            return None, ""
        rest = path[4:]
        if not PATH_OK.match(rest) or ".." in rest.split("/"):
            return None, ""
        return rest, query

    def unknown(self):
        self.send_api_error(404, "invalid_request_error",
                            "Unknown endpoint. This proxy forwards only /v1/chat/completions, /v1/completions, "
                            "/v1/responses, /v1/embeddings (POST) and /v1/models (GET).")

    # ---- methods -------------------------------------------------------------
    def tenant(self):
        """Gateway mode: (tenant id, tenant) for this request's token, or None
        after the refusal was sent."""
        found = self.gw.authenticate(self.headers.get("Authorization"))
        if found is None:
            log(f"{self.command} refused: no valid token")
            self.send_api_error(401, "invalid_request_error", "Incorrect API key provided.", code="invalid_api_key")
        elif not found[1]["enabled"]:
            log(f"{self.command} refused: tenant {found[0]} is switched off")
            self.send_api_error(403, "invalid_request_error", "The model access of this bot is switched off",
                                code="tenant_disabled")
            found = None
        return found

    def do_GET(self):
        path = self.path.partition("?")[0]
        if path == "/healthz":
            return self.send_json(200, {"ok": True})
        if path == "/usage" and self.gw is None:
            return self.send_json(200, self.meter.snapshot())
        rest, query = self.route()
        if rest == "models" or (rest or "").startswith("models/"):
            if self.gw is None:
                return self.forward("GET", rest, query, None, None, False)
            found = self.tenant()   # answered here: a tenant sees its own list, not the upstream's
            if found is None:
                return None
            models = [{"id": m, "object": "model", "owned_by": "gateway"} for m in found[1]["models"]]
            if rest == "models":
                return self.send_json(200, {"object": "list", "data": models})
            for m in models:
                if m["id"] == rest[7:]:
                    return self.send_json(200, m)
            return self.send_api_error(404, "invalid_request_error", "This bot has no model with that id.",
                                       code="model_not_found")
        return self.unknown()

    def do_POST(self):
        rest, query = self.route()
        if rest not in POST_ROUTES:
            self.close_connection = True  # the body is not read
            return self.unknown()
        tid = tenant = None
        if self.gw is not None:
            found = self.tenant()
            if found is None:
                self.close_connection = True  # the body is not read
                return
            tid, tenant = found
        body = self.read_body()
        if body is None:
            return
        try:
            req = json.loads(body)
        except ValueError:
            req = None
        model = req["model"] if isinstance(req, dict) and isinstance(req.get("model"), str) else None
        if tenant is not None and model not in tenant["models"]:
            self.gw.count("model")
            log(f"POST {rest} tenant={tid} refused: model not in its list")
            if model is None:
                return self.send_api_error(400, "invalid_request_error",
                                           "The request must be JSON with a 'model' field.", code="model_required")
            return self.send_api_error(
                400, "invalid_request_error", f"The model '{model[:120]}' is not available to this bot. "
                "Available: " + (", ".join(tenant["models"]) or "none") + ".", code="model_not_allowed")

        who = f" tenant={tid}" if tid else ""
        refused = self.gw.admit(tid, tenant) if tenant is not None else self.meter.admit()
        if refused:
            if refused[0] == "exhausted":
                log(f"POST {rest}{who} refused: allowance used up")
                return self.send_exhausted(rest, req, refused[1])
            _, status, kind, message, headers = refused
            log(f"POST {rest}{who} refused: {kind}")
            return self.send_api_error(status, kind, message, headers)

        try:
            injected = False
            # Ask the upstream for token usage on streams when the client did not.
            if (isinstance(req, dict) and self.cfg.stream_usage and rest in STREAM_USAGE_ROUTES
                    and req.get("stream") is True):
                opts = req.get("stream_options")
                if not (isinstance(opts, dict) and opts.get("include_usage") is True):
                    req["stream_options"] = {**(opts if isinstance(opts, dict) else {}), "include_usage": True}
                    body, injected = json.dumps(req).encode(), True
            record = self.meter.record if tenant is None else (lambda *usage: self.gw.record(tid, *usage))
            self.forward("POST", rest, query, body, model, injected, record, who)
        finally:
            if tenant is not None:
                self.gw.done(tid)   # hands back the slot taken in admit()

    def read_body(self):
        """The request body, or None after an error reply was sent."""
        limit = self.cfg.max_body
        try:
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                parts, size = [], 0
                while True:
                    n = int(self.rfile.readline(1024).split(b";")[0].strip() or b"0", 16)
                    if n == 0:
                        while self.rfile.readline(8192).strip():  # trailers
                            pass
                        break
                    size += n
                    if size > limit:
                        raise OverflowError
                    parts.append(self.rfile.read(n))
                    self.rfile.readline(8)
                return b"".join(parts)
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n < 0:
                raise ValueError
            if n > limit:
                raise OverflowError
            body = self.rfile.read(n)
            if len(body) != n:
                raise ValueError
            return body
        except OverflowError:
            self.close_connection = True
            self.send_api_error(413, "invalid_request_error", "Request body too large for this proxy.")
        except ValueError:
            self.close_connection = True
            self.send_api_error(400, "invalid_request_error", "Malformed request body framing.")
        return None

    # ---- pass-through --------------------------------------------------------
    def forward(self, method, rest, query, body, model, injected, record=None, who=""):
        cfg, t0 = self.cfg, time.monotonic()
        target = f"{cfg.base_path}/{rest}" + (f"?{query}" if query else "")
        tries, waited = 0, 0.0
        while True:
            conn = None
            try:
                conn = cfg.connect()
                conn.putrequest(method, target, skip_accept_encoding=True)
                for k, v in self.headers.items():
                    lk = k.lower()
                    if lk not in DROP_REQUEST and (not cfg.gateway or lk in GATEWAY_REQUEST_HEADERS):
                        conn.putheader(k, v)
                conn.putheader("Accept-Encoding", "identity")  # so `usage` can be read as it passes
                if cfg.key:
                    conn.putheader("Authorization", "Bearer " + cfg.key)
                if body is not None:
                    conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException, ValueError) as e:
                if conn is not None:
                    conn.close()
                log(f"{method} {rest}{who} -> upstream unreachable ({type(e).__name__})")
                self.close_connection = True
                return self.send_api_error(502, "upstream_error", "The model endpoint could not be reached.")
            # A busy upstream is asked again here, while nothing has gone to
            # the client yet, so a passing limit of the provider does not
            # reach the agent as an error.
            if resp.status in RETRY_STATUS and tries < cfg.retries:
                wait = retry_after(resp)
                if wait is None:
                    wait = cfg.retry_backoff * 2 ** tries
                if waited + wait <= cfg.retry_max_wait:
                    conn.close()
                    tries, waited = tries + 1, waited + wait
                    cfg.sleep(wait)
                    continue
            break
        retried = f" after {tries} retries ({waited:.1f}s waiting)" if tries else ""

        if cfg.gateway and resp.status in GATEWAY_MASKED_STATUS:
            # About the shared upstream account (its key, balance or rate
            # limit), not about this tenant: the tenant gets a plain 503, never
            # a quota or key error that its agent would act on.
            conn.close()
            log(f"{method} {rest}{who} -> upstream {resp.status}{retried}; answered 503")
            return self.send_api_error(503, "server_error", "The model provider is not available at the moment",
                                       code="upstream_unavailable")

        status = resp.status
        is_sse = "text/event-stream" in (resp.getheader("Content-Type") or "").lower()
        length = resp.getheader("Content-Length")
        metered = record is not None and 200 <= status < 300
        self.send_response_only(status, resp.reason)
        for k, v in resp.getheaders():
            lk = k.lower()
            if lk not in DROP_RESPONSE and (not cfg.gateway or lk in GATEWAY_RESPONSE_HEADERS):
                self.send_header(k, v)
        if status in (204, 304) or status < 200:
            mode = "none"
        elif length is not None and not (is_sse and injected):
            self.send_header("Content-Length", length)
            mode = "length"
        elif self.request_version == "HTTP/1.0":
            self.send_header("Connection", "close")
            self.close_connection = True
            mode = "close"
        else:
            self.send_header("Transfer-Encoding", "chunked")
            mode = "chunked"
        self.end_headers()

        scan = SSEScanner(strip=injected) if (is_sse and metered) else None
        kept = bytearray() if (metered and not is_sse) else None
        note = ""

        def write(data):
            if mode == "chunked":
                data = b"%x\r\n%s\r\n" % (len(data), data)
            self.wfile.write(data)  # unbuffered: each piece leaves as it arrives

        try:
            while mode != "none":
                data = resp.read1(65536)
                if not data:
                    break
                if scan is not None:
                    data = scan.feed(data)
                elif kept is not None and len(kept) <= KEEP_JSON_BYTES:
                    kept += data
                if data:
                    write(data)
            if scan is not None:
                tail = scan.finish()
                if tail:
                    write(tail)
            if mode == "chunked":
                self.wfile.write(b"0\r\n\r\n")
        except (OSError, http.client.HTTPException) as e:
            self.close_connection = True
            note = f" interrupted ({type(e).__name__})"
        finally:
            conn.close()

        took = time.monotonic() - t0
        if not metered:
            return log(f"{method} {rest}{who} -> {status}{retried}{note} {took:.1f}s")
        usage, estimated = None, False
        if scan is not None:
            usage = scan.usage
        elif len(kept) <= KEEP_JSON_BYTES:
            try:
                usage = usage_tokens(json.loads(bytes(kept)))
            except ValueError:
                usage = None
        if usage is None:
            # The upstream sent no usage: a rough count, so the cap still bites.
            estimated = True
            usage = (len(body or b"") // 4, scan.chunks if scan is not None else len(kept) // 4, 0)
        record(model, usage[0], usage[1], estimated, usage[2])
        log(f"{method} {rest}{who} -> {status}{' stream' if is_sse else ''} prompt={usage[0]} completion={usage[1]}"
            f"{f' cached={usage[2]}' if usage[2] else ''}{' (estimated)' if estimated else ''}{retried}{note} {took:.1f}s")


class AdminHandler(BaseHTTPRequestHandler):
    """Gateway mode, second listener: the tenant table and the usage report.
    Nothing here is served by the main listener."""
    protocol_version = "HTTP/1.1"
    server_version = "modelproxy"
    sys_version = ""
    timeout = 60
    cfg = None
    gw = None

    def log_message(self, fmt, *args):
        pass

    send_json = Handler.send_json

    def fail(self, status, message):
        self.send_json(status, {"ok": False, "error": message})

    def allowed(self):
        want = self.cfg.admin_token
        scheme, _, got = (self.headers.get("Authorization") or "").strip().partition(" ")
        if not want or (scheme.lower() == "bearer" and hmac.compare_digest(got.strip().encode(), want.encode())):
            return True
        self.close_connection = True
        self.fail(401, "the admin token is missing or wrong")
        return False

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/healthz":
            return self.send_json(200, {"ok": True})
        if path not in ("/admin/usage", "/admin/tenants"):
            return self.fail(404, "unknown admin endpoint")
        if not self.allowed():
            return None
        if path == "/admin/tenants":
            return self.send_json(200, self.gw.export())
        days = urllib.parse.parse_qs(query).get("days", ["7"])[0]
        return self.send_json(200, self.gw.usage(int(days) if days.isdigit() else 7))

    def do_PUT(self):
        self.close_connection = True
        if self.path.partition("?")[0] != "/admin/tenants":
            return self.fail(404, "unknown admin endpoint")
        if not self.allowed():
            return None
        try:
            n = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_ADMIN_BODY:
            return self.fail(413 if n > 0 else 411, "a Content-Length of at most 8 MB is required")
        body = self.rfile.read(n)
        try:
            if len(body) != n:
                raise ValueError
            obj = json.loads(body)
        except ValueError:
            return self.fail(400, "the body is not valid JSON")
        try:
            return self.send_json(200, self.gw.replace(obj))
        except StateError as e:
            log("tenant table refused (the one in force stays)")
            return self.fail(400, str(e))
        except OSError as e:
            log(f"tenant table could not be stored ({type(e).__name__}); the one in force stays")
            return self.fail(500, "the table could not be stored; the one in force stays")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


def make_server(cfg, meter=None, gateway=None):
    """The main listener. Room mode: with a Meter. Gateway mode: with a Gateway."""
    if cfg.gateway:
        gateway = gateway or Gateway(cfg)
    else:
        meter = meter or Meter(cfg)
    handler = type("BoundHandler", (Handler,), {"cfg": cfg, "meter": meter, "gw": gateway})
    return Server((cfg.listen_host, cfg.listen_port), handler)


def make_admin_server(cfg, gateway):
    handler = type("BoundAdminHandler", (AdminHandler,), {"cfg": cfg, "gw": gateway})
    return Server((cfg.admin_host, cfg.admin_port), handler)


def main():
    admin = None
    try:
        cfg = Config(os.environ)
        gateway = Gateway(cfg) if cfg.gateway else None
        srv = make_server(cfg, gateway=gateway)
        if gateway:
            admin = make_admin_server(cfg, gateway)
    except ConfigError as e:
        sys.exit(f"[modelproxy] {e}; refusing to start")
    except OSError as e:
        sys.exit(f"[modelproxy] cannot start ({type(e).__name__}: is the data directory writable and the port free?)")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if gateway:
        table = gateway.export()
        log(f"gateway mode: listening on {cfg.listen_host}:{cfg.listen_port}, admin on "
            f"{cfg.admin_host}:{cfg.admin_port} (admin token {'set' if cfg.admin_token else 'not set'}); "
            f"upstream {cfg.scheme}://{cfg.host}; key {'set' if cfg.key else 'not set'}; "
            f"{len(table['tenants'])} tenants in the stored table")
        if not gateway.loaded_table:
            log("note: no tenant table yet; every request is refused until PUT /admin/tenants")
        threading.Thread(target=admin.serve_forever, daemon=True).start()
    else:
        caps = ", ".join(f"{k}={v}" for k, v in cfg.limits().items() if v) or "none"
        log(f"listening on {cfg.listen_host}:{cfg.listen_port}; upstream {cfg.scheme}://{cfg.host}; "
            f"key {'set' if cfg.key else 'not set'}; limits: {caps}")
    if cfg.scheme == "http" and cfg.key:
        log("note: MODEL_BASE_URL is plain http; the key travels unencrypted to that host")
    srv.serve_forever()


if __name__ == "__main__":
    main()
