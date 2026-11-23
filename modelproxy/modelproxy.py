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
        GET /admin/requests  one record per request, no content (?tenant=&since=&limit=)
    A room-mode proxy can point at a gateway (MODEL_BASE_URL = the gateway,
    MODEL_API_KEY = the bot's token) like at any other endpoint.

Endpoints on the main listener (both roles):
  POST /v1/chat/completions, /v1/completions, /v1/responses, /v1/embeddings
  GET  /v1/models, /v1/models/<id>   room: passed through; gateway: the tenant's own list
  GET  /usage                        room only: token counters, limits, what is left (JSON)
  GET  /usage/requests               room only: one record per request, no content (?since=&limit=)
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

Time limits: no first answer byte within MODELPROXY_FIRST_BYTE_TIMEOUT (30 s)
gives the attempt up and asks once more (45 s), then 504; no byte for
MODELPROXY_STALL_TIMEOUT (60 s) once an answer started ends it. Off by default
in a room proxy whose upstream is a Fugal gateway (the gateway does it).

Malformed tool calls (both roles): a chat completion request that offers tools
gets its answer checked before it reaches the client (see ToolCallCheck). An
answer whose tool call is broken (arguments that are not a JSON object, no tool
name, the model's own special-token markup, a stream cut off inside a tool
call) is thrown away and the same request is sent again, at most
MODELPROXY_MALFORMED_RETRIES times (default 2; 0 = off); if every attempt is
broken the last one is passed on as it is. To be able to do that, a stream is
held back until it carries a tool call (then to its end) or more than
MODELPROXY_TOOLCALL_HOLD_CHARS characters of text (default 600; then it is
released and streams on with no further check). Nothing is retried once a
byte of the answer has gone to the client. Only the answer passed on is
billed; thrown-away attempts are counted apart (malformed_* and discarded_*
counters). A gateway marks the answers it checked (CHECKED_HEADER) so that a
room proxy behind it does not hold them back a second time.

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

CHECKED_HEADER = "X-Modelproxy-Toolcalls"   # "checked": a gateway already checked this answer
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
              "te", "trailer", "trailers", "transfer-encoding", "upgrade"}
# Not forwarded upstream: hop-by-hop headers, and every credential a client
# may send (the agent only has a placeholder; the real key is added here).
DROP_REQUEST = HOP_BY_HOP | {"host", "content-length", "authorization", "accept-encoding", "expect",
                             "cookie", "x-api-key", "api-key"}
DROP_RESPONSE = HOP_BY_HOP | {"content-length", CHECKED_HEADER.lower()}
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
MAX_HOLD_BYTES = 8 * 1024 * 1024    # an answer held back for the tool-call check is released past this size
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
# What the user is told (OpenClaw puts "LLM request rejected: " in front): the
# free allowance is used up, when it comes back, and what to do now. Deliberately
# without any punctuation: OpenClaw's web
# chat shows an error text with a backslash before every punctuation mark.
# And without words agent frameworks read as "retry soon" or as a billing
# failure of the key (rate limit, quota, exceeded, credits, ...).
USED_UP_TODAY = ("The free allowance for today is used up and it comes back at midnight UTC "
                 "To keep going now add your own model key in Settings on the Fugal Bot page")
USED_UP_TOTAL = ("The free allowance for this bot is used up "
                 "To keep going add your own model key in Settings on the Fugal Bot page")
USED_UP_PLATFORM = ("The free allowance shared by all bots is used up for today and it comes back at midnight UTC "
                    "To keep going now add your own model key in Settings on the Fugal Bot page")


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


def _seconds(env, name, default):
    """Seconds of zero or more where 0 means off; unset = `default`."""
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number of seconds") from None
    if not math.isfinite(v) or v < 0:
        raise ConfigError(f"{name} must be zero or more")
    return v


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

        # Upstream 429 / 503: how often to try again, and the longest total wait
        # (seconds) a request may spend waiting for them with nothing sent.
        self.retries = _count(env, "MODELPROXY_UPSTREAM_RETRIES", 3 if self.gateway else 0)
        self.retry_max_wait = _number(env, "MODELPROXY_RETRY_MAX_WAIT", float) or 10.0
        # A room proxy whose upstream is a Fugal gateway (its key is a gateway
        # token) leaves the first-byte and stall timeouts to the gateway, so
        # the waits are not doubled. Explicit settings still win.
        self.upstream_is_gateway = not self.gateway and bool(TOKEN.match(self.key))
        quiet = self.upstream_is_gateway
        # No first answer byte (for a stream: no `data:` event) within this many
        # seconds: the attempt is given up and the request asked again once,
        # with FIRST_BYTE_RETRY_FACTOR times the time; then a 504. 0 = off.
        self.first_byte_timeout = _seconds(env, "MODELPROXY_FIRST_BYTE_TIMEOUT", 0.0 if quiet else 30.0)
        self.first_byte_retry_factor = 1.5
        # No byte at all for this many seconds once the answer has started:
        # the answer is ended (asked again while nothing has gone out). 0 = off.
        self.stall_timeout = _seconds(env, "MODELPROXY_STALL_TIMEOUT", 0.0 if quiet else 60.0)
        # One line per request in requests-<day>.jsonl (no content): kept this
        # many days, each day's file at most this many MB.
        self.request_log_days = _count(env, "MODELPROXY_REQUEST_LOG_DAYS", 35)
        self.request_log_bytes = int((_number(env, "MODELPROXY_REQUEST_LOG_MB", float) or 20) * 1024 * 1024)
        self.retry_backoff = 1.0      # first wait when the upstream names none; doubles each time
        self.sleep = time.sleep
        # Broken tool calls in answers to requests that offer tools: how often
        # to ask again (0 = no check, nothing held back), and how much text a
        # stream may carry before it is released unchecked.
        self.malformed_retries = _count(env, "MODELPROXY_MALFORMED_RETRIES", 2)
        self.hold_chars = _count(env, "MODELPROXY_TOOLCALL_HOLD_CHARS", 600)
        self.keepalive_seconds = 15.0  # an SSE comment to the client while a stream is held back

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
    """One row of counters. `requests` .. `estimated_requests` are the answers
    passed on to the client (what is billed). The rest are about broken tool
    calls (see ToolCallCheck): `malformed_detected` broken answers seen,
    `malformed_retried` requests sent again because of one, `malformed_forwarded`
    broken answers passed on all the same (every attempt broken, or found after
    the answer had started to stream), and `discarded_*` the tokens and USD of
    the attempts thrown away (never part of `usd`)."""
    return {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "cached_prompt_tokens": 0, "usd": 0.0, "estimated_requests": 0,
            "malformed_detected": 0, "malformed_retried": 0, "malformed_forwarded": 0,
            "discarded_requests": 0, "discarded_prompt_tokens": 0, "discarded_cached_prompt_tokens": 0,
            "discarded_completion_tokens": 0, "discarded_usd": 0.0,
            "first_byte_timeouts": 0, "first_byte_retries": 0, "first_byte_failures": 0, "stalls": 0}


