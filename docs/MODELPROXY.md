# The model proxy

`modelproxy/modelproxy.py` is one stdlib-Python program in one image
(`fugal-bot-modelproxy`) with two roles, chosen by `MODELPROXY_MODE`:

- **`room`** (default): runs next to one bot and keeps the model API key away
  from the agent. This is the `modelproxy` service in `compose.yaml`; see the
  README and `SECURITY-NOTES.md`.
- **`gateway`**: runs once, centrally, in front of one upstream account that
  many bots share. Each bot has its own token, a daily allowance in US dollars
  and a list of models; there is one daily cap across all bots.

A room-mode proxy can point at a gateway like at any other endpoint
(`MODEL_BASE_URL` = the gateway, `MODEL_API_KEY` = the bot's token), so a
hosted bot's room holds a per-bot token and never the upstream key.

Unit tests (stdlib, loopback only): `python3 -I modelproxy/test_modelproxy.py`.

## What the bot says when an allowance is used up

A request that arrives after a cap is reached is not sent upstream. How it is
answered matters more than it looks, because the agent framework reacts to
the form of the reply. `MODELPROXY_EXHAUSTED_REPLY` chooses the form; it
applies to both roles.

Measured on 2026-11-23 against OpenClaw v2026.11.6 (the pinned release) with
the scripted test model, one fresh data volume per form, a cap of one token:

| Reply | What the user sees on the first refused message | Requests per refused message | After the cap is lifted |
|---|---|---|---|
| `429`, `insufficient_quota` | Web chat: "Your request couldn't be completed: The AI service reported a billing problem. Check your account's credit balance and usage limits before trying again." The proxy's text is not shown. Classed as a billing failure. | 1 | **Locked out.** OpenClaw disables the provider key for 10 minutes. Every message in that time fails inside OpenClaw without a request ("Inline API key for provider "bot" is temporarily disabled after a provider auth/billing failure. Retry after about 10 minutes ..."), also after the cap is lifted. |
| `402`, `insufficient_quota` | Classed as a billing failure, like 429. | 1 | **Locked out**, the same 10 minutes. |
| `400`, `invalid_request_error` (default) | Web chat: "Unable to finish this reply. Your request couldn't be completed: LLM request rejected: " followed by the proxy's text. Classed as a format error: shown, not retried. | 1 | **The very next message is answered.** No cooldown state is written. |
| `200`, a normal assistant message | The proxy's text as the bot's answer. | 1 | The very next message is answered. But see below. |

(Through OpenClaw's own OpenAI-compatible API, which the smoke test uses, the
first refused message carries the proxy's text in all four forms; the web
chat differs as shown.)

More on each:

- **The lockout (429 and 402)** is stored in the bot's data
  (`"disabledUntil"` and `"disabledReason":"billing"` in OpenClaw's auth
  state in `~/.openclaw`), so it **survives a restart** of the bot: after a
  restart inside the window the next message was still refused without a
  request. It **does not grow**: the first, second and third billing failure
  in a row (each sent just after the previous window had ended) each started
  a window of exactly 600 seconds, and the stored `errorCount` stayed 1.
  Lifting the cap does not clear it. Once a window has ended the next call
  does go out again.
- **The 200 form is unsafe for conversation history.** OpenClaw cannot tell
  the notice from a real answer. With the allowance used up, a context
  compaction (`/compact`, or the automatic one) made four summary requests,
  got the notice four times, and stored a summary whose "Decisions" section
  was the notice text. The older turns that summary replaced are then gone
  from the model's context. With the 400 form the same compaction failed
  cleanly ("cancelling compaction to preserve history") and nothing was
  stored. The notice would also be stored as an assistant turn in every
  transcript and be sent on by channels and automations as if the bot had
  said it.
- **The 400 form** has neither problem, which is why it is the default.
  OpenClaw's web chat shows an error text with a backslash before every
  punctuation mark, so the proxy's texts are written without punctuation:
  "This bot has used up its inference allowance for today and it renews at
  midnight UTC" (a daily cap), "This bot has used up its whole inference
  allowance and the owner of the bot can raise it" (a total cap), "The
  inference allowance shared by all bots is used up for today and it renews
  at midnight UTC" (the gateway's cap across all bots).

Not measured: what Telegram shows (no token was used), and other agent
frameworks. If OpenClaw is upgraded, run `scripts/smoke.sh`: its last step
checks that a refused message is told to the user in the proxy's words and
that the next message is answered once the cap is lifted.

## Upstream 429 and 503

A busy upstream is asked again by the proxy itself, as long as nothing has
been sent to the client: it waits as the upstream says (`retry-after-ms`,
or `Retry-After` in seconds or as a date), else 1, 2, 4 seconds, and then
repeats the call. `MODELPROXY_UPSTREAM_RETRIES` is the number of repeats
(default 3 in gateway mode, 0 in room mode, where the bot's own key is in
use and the answer is passed through as it is). `MODELPROXY_RETRY_MAX_WAIT`
(default 30 seconds) bounds the waiting in total: a wait that would pass it
is not started. A reply that has begun is never repeated.

## Gateway mode

```
MODELPROXY_MODE=gateway
MODEL_BASE_URL=https://provider.example/v1    # the one upstream
MODEL_API_KEY=...                             # its key; held only here
MODELPROXY_LISTEN_HOST=0.0.0.0                # main listener, default port 9001
MODELPROXY_LISTEN_PORT=9001
MODELPROXY_ADMIN_LISTEN_HOST=127.0.0.1        # admin listener, default 127.0.0.1:9002
MODELPROXY_ADMIN_LISTEN_PORT=9002
MODELPROXY_ADMIN_TOKEN=                       # optional: then admin calls need "Authorization: Bearer <it>"
MODELPROXY_TENANT_CONCURRENCY=4               # calls in progress per bot; 0 = no limit
MODELPROXY_DATA_DIR=/data                     # tenants.json, gateway-usage-<day>.json
```

`BUDGET_*` settings are refused in this mode: allowances and prices come
from the tenant table.

**The two listeners.** The main listener serves `/v1/*` and `/healthz` and
nothing else; put only this one behind the public reverse proxy. The admin
listener must never be published: bind it to an address that only the
controller can reach (in a container, `0.0.0.0` with no published port, on
a network shared with the controller only). `/admin/*` on the main listener
is a 404.

**Tokens.** A bot's token is `fbk_<bot_id>_<43 characters>`: the bot id (1 to
40 letters and digits), then 43 characters from `A-Z a-z 0-9 - _` (32 random
bytes in URL-safe base64, no padding). The gateway is given and stores only
`token_sha256`: the SHA-256 of the whole token text, in hex. A request
carries the token as `Authorization: Bearer fbk_...`.

**Admin API** (JSON):

- `PUT /admin/tenants` replaces the whole state. All or nothing: a faulty
  body is answered with 400 and a reason, and the table in force stays. The
  new table is written to disk first and used from the next request on; no
  restart.

  ```json
  {
    "tenants": [
      {"id": "bq4x7k2m9pa", "token_sha256": "<64 hex digits>", "usd_per_day": 0.35,
       "models": ["model-a", "model-b"], "enabled": true}
    ],
    "prices": {"model-a": [0.6, 2.5, 0.06], "model-b": [1, 3]},
    "platform_usd_per_day": 9
  }
  ```

  `prices` is USD per million tokens, a row being `[input, output]` or
  `[input, output, cached_input]` (see "Cached prompt tokens" below); every
  model on a tenant's list needs a row (or a `"*"` row for all others). `usd_per_day` 0
  means nothing, not "no limit". `platform_usd_per_day` is required: a
  number (0 stops all inference) or `null` for no cap across bots.
  `enabled` defaults to true.
- `GET /admin/tenants` returns the table as stored.
- `GET /admin/usage` (optional `?days=N`, default 7, at most 35) returns
  `today`, `platform` (`usd_per_day`, `day`, `remaining_usd`, `exceeded`,
  `days`), `tenants` by id (`in_table`, `enabled`, `usd_per_day`, `models`,
  `day`, `remaining_usd`, `exceeded`, `in_flight`, `days`) and
  `rejected_since_start`. A counter row is `requests`, `prompt_tokens`,
  `cached_prompt_tokens`, `completion_tokens`, `total_tokens`, `usd`,
  `estimated_requests`.