# Events counted per request (see Handler.forward): `first_byte_timeouts`
# attempts given up for no first byte (their cost is not known: the upstream
# may bill a prompt it never answered), `first_byte_retries` requests asked
# again because of one, `first_byte_failures` requests answered 504 after the
# second wait too, `stalls` answers that stopped part way.
EVENTS = ("first_byte_timeouts", "first_byte_retries", "first_byte_failures", "stalls")


def add_malformed(row, detected, retried, forwarded, discarded):
    """Adds the broken-answer counters of one client request to a row.
    `discarded` is [(prompt, completion, cached, usd)] of the attempts thrown away."""
    row["malformed_detected"] += detected
    row["malformed_retried"] += retried
    row["malformed_forwarded"] += forwarded
    for prompt, completion, cached, usd in discarded:
        row["discarded_requests"] += 1
        row["discarded_prompt_tokens"] += prompt
        row["discarded_cached_prompt_tokens"] += min(cached, prompt)
        row["discarded_completion_tokens"] += completion
        row["discarded_usd"] = round(row["discarded_usd"] + usd, 8)


class Meter:
    """Per-day (UTC) and total counters, kept in one small JSON file."""

    def __init__(self, cfg, clock=time.time):
        self.cfg, self.clock = cfg, clock
        self.path = os.path.join(cfg.data_dir, "usage.json")
        self.lock = threading.Lock()
        self.total, self.days = _zero(), {}
        self.rejected = {"budget": 0, "rate_limit": 0}   # since this start
        self.recent = collections.deque()                # request times, for the per-minute limit
        self.requests = RequestLog(cfg, clock)
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
        return usd

    def record_malformed(self, model, detected, retried, forwarded, discarded):
        """Broken answers of one request; `discarded` is [(prompt, completion, cached)]
        of the attempts thrown away. Counted apart: never part of `usd` or a cap."""
        price = self.cfg.price(model)
        rows = [(p, c, k, cost_usd(price, p, c, k)) for p, c, k in discarded]
        with self.lock:
            for row in (self.total, self.days.setdefault(self.today(), _zero())):
                add_malformed(row, detected, retried, forwarded, rows)
            try:
                self._save()
            except OSError as e:
                log(f"could not write the usage file ({type(e).__name__}); counting in memory")

    def count_events(self, events):
        """Adds {event name: n} (EVENTS) to today's row and the total."""
        if not events:
            return
        with self.lock:
            for row in (self.total, self.days.setdefault(self.today(), _zero())):
                for name, n in events.items():
                    row[name] += n
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


def platform_spent(row):
    """What the platform's daily cap is measured against: the billed answers plus
    the attempts thrown away for a broken tool call (the upstream bills both)."""
    return row["usd"] + row.get("discarded_usd", 0.0)


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
        self.requests = RequestLog(cfg, clock)
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
        return day["tenants"].get(tid, _zero())["usd"], platform_spent(day["platform"])

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
        return usd

    def record_malformed(self, tid, model, detected, retried, forwarded, discarded):
        """Broken answers of one request (see Meter.record_malformed). The
        thrown-away attempts never count against the tenant's allowance; their
        USD does count against the platform's daily cap (the shared upstream
        account pays for them)."""
        with self.lock:
            price = price_of(self.state["prices"], model)
            rows = [(p, c, k, cost_usd(price, p, c, k)) for p, c, k in discarded]
            today = self.today()
            day = self.days.setdefault(today, {"platform": _zero(), "tenants": {}})
            for row in (day["platform"], day["tenants"].setdefault(tid, _zero())):
                add_malformed(row, detected, retried, forwarded, rows)
            try:
                self._save_day(today)
            except OSError as e:
                log(f"could not write the usage file ({type(e).__name__}); counting in memory")

    def count_events(self, tid, events):
        """Adds {event name: n} (EVENTS) to the tenant's and the platform's row of today."""
        if not events:
            return
        with self.lock:
            today = self.today()
            day = self.days.setdefault(today, {"platform": _zero(), "tenants": {}})
            for row in (day["platform"], day["tenants"].setdefault(tid, _zero())):
                for name, n in events.items():
                    row[name] += n
            try:
                self._save_day(today)
            except OSError as e:
                log(f"could not write the usage file ({type(e).__name__}); counting in memory")

    def price(self, model):
        with self.lock:
            return price_of(self.state["prices"], model)

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
                                 "remaining_usd": left(cap, platform_spent(now["platform"])),
                                 "exceeded": cap is not None and platform_spent(now["platform"]) >= cap,
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


# The special-token markup of DeepSeek-style chat templates (FULLWIDTH VERTICAL
# LINE U+FF5C inside angle brackets, LOWER ONE EIGHTH BLOCK U+2581 between
# words), which a broken answer leaks into tool-call arguments, e.g.
# `</\uff5cDSML\uff5c>` or `<\uff5ctool\u2581call\u2581begin\uff5c>`. Plain `<`, `|` and
# `>` in a command never match.
MARKUP = re.compile("\uff5cDSML\uff5c|<\uff5c|</\uff5c|\uff5c>|tool\u2581call|end\u2581of\u2581sentence")
TEXT_KEYS = ("content", "reasoning_content", "reasoning")

# Degenerate output inside tool-call arguments (a model that has started to
# loop or to produce garbage). Kept narrow on purpose; a false alarm costs at
# most MODELPROXY_MALFORMED_RETRIES extra calls, because when every attempt
# looks the same the last one is passed on.
#   - a unit of 2 to 80 characters with at least 2 letters, repeated 16 times
#     or more in a row and 80 characters or more in all ("the the the ...",
#     the same line again and again); units that are common data literals
#     (null, NaN, true, ...) do not count;
#   - one letter 60 times or more in a row, unless it sits in a token with
#     digits, "+", "/" or "=" (base64 and hex blobs carry runs of "A" or "0");
#   - a word (letters without a break) of 6 letters or more mixing three or
#     more scripts: Latin, CJK (Han, kana together: Japanese mixes them),
#     Hangul, Cyrillic, Arabic, Hebrew, Thai, Devanagari. Greek is left out
#     (maths). Two scripts in a word ("使用Python") are normal.
REPEAT_UNIT = re.compile(r"(.{2,80}?)\1{15,}", re.S)
REPEAT_LETTER = re.compile(r"([^\W\d_])\1{59,}")
DATA_LITERALS = {"null", "none", "nan", "true", "false", "undefined", "na", "nil", "n/a", "x", "xx", "xxx",
                 "todo", "tbd", "0x", "yes", "no", "on", "off"}
SCAN_CHARS = 64 * 1024      # of each argument string, at most (keeps the check under ~0.2 s)
_SCRIPTS = (("cjk", (0x2E80, 0x2FDF), (0x3040, 0x30FF), (0x3100, 0x312F), (0x31A0, 0x31FF), (0x3400, 0x4DBF),
             (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0xFF66, 0xFF9F), (0x20000, 0x2FA1F)),
            ("hangul", (0x1100, 0x11FF), (0x3130, 0x318F), (0xAC00, 0xD7AF)),
            ("cyrillic", (0x0400, 0x052F)), ("arabic", (0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF),
                                               (0xFE70, 0xFEFF)),
            ("hebrew", (0x0590, 0x05FF)), ("thai", (0x0E00, 0x0E7F)), ("devanagari", (0x0900, 0x097F)))


def _script(ch):
    o = ord(ch)
    if o < 0x250:
        return "latin" if ch.isalpha() else None
    for name, *ranges in _SCRIPTS:
        if any(a <= o <= b for a, b in ranges):
            return name
    return None


def degenerate(text):
    """A reason if `text` (one decoded argument string) is clearly degenerate, else None."""
    text = text[:SCAN_CHARS]
    for m in REPEAT_UNIT.finditer(text):
        unit = m.group(1)
        if len(m.group(0)) < 80 or sum(c.isalpha() for c in unit) < 2:
            continue
        if len(set(unit)) == 1:     # a single character: the letter rule below decides
            continue
        core = re.sub(r"[\W_]+", "", unit.lower())
        if core in DATA_LITERALS or unit.strip(" ,;|\t\n\"'").lower() in DATA_LITERALS:
            continue
        return "degenerate repetition in a tool call"
    for m in REPEAT_LETTER.finditer(text):
        a, b = m.start(), m.end()
        while a > 0 and not text[a - 1].isspace():
            a -= 1
        while b < len(text) and not text[b].isspace():
            b += 1
        if not re.search(r"[0-9+/=]", text[a:b]):
            return "degenerate repetition in a tool call"
    for word in re.findall(r"[^\W\d_]{6,}", text):
        scripts = {_script(c) for c in word} - {None}
        if len(scripts) >= 3:
            return "mixed-script garbage in a tool call"
    return None


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def offers_tools(req):
    """True for a chat completion request that offers the model tools."""
    return isinstance(req, dict) and any(isinstance(req.get(k), list) and req[k] for k in ("tools", "functions"))