**What a tenant gets:**

| Case | Answer |
|---|---|
| No token, a malformed or unknown one | 401 `invalid_api_key` |
| `enabled: false` | 403 `tenant_disabled` |
| A model not on its list (or no `model` in the body) | 400 `model_not_allowed` naming the model and the list (`model_required`); not sent upstream |
| Its daily allowance, or the cap across all bots, is used up | the `MODELPROXY_EXHAUSTED_REPLY` form (default 400 `allowance_exhausted`) |
| More than `MODELPROXY_TENANT_CONCURRENCY` calls in progress | 429 `rate_limit_exceeded` with `Retry-After: 2` |
| Upstream 429 or 503 that outlasts the retries; upstream 401, 402 or 403 | 503 `upstream_unavailable`. These are about the shared account (its rate limit, key or balance), so they never reach a bot as a quota or key error, which its agent would act on (see the lockout above) |
| `GET /v1/models` | its own list, answered by the gateway |

Days are UTC days. An allowance is checked before a call and the cost is
known after it, so the last calls can overshoot: by at most the cost of
`MODELPROXY_TENANT_CONCURRENCY` calls per bot. Cost is taken from the
upstream's `usage` field and the price table; if the upstream sends no
usage, a rough estimate is counted and marked.

**Cached prompt tokens.** Providers that cache the repeated start of a prompt
bill those tokens at a fraction of the input price (about a tenth), and report
how many were served from the cache. The proxy reads that count from the
upstream's `usage`, in the first of these places it finds a non-negative whole
number: `prompt_tokens_details.cached_tokens` (OpenAI style; Chutes, vLLM),
`input_tokens_details.cached_tokens` (Responses API), `prompt_cache_hit_tokens`
(DeepSeek), `cache_read_input_tokens` (Anthropic style), `cached_tokens`.
Cached tokens count as part of `prompt_tokens`. They are kept as their own
counter, `cached_prompt_tokens`, in every counter row (per tenant, platform,
day and total in the gateway; per day and total in a room's `GET /usage`), for
JSON and streamed replies alike. The cost of a call is

    (prompt - cached) * input + cached * cached_input + completion * output

per million tokens. A price row with two values bills cached tokens at the
full input price, which is what happened before the third value existed and
the safe default when a provider's cache price is not known. A count above the
prompt is treated as the whole prompt; the cost is never negative; a reply
that reports no cache information counts zero cached tokens. A room proxy
takes the same rows in `BUDGET_PRICES`, for example
`{"model-a": [0.6, 2.5, 0.06], "*": [1, 3]}`. An older gateway (before this
change) refuses three-value rows, so send two-value rows to it.

Stored files stay compatible in both directions as far as rows go: a counter
file written before the counter existed loads with zero cached tokens, and a
stored table keeps each row with the number of values it was given.

**Kept apart.** Of a tenant's request headers only `Content-Type`, `Accept`
and `User-Agent` go upstream (not its address as added by a reverse proxy,
no cookies); of the upstream's response headers only `Content-Type`,
`Cache-Control`, `Date` and `X-Request-Id` go back. Logged per request: the
bot id, the path, the status, token counts and the duration. Never logged
or stored: bodies, headers, the upstream key, tokens.

**Files.** `tenants.json` and one `gateway-usage-<day>.json` per UTC day (35
days kept), each replaced atomically (written beside, synced, renamed), mode
600. An unreadable file is kept aside (`*.unreadable-<time>`) and the
gateway starts without it: with no table every request is a 401 until the
next `PUT /admin/tenants`.

## Chain test

`scripts/gateway-chain-test.sh` runs the whole path in Docker with the
scripted model: agent, the bot's model proxy in room mode, a second model
proxy in gateway mode (`scripts/gateway-test.compose.yaml`), the scripted
model. It checks an agent turn end to end, the counters at both hops, who
holds which secret, the model list, the bot's allowance, the cap across all
bots, and that the next message is answered once the table is put back.