class ToolCallCheck:
    """Watches one chat completion answer (stream events, or a whole JSON reply)
    to a request that offered tools, and says what, if anything, makes its
    tool calls unusable (problem()). Text is only counted, and searched for
    the model's markup."""

    def __init__(self):
        self.calls = {}       # (choice, index) -> [name, arguments] as streamed
        self.finish = {}      # choice -> finish_reason
        self.text = 0         # characters of text and reasoning so far
        self.tail = {}        # choice -> the last few text characters (markup split across chunks)
        self.markup = False   # markup in the text
        self.done = False     # `data: [DONE]` seen

    @property
    def tool_seen(self):
        return bool(self.calls)

    def payload(self, raw):
        """One SSE `data:` payload (bytes)."""
        if raw == b"[DONE]":
            self.done = True
            return
        try:
            obj = json.loads(raw)
        except ValueError:
            return
        for ch in (obj.get("choices") if isinstance(obj, dict) else None) or []:
            if isinstance(ch, dict):
                self._choice(ch, ch.get("delta"))

    def message(self, obj):
        """A whole (non-streamed) chat completion."""
        for ch in (obj.get("choices") if isinstance(obj, dict) else None) or []:
            if isinstance(ch, dict):
                self._choice(ch, ch.get("message"))

    def _choice(self, ch, part):
        i = ch.get("index", 0)
        if ch.get("finish_reason"):
            self.finish[i] = ch["finish_reason"]
        if not isinstance(part, dict):
            return
        for key in TEXT_KEYS:
            v = part.get(key)
            if isinstance(v, str) and v:
                self.text += len(v)
                seen = self.tail.get(i, "") + v
                if MARKUP.search(seen):
                    self.markup = True
                self.tail[i] = seen[-24:]
        calls = part.get("tool_calls")
        if isinstance(part.get("function_call"), dict):     # the legacy single call
            calls = [{"index": 0, "function": part["function_call"]}]
        for n, tc in enumerate(calls if isinstance(calls, list) else []):
            if not isinstance(tc, dict):
                continue
            slot = self.calls.setdefault((i, tc.get("index", n)), ["", ""])
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            if isinstance(fn.get("name"), str):
                slot[0] += fn["name"]
            if isinstance(fn.get("arguments"), str):
                slot[1] += fn["arguments"]
            elif isinstance(fn.get("arguments"), dict):     # some servers send an object
                slot[1] += json.dumps(fn["arguments"])

    def problem(self, stream, interrupted=False):
        """Why this answer's tool calls cannot be used, or None if they can."""
        if interrupted:
            return "the answer was cut off"
        for name, args in self.calls.values():
            if not name.strip():
                return "a tool call without a name"
            if MARKUP.search(name) or MARKUP.search(args):
                return "model markup in a tool call"
            if args.strip():
                try:
                    value = json.loads(args)
                except ValueError:
                    return "tool-call arguments are not valid JSON"
                if not isinstance(value, dict):
                    return "tool-call arguments are not a JSON object"
                if MARKUP.search(json.dumps(value, ensure_ascii=False)):
                    return "model markup in a tool call"
                for s in _strings(value):
                    why = degenerate(s)
                    if why:
                        return why
        if self.markup:
            return "model markup in the text"
        if self.calls:
            if "length" in self.finish.values():
                return "a tool call cut off at the length limit"
            if stream and not self.done:
                return "the stream ended inside a tool call"
            if stream and not self.finish:
                return "the stream ended without a finish reason"
        return None


class SSEScanner:
    """Reads a server-sent-event stream as it passes through and remembers the
    last `usage` it saw. With strip=True (the proxy added
    stream_options.include_usage itself) the usage-only chunk (`"choices": []`)
    is removed; for that, bytes are released one complete event at a time.
    With strip=False every byte is released at once, unchanged."""

    def __init__(self, strip, observer=None):
        self.strip, self.buf = strip, b""
        self.usage, self.chunks = None, 0
        self.observer = observer   # called with every `data:` payload (bytes), e.g. ToolCallCheck.payload

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
            if payload and self.observer is not None:
                self.observer(payload)
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


class RequestLog:
    """One JSON line per request that went upstream, in requests-<UTC day>.jsonl
    in the data directory, for a later "what did this task cost" view and for
    diagnosis: times, tokens, cost, outcome. Never content, headers or keys.
    Files older than `days` are deleted; a day's file stops growing at
    `max_bytes` (noted once in the log)."""

    NAME = re.compile(r"requests-(\d{4}-\d{2}-\d{2})\.jsonl\Z")

    def __init__(self, cfg, clock=time.time):
        self.dir, self.days, self.max_bytes, self.clock = cfg.data_dir, cfg.request_log_days, cfg.request_log_bytes, clock
        self.lock = threading.Lock()
        self.full = set()
        self.pruned = None

    def _path(self, day):
        return os.path.join(self.dir, f"requests-{day}.jsonl")

    def _prune(self, today, ts):
        if self.pruned == today:
            return
        self.pruned = today
        first = utc_day(ts - (max(1, self.days) - 1) * 86400)    # the oldest day kept, today included
        for name in os.listdir(self.dir):
            m = self.NAME.match(name)
            if m and m.group(1) < first:
                try:
                    os.remove(os.path.join(self.dir, name))
                except OSError:
                    pass

    def add(self, entry):
        if not self.days:
            return
        line = (json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n").encode()
        with self.lock:
            ts = entry.get("ts") or self.clock()
            day = utc_day(ts)
            path = self._path(day)
            try:
                self._prune(day, ts)
                if os.path.exists(path) and os.path.getsize(path) + len(line) > self.max_bytes:
                    if day not in self.full:
                        self.full.add(day)
                        log(f"request log for {day} is full ({self.max_bytes // (1024 * 1024)} MB); later requests "
                            "of the day are only counted")
                    return
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "ab") as f:
                    f.write(line)
            except OSError as e:
                log(f"could not write the request log ({type(e).__name__})")

    def read(self, since=0.0, limit=200, tenant=None):
        """Entries with ts >= since (and of `tenant`, if given), oldest first, at most `limit` (the newest)."""
        out = []
        first = utc_day(since) if since > 0 else ""
        with self.lock:
            names = sorted(n for n in os.listdir(self.dir) if self.NAME.match(n) and self.NAME.match(n).group(1) >= first)
            for name in names:
                try:
                    with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                        for raw in f:
                            try:
                                e = json.loads(raw)
                            except ValueError:
                                continue
                            if e.get("ts", 0) >= since and (tenant is None or e.get("tenant") == tenant):
                                out.append(e)
                except OSError:
                    continue
        return out[-limit:] if limit else out


def query_params(query):
    """(since, limit, tenant) from a query string: since = unix seconds (0),
    limit = 1..5000 (200)."""
    q = urllib.parse.parse_qs(query)
    try:
        since = max(0.0, float(q.get("since", ["0"])[0]))
    except ValueError:
        since = 0.0
    if not math.isfinite(since):
        since = 0.0
    limit = q.get("limit", ["200"])[0]
    limit = min(5000, max(1, int(limit))) if limit.isdigit() else 200
    tenant = q.get("tenant", [None])[0]
    return since, limit, (tenant if tenant and TENANT_ID.match(tenant) else None)


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
        if path == "/usage/requests" and self.gw is None:
            since, limit, _ = query_params(self.path.partition("?")[2])
            return self.send_json(200, {"requests": self.meter.requests.read(since, limit)})
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
            malformed = (self.meter.record_malformed if tenant is None
                         else (lambda *counts: self.gw.record_malformed(tid, *counts)))
            guard = rest == "chat/completions" and self.cfg.malformed_retries > 0 and offers_tools(req)
            self.forward("POST", rest, query, body, model, injected, record, who, guard, malformed, tid)
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
    def forward(self, method, rest, query, body, model, injected, record=None, who="", guard=False,
                malformed=None, tid=None):
        """Sends the request upstream and the answer back. With `guard` (a chat
        completion request that offers tools) a broken tool call is asked again
        while nothing of the answer has gone to the client (see ToolCallCheck);
        `malformed` then receives the counters of the broken attempts.

        Waits are bounded: no first answer byte within MODELPROXY_FIRST_BYTE_TIMEOUT
        (for a stream: no `data:` event) gives that attempt up and asks once more
        with 1.5 times the time, then answers 504; no byte for
        MODELPROXY_STALL_TIMEOUT once an answer has started ends it (asked again
        if nothing of it has gone out and it was a held tool-call answer)."""
        cfg, t0 = self.cfg, time.monotonic()
        store = self.gw if self.gw is not None else self.meter
        started = store.clock() if store is not None else time.time()
        target = f"{cfg.base_path}/{rest}" + (f"?{query}" if query else "")
        attempts = 1 + (cfg.malformed_retries if guard else 0)
        self.out_mode = None            # how the body is framed, once the status line has gone out
        self.wlock = threading.RLock()  # the keep-alive thread writes too
        self.keepalive = None
        bad = []          # broken attempts, held back and not sent (the last one may still be)
        final = None
        events = collections.Counter()
        wait = cfg.first_byte_timeout
        first_byte_tries = 1 if wait else 0

        def finish(status, error, a=None, usage=None, estimated=False, usd=0.0, discarded_usd=0.0, detected=0):
            took = time.monotonic() - t0
            if record is not None and store is not None:
                if events:
                    if self.gw is not None:
                        self.gw.count_events(tid, dict(events))
                    else:
                        self.meter.count_events(dict(events))
                entry = {"ts": round(started, 3), "route": rest, "model": model, "status": status,
                         "duration_s": round(took, 3),
                         "ttfb_s": round(a.first_data - t0, 3) if a is not None and a.first_data else None,
                         "stream": bool(a is not None and a.is_sse), "error": error,
                         "prompt_tokens": usage[0] if usage else 0, "cached_prompt_tokens": usage[2] if usage else 0,
                         "completion_tokens": usage[1] if usage else 0, "usd": round(usd, 8), "estimated": estimated,
                         "malformed": detected, "malformed_retried": len(bad), "discarded_usd": round(discarded_usd, 8),
                         "first_byte_timeouts": events["first_byte_timeouts"],
                         "stalled": bool(a is not None and a.stalled), "interrupted": bool(a is not None and a.interrupted)}
                if tid is not None:
                    entry["tenant"] = tid
                store.requests.add(entry)
            return took

        try:
            while True:
                opened = self.open_upstream(method, target, body, rest, who, wait)
                a = None
                if opened is not TIMED_OUT:
                    if opened is None or (cfg.gateway and opened[1].status in GATEWAY_MASKED_STATUS) \
                            or (bad and not 200 <= opened[1].status < 300):
                        # No usable answer this time: a broken one held from before is
                        # better than an error (OpenClaw copes with it as it always did).
                        if opened is not None:
                            opened[0].close()
                        if bad:
                            final = bad[-1]
                            log(f"{method} {rest}{who} -> asking again failed; the broken answer is passed on")
                            break
                        if opened is None:
                            self.close_connection = True
                            finish(502, "upstream_unreachable")
                            return self.send_api_error(502, "upstream_error", "The model endpoint could not be reached.")
                        # About the shared upstream account (its key, balance or rate
                        # limit), not about this tenant: the tenant gets a plain 503, never
                        # a quota or key error that its agent would act on.
                        log(f"{method} {rest}{who} -> upstream {opened[1].status}{opened[2]}; answered 503")
                        finish(503, f"upstream_{opened[1].status}")
                        return self.send_api_error(503, "server_error",
                                                   "The model provider is not available at the moment",
                                                   code="upstream_unavailable")
                    conn, resp, retried, sock, deadline = opened
                    metered = record is not None and 200 <= resp.status < 300
                    checked = guard and metered and not (not cfg.gateway and resp.getheader(CHECKED_HEADER))
                    a = Attempt(conn, resp, retried, injected, metered, checked, sock, deadline)
                    hold = checked and len(bad) + 1 < attempts
                    if hold and a.is_sse and self.keepalive is None and cfg.keepalive_seconds:
                        self.keepalive = KeepAlive(self, a, cfg.keepalive_seconds)
                    self.relay(a, hold)
                if a is None or a.first_byte_timeout:
                    # Nothing of this attempt went out (comments aside): give it up.
                    events["first_byte_timeouts"] += 1
                    if first_byte_tries:
                        first_byte_tries -= 1
                        events["first_byte_retries"] += 1
                        log(f"{method} {rest}{who} -> no first byte in {wait:g}s; asking again "
                            f"(waiting up to {wait * cfg.first_byte_retry_factor:g}s)")
                        wait *= cfg.first_byte_retry_factor
                        continue
                    events["first_byte_failures"] += 1
                    if bad:
                        final = bad[-1]
                        log(f"{method} {rest}{who} -> no first byte in {wait:g}s again; the broken answer is passed on")
                        break
                    took = finish(504, "first_byte_timeout")
                    log(f"{method} {rest}{who} -> no first byte in {wait:g}s again; answered 504 {took:.1f}s")
                    return self.send_timeout()
                final = a
                if a.stalled:
                    events["stalls"] += 1
                    log(f"{method} {rest}{who} -> the answer stalled (no byte for {cfg.stall_timeout:g}s)"
                        + (" after part of it had gone out; the stream was closed" if a.sent else ""))
                if a.sent or not a.problem or len(bad) + 1 == attempts:
                    break
                bad.append(a)
                log(f"{method} {rest}{who} -> malformed answer ({a.problem}); asking again "
                    f"({len(bad)} of {attempts - 1})")
            if not final.sent:
                self.release(final)
        finally:
            self.stop_keepalive()
        if final.interrupted:
            self.close_connection = True

        ttfb = f" ttfb={final.first_data - t0:.1f}s" if final.first_data else ""
        waits = f" first-byte-retries={events['first_byte_retries']}" if events["first_byte_retries"] else ""
        if not final.metered:
            took = finish(final.status, None, final)
            return log(f"{method} {rest}{who} -> {final.status}{final.retried}{final.note}{waits} {took:.1f}s{ttfb}")
        usage, estimated = final.usage(body)
        usd = record(model, usage[0], usage[1], estimated, usage[2]) or 0.0
        discarded = [b.usage(body)[0] for b in bad if b is not final]
        price = store.price(model) if self.gw is not None else cfg.price(model)
        discarded_usd = sum(cost_usd(price, p, c, k) for p, c, k in discarded)
        detected = len(bad) + (1 if final.problem and final not in bad else 0)
        if final.problem:
            log(f"{method} {rest}{who} -> malformed answer ({final.problem}) passed on"
                + (" (it had started to stream)" if final.streamed_early else ""))
        if detected and malformed is not None:
            malformed(model, detected, len(bad), 1 if final.problem else 0, discarded)
        took = finish(final.status, "stalled" if final.stalled else None, final, usage, estimated, usd,
                      discarded_usd, detected)
        log(f"{method} {rest}{who} -> {final.status}{' stream' if final.is_sse else ''} prompt={usage[0]} "
            f"completion={usage[1]}{f' cached={usage[2]}' if usage[2] else ''}{' (estimated)' if estimated else ''}"
            f"{f' after {len(bad)} malformed' if bad else ''}{final.retried}{waits}{final.note} {took:.1f}s{ttfb}")

    def send_timeout(self):
        """The answer when the upstream did not start to answer, twice. A JSON 504
        if nothing has gone out; inside an SSE stream whose head a keep-alive
        already sent, an error event, and the stream ends."""
        message = "The model did not start to answer in time Please send your message again"
        self.close_connection = True
        with self.wlock:
            if self.out_mode is None:
                return self.send_api_error(504, "timeout_error", message, code="upstream_timeout")
            err = {"error": {"message": message, "type": "timeout_error", "param": None, "code": "upstream_timeout"}}
            try:
                self.write_body(b"data: " + json.dumps(err).encode() + b"\n\n")
                self.end_body()
            except OSError:
                pass

    def open_upstream(self, method, target, body, rest, who, wait=0.0):
        """(connection, response, note on retries, socket, first-byte deadline)
        for one attempt, asking again on a busy upstream (429 / 503) while
        nothing has gone to the client; None if the upstream could not be
        reached; TIMED_OUT if it sent no answer head within `wait` seconds."""
        cfg = self.cfg
        tries, waited = 0, 0.0
        while True:
            conn, connected = None, False
            try:
                conn = cfg.connect()
                connected = True
                sock = conn.sock
                deadline = time.monotonic() + wait if wait else None
                if deadline is not None:
                    sock.settimeout(wait)
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
            except TimeoutError:
                if conn is not None:
                    conn.close()
                if connected and wait:
                    return TIMED_OUT
                log(f"{method} {rest}{who} -> upstream unreachable (TimeoutError)")
                return None
            except (OSError, http.client.HTTPException, ValueError) as e:
                if conn is not None:
                    conn.close()
                log(f"{method} {rest}{who} -> upstream unreachable ({type(e).__name__})")
                return None
            # A busy upstream is asked again here, while nothing has gone to
            # the client yet, so a passing limit of the provider does not
            # reach the agent as an error. The waits are bounded by
            # MODELPROXY_RETRY_MAX_WAIT in all, and every line says how long.
            if resp.status in RETRY_STATUS and tries < cfg.retries:
                pause = retry_after(resp)
                if pause is None:
                    pause = cfg.retry_backoff * 2 ** tries
                if waited + pause <= cfg.retry_max_wait:
                    conn.close()
                    tries, waited = tries + 1, waited + pause
                    log(f"{method} {rest}{who} -> upstream {resp.status}; waiting {pause:.1f}s before asking again "
                        f"({tries} of {cfg.retries}, {waited:.1f}s of {cfg.retry_max_wait:.0f}s)")
                    cfg.sleep(pause)
                    continue
                log(f"{method} {rest}{who} -> upstream {resp.status}; not waiting {pause:.1f}s more "
                    f"(over the {cfg.retry_max_wait:.0f}s bound)")
            return conn, resp, (f" after {tries} retries ({waited:.1f}s waiting)" if tries else ""), sock, deadline

    def send_head(self, a, length=None):
        """The status line and headers of attempt `a` (`length` when the whole
        body is known). Only once per request: after a keep-alive sent the head
        of an earlier attempt, later attempts go out under it."""
        with self.wlock:
            if self.out_mode is not None:
                return
            cfg, resp = self.cfg, a.resp
            self.send_response_only(a.status, resp.reason)
            for k, v in resp.getheaders():
                lk = k.lower()
                if lk not in DROP_RESPONSE and (not cfg.gateway or lk in GATEWAY_RESPONSE_HEADERS):
                    self.send_header(k, v)
            if cfg.gateway and a.checked:
                self.send_header(CHECKED_HEADER, "checked")
            upstream_length = resp.getheader("Content-Length")
            if a.status in (204, 304) or a.status < 200:
                mode = "none"
            elif length is not None:
                self.send_header("Content-Length", str(length))
                mode = "length"
            elif upstream_length is not None and not (a.is_sse and (a.injected or a.checked)):
                self.send_header("Content-Length", upstream_length)
                mode = "length"
            elif self.request_version == "HTTP/1.0":
                self.send_header("Connection", "close")
                self.close_connection = True
                mode = "close"
            else:
                self.send_header("Transfer-Encoding", "chunked")
                mode = "chunked"
            self.end_headers()
            self.out_mode = mode

    def write_body(self, data):
        with self.wlock:
            if self.out_mode == "none" or not data:
                return
            if self.out_mode == "chunked":
                data = b"%x\r\n%s\r\n" % (len(data), data)
            self.wfile.write(data)  # unbuffered: each piece leaves as it arrives
            if self.keepalive is not None:
                self.keepalive.wrote()

    def end_body(self):
        with self.wlock:
            if self.out_mode == "chunked":
                self.wfile.write(b"0\r\n\r\n")

    def read_upstream(self, a):
        """The next piece of attempt `a`'s answer (b"" at its end), waiting at
        most until its first-byte deadline (before the first data) or
        MODELPROXY_STALL_TIMEOUT (after it); raises UpstreamSilent then."""
        cfg = self.cfg
        if a.first_data is None and a.deadline is not None:
            limit = a.deadline - time.monotonic()
            if limit <= 0:
                raise UpstreamSilent("first byte")
        else:
            limit = cfg.stall_timeout or cfg.upstream_timeout
        try:
            if a.sock is not None:
                a.sock.settimeout(max(0.001, limit))
            data = a.resp.read1(65536)
        except TimeoutError:
            raise UpstreamSilent("first byte" if a.first_data is None and a.deadline is not None else "stall") from None
        if data and a.first_data is None:
            if a.is_sse:
                a.pre = (a.pre + data)[-4096:]
                if b"\ndata:" in b"\n" + a.pre:
                    a.first_data = time.monotonic()
            else:
                a.first_data = time.monotonic()
        return data

    def relay(self, a, hold):
        """Reads attempt `a`'s answer. Without `hold` it goes to the client as
        it arrives (the head with the first piece, so an attempt that never
        starts can still be given up). With `hold` it is kept back (a.held)
        until it is known to be a tool call (then to its end) or plain text
        longer than the hold limit (then it is released and streams on);
        a.sent says whether any of it went out."""
        if not hold:
            self.stop_keepalive()   # a comment must never land inside a passing event
            if a.status in (204, 304) or a.status < 200:
                self.send_head(a)
                a.sent = True
                a.conn.close()
                return
        try:
            while True:
                if a.sent and self.out_mode == "none":
                    break
                data = self.read_upstream(a)
                if not data:
                    break
                if a.scan is not None:
                    data = a.scan.feed(data)
                if a.kept is not None and len(a.kept) <= KEEP_JSON_BYTES:
                    a.kept += data
                if not data:
                    continue
                if not hold:
                    if not a.sent:
                        self.send_head(a)
                        a.sent = True
                    self.write_body(data)
                    continue
                if a.sent:
                    self.write_body(data)
                    continue
                a.held += data
                if len(a.held) > MAX_HOLD_BYTES or (
                        a.is_sse and not a.check.tool_seen and a.check.text > self.cfg.hold_chars):
                    self.stop_keepalive()
                    self.send_head(a)          # plain text (or too big to hold): it streams on from here
                    a.sent = a.streamed_early = True
                    self.write_body(bytes(a.held))
                    a.held = bytearray()
            if a.scan is not None:
                tail = a.scan.finish()
                if a.sent:
                    self.write_body(tail)
                else:
                    a.held += tail
            if not hold and not a.sent:        # an empty answer
                self.send_head(a)
                a.sent = True
            if a.sent:
                self.end_body()
        except UpstreamSilent as e:
            if str(e) == "first byte":
                a.first_byte_timeout = True
                a.note = " (no first byte in time)"
            else:
                a.stalled = a.interrupted = True
                a.note = f" stalled (no byte for {self.cfg.stall_timeout:g}s)"
        except (OSError, http.client.HTTPException) as e:
            a.interrupted = True
            a.note = f" interrupted ({type(e).__name__})"
        finally:
            a.conn.close()
        if a.checked and not a.first_byte_timeout:
            if not a.is_sse and a.kept is not None and len(a.kept) <= KEEP_JSON_BYTES:
                try:
                    a.check.message(json.loads(bytes(a.kept)))
                except ValueError:
                    pass
            # An interruption after bytes went out may be the client leaving: not judged.
            a.problem = None if (a.sent and a.interrupted) else a.check.problem(a.is_sse, a.interrupted)

    def stop_keepalive(self):
        if self.keepalive is not None:
            self.keepalive.stop()

    def release(self, a):
        """Sends an answer that was held back in full."""
        self.stop_keepalive()
        try:
            self.send_head(a, length=len(a.held))
            self.write_body(bytes(a.held))
            self.end_body()
        except OSError as e:
            a.interrupted = True
            a.note = f" interrupted ({type(e).__name__})"
        a.sent, a.held = True, bytearray()


class KeepAlive:
    """While a streamed answer is held back for the tool-call check, the client
    hears an SSE comment (`: keepalive`) every `every` seconds without other
    output, so its idle watchdog (OpenClaw: about 120 s for a cloud provider)
    does not end the request. The first comment sends the head of the attempt
    under watch (a 2xx stream); the answer that is finally passed on, this one
    or a later one, goes out under that head. A comment is not part of any
    answer, so asking again after one is still safe."""

    def __init__(self, handler, attempt, every):
        self.h, self.a, self.every = handler, attempt, every
        self.last = time.monotonic()
        self.done = threading.Event()
        self.sent = 0
        threading.Thread(target=self.run, daemon=True).start()

    def wrote(self):
        self.last = time.monotonic()

    def run(self):
        while not self.done.wait(min(self.every, 1.0)):
            if time.monotonic() - self.last < self.every:
                continue
            with self.h.wlock:
                if self.done.is_set():
                    return
                try:
                    self.h.send_head(self.a)
                    if self.h.out_mode in ("chunked", "close"):
                        self.h.write_body(b": keepalive\n\n")
                        self.sent += 1
                except OSError:
                    return

    def stop(self):
        with self.h.wlock:
            self.done.set()


class UpstreamSilent(Exception):
    """The upstream sent nothing for too long: "first byte" or "stall"."""


TIMED_OUT = object()   # open_upstream: no answer head before the first-byte deadline


class Attempt:
    """One upstream answer on its way to the client."""

    def __init__(self, conn, resp, retried, injected, metered, checked, sock=None, deadline=None):
        self.conn, self.resp, self.retried, self.injected = conn, resp, retried, injected
        self.metered, self.checked = metered, checked
        self.sock, self.deadline = sock, deadline
        self.status = resp.status
        self.is_sse = "text/event-stream" in (resp.getheader("Content-Type") or "").lower()
        self.check = ToolCallCheck() if checked else None
        observer = self.check.payload if (checked and self.is_sse) else None
        self.scan = SSEScanner(strip=injected, observer=observer) if (self.is_sse and metered) else None
        self.kept = bytearray() if (metered and not self.is_sse) else None
        self.held = bytearray()
        self.pre = b""
        self.first_data = None
        self.sent = self.streamed_early = self.interrupted = False
        self.first_byte_timeout = self.stalled = False
        self.problem, self.note = None, ""

    def usage(self, body):
        """((prompt, completion, cached), estimated) of this answer."""
        usage = None
        if self.scan is not None:
            usage = self.scan.usage
        elif self.kept is not None and len(self.kept) <= KEEP_JSON_BYTES:
            try:
                usage = usage_tokens(json.loads(bytes(self.kept)))
            except ValueError:
                usage = None
        if usage is not None:
            return usage, False
        # The upstream sent no usage: a rough count, so the cap still bites.
        return (len(body or b"") // 4, self.scan.chunks if self.scan is not None else len(self.kept or b"") // 4, 0), True


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
        if path not in ("/admin/usage", "/admin/tenants", "/admin/requests"):
            return self.fail(404, "unknown admin endpoint")
        if not self.allowed():
            return None
        if path == "/admin/tenants":
            return self.send_json(200, self.gw.export())
        if path == "/admin/requests":
            since, limit, tenant = query_params(query)
            return self.send_json(200, {"requests": self.gw.requests.read(since, limit, tenant)})
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
