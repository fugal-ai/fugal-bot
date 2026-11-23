#!/usr/bin/env bash
# Chain test, Docker only, scripted model, no paid call and no real key:
#
#   agent (OpenClaw) -> the bot's model proxy (room mode) -> a model proxy in
#   gateway mode -> the scripted model
#
# This is how a hosted bot reaches its model: the room holds only a per-bot
# token (fbk_<bot_id>_<43 chars>); the gateway holds the upstream key, checks
# the token against the tenant table, and enforces the model list, the bot's
# daily allowance and the cap across all bots. The test drives real agent turns
# through the password proxy and changes the tenant table while it runs.
#
# Usage: scripts/gateway-chain-test.sh [--keep]
# Optional environment: CHAIN_PROJECT, CHAIN_PORT, and the image names /
# FRONT_NET / TUNNEL_NET that compose.yaml reads.
set -uo pipefail
cd "$(dirname "$0")/.."

PROJECT=${CHAIN_PROJECT:-fugal-bot-chain}
export PORT=${CHAIN_PORT:-18082} BIND_ADDRESS=127.0.0.1 PUBLIC_HOSTNAME=
[ -f .env ] || { echo "no .env (cp .env.example .env)"; exit 2; }
PW=$(grep -E '^ACCESS_PASSWORD=' .env | head -1 | cut -d= -f2-)
[ -n "$PW" ] || { echo "ACCESS_PASSWORD empty in .env"; exit 2; }
DC=(docker compose -p "$PROJECT" --env-file .env -f compose.yaml -f scripts/smoke.compose.yaml -f scripts/gateway-test.compose.yaml)
BASE="http://127.0.0.1:${PORT}"
JAR=$(mktemp); trap 'rm -f "$JAR"' EXIT
KEEP=0; [ "${1:-}" = --keep ] && KEEP=1

# Stand-ins made for this run and worth nothing: the upstream key (only the
# gateway gets it) and the bot's gateway token (only the bot's model proxy).
rand() { head -c "$1" /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "$2"; }
BOT_ID="b$(rand 64 10 | tr 'A-Z0189' 'a-z2345')"
export SMOKE_CANARY_KEY="sk-chain-$(rand 48 32)"
export GATEWAY_TEST_TOKEN="fbk_${BOT_ID}_$(rand 96 43)"
TOKEN_SHA=$(printf %s "$GATEWAY_TEST_TOKEN" | sha256sum | cut -d' ' -f1)
export MODEL_NAME=tools MODEL_SUPPORTS_TOOLS=true MODEL_CONTEXT_WINDOW=131072 MODEL_MAX_TOKENS=4096 MODEL_REASONING=false
export MODEL_BASE_URL=unused-the-overlay-sets-it MODEL_API_KEY=unused-the-overlay-sets-it
export BUDGET_TOKENS_PER_DAY= BUDGET_TOKENS_TOTAL= BUDGET_PRICES= BUDGET_USD_PER_DAY= BUDGET_USD_TOTAL= BUDGET_REQUESTS_PER_MINUTE=
export MODELPROXY_EXHAUSTED_REPLY=

PASS=0; FAIL=0
ok()   { echo "PASS  $*"; PASS=$((PASS + 1)); }
bad()  { echo "FAIL  $*"; FAIL=$((FAIL + 1)); }
step() { echo; echo "== $*"; }
check(){ local name=$1; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }
has()  { [[ "$1" == *"$2"* ]]; }

wait_healthy() {
  local t0=$SECONDS b p
  while [ $((SECONDS - t0)) -lt 300 ]; do
    b=$(docker inspect -f '{{.State.Health.Status}}' "$("${DC[@]}" ps -q bot)" 2>/dev/null)
    p=$(docker inspect -f '{{.State.Health.Status}}' "$("${DC[@]}" ps -q proxy)" 2>/dev/null)
    [ "$b" = healthy ] && [ "$p" = healthy ] && { echo "healthy after $((SECONDS - t0))s"; return 0; }
    sleep 3
  done
  echo "bot=$b proxy=$p"; "${DC[@]}" logs --tail 40 bot; return 1
}
chat() {   # chat <session-user> <message> -> the assistant text, or the error text
  local out i
  for i in $(seq 1 12); do
    out=$(curl -s -b "$JAR" --max-time 600 "$BASE/v1/chat/completions" -H 'Content-Type: application/json' \
      -d "$(python3 -I -c 'import json,sys;print(json.dumps({"model":"openclaw/default","user":sys.argv[1],"messages":[{"role":"user","content":sys.argv[2]}]}))' "$1" "$2")" |
      python3 -I -c 'import json,sys
r=sys.stdin.read()
try:
  d=json.loads(r); print(d["choices"][0]["message"]["content"] if "choices" in d else "ERROR: " + d["error"]["message"])
except Exception: print("RAW:", r[:600])')
    case "$out" in *"Please retry"*|*"is starting"*) echo "(gateway still starting, retrying)" >&2; sleep 5 ;; *) break ;; esac
  done
  printf '%s\n' "$out"
}
# The gateway's admin listener has no published port: it is called from inside
# the gateway's own container. admin GET <path> | admin PUT <path> <json>
admin() {
  printf '%s' "${3:-}" | "${DC[@]}" exec -T llmgateway python3 -I -c '
import sys, urllib.request, urllib.error
body = sys.stdin.buffer.read() or None
req = urllib.request.Request("http://127.0.0.1:9002" + sys.argv[2], data=body, method=sys.argv[1])
try:
    print(urllib.request.urlopen(req, timeout=10).read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())' "$1" "$2"
}
# table <usd a day for the bot> <model ids as a JSON list> <usd a day for all bots, or null>
table() {
  admin PUT /admin/tenants "{\"tenants\":[{\"id\":\"$BOT_ID\",\"token_sha256\":\"$TOKEN_SHA\",\"usd_per_day\":$1,\"models\":$2,\"enabled\":true}],\"prices\":{\"tools\":[1,3],\"other\":[1,3]},\"platform_usd_per_day\":$3}"
}
num() { python3 -I -c 'import json,sys
d=json.loads(sys.argv[1] or "{}")
for k in sys.argv[2].split("."): d=d.get(k, 0) if isinstance(d, dict) else 0
print(d if d is not None else "")' "$1" "$2"; }
room_usage() { "${DC[@]}" exec -T modelproxy python3 -I -c 'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:9001/usage",timeout=5).read().decode())'; }

step "build (Docker only) + start: agent -> model proxy -> gateway -> scripted model"
avail=$(free -m | awk '/^Mem:/ {print $7}')
[ "$avail" -ge 3000 ] || { echo "only ${avail} MB of memory available; not starting"; exit 3; }
"${DC[@]}" down -v --remove-orphans >/dev/null 2>&1
"${DC[@]}" build -q || { echo build failed; exit 1; }
"${DC[@]}" up -d || exit 1
check "bot and proxy healthy" wait_healthy
curl -s -o /dev/null -c "$JAR" --data-urlencode "password=$PW" --data-urlencode "next=/" "$BASE/_gate/login"

step "before the gateway knows the bot: refused"
direct=$("${DC[@]}" exec -T modelproxy python3 -I -c '
import os, urllib.request, urllib.error
req = urllib.request.Request(os.environ["MODEL_BASE_URL"] + "/models", headers={"Authorization": "Bearer " + os.environ["MODEL_API_KEY"]})
try: print(urllib.request.urlopen(req, timeout=10).status)
except urllib.error.HTTPError as e: print(e.code)')
check "the bot's token is not accepted yet (HTTP $direct)" test "$direct" = 401

step "tenant table put through the admin listener (no restart)"
out=$(table 5 '["tools"]' 100); echo "$out"
check "table accepted" has "$out" '"ok": true'
stored=$("${DC[@]}" exec -T llmgateway cat /data/tenants.json)
check "the gateway stores the token's SHA-256" has "$stored" "$TOKEN_SHA"
check "and not the token" bash -c '[[ "$0" != *"$1"* ]]' "$stored" "$GATEWAY_TEST_TOKEN"

step "an agent turn through the whole chain"
reply=$(chat chain-memory "Please remember this code word for later: MAGPIE-17. Reply with one short friendly sentence.")
echo "reply: ${reply:0:300}"
check "the scripted model answered (it accepts only the gateway's upstream key)" has "$reply" "code word MAGPIE-17"
gu=$(admin GET /admin/usage); ru=$(room_usage)
echo "gateway: bot $BOT_ID requests $(num "$gu" "tenants.$BOT_ID.day.requests"), tokens $(num "$gu" "tenants.$BOT_ID.day.total_tokens")," \
  "usd $(num "$gu" "tenants.$BOT_ID.day.usd"); all bots usd $(num "$gu" platform.day.usd); room proxy tokens $(num "$ru" total.total_tokens)"
check "the gateway counted the call for this bot" test "$(num "$gu" "tenants.$BOT_ID.day.requests")" -ge 1
check "the gateway and the bot's own model proxy counted the same tokens" \
  test "$(num "$gu" "tenants.$BOT_ID.day.total_tokens")" = "$(num "$ru" total.total_tokens)" -a "$(num "$ru" total.total_tokens)" -gt 0
check "tokens came from the upstream usage field at both hops" \
  test "$(num "$gu" "tenants.$BOT_ID.day.estimated_requests")" = 0 -a "$(num "$ru" total.estimated_requests)" = 0
check "priced in USD from the table" python3 -I -c 'import sys; sys.exit(0 if float(sys.argv[1]) > 0 else 1)' "$(num "$gu" "tenants.$BOT_ID.day.usd")"

step "who holds what"
check "the bot's model proxy holds the bot's token, not the upstream key" \
  test "$("${DC[@]}" exec -T modelproxy sh -c 'tr "\0" "\n" < /proc/1/environ' | grep -c -F "$SMOKE_CANARY_KEY")" = 0
check "the agent container holds neither" \
  test "$("${DC[@]}" exec -T bot sh -c 'cat /proc/[0-9]*/environ 2>/dev/null; env' | grep -a -c -F -e "$SMOKE_CANARY_KEY" -e "$GATEWAY_TEST_TOKEN")" = 0
check "the agent container cannot reach the gateway at all" \
  bash -c '! "$@" >/dev/null 2>&1' _ "${DC[@]}" exec -T bot node -e 'fetch("http://llmgateway:9001/healthz", { signal: AbortSignal.timeout(4000) }).then(() => process.exit(0), () => process.exit(1))'
pub=$("${DC[@]}" exec -T modelproxy python3 -I -c '
import urllib.request, urllib.error
for path in ("/admin/usage", "/admin/tenants", "/usage"):
    try: print(urllib.request.urlopen("http://llmgateway:9001" + path, timeout=5).status, end=" ")
    except urllib.error.HTTPError as e: print(e.code, end=" ")')
check "the admin API is not served on the gateway's main listener ($pub)" test "$pub" = "404 404 404 "

step "model list: the bot's model is taken off its list"
table 5 '["other"]' 100 >/dev/null
reply=$(chat chain-memory "Hello, are you there?")
echo "what the user is told: ${reply:0:300}"
check "refused, and the reply names the model and the list" has "$reply" "is not available to this bot"

step "allowance: the bot's daily allowance is used up"
table 0.000001 '["tools"]' 100 >/dev/null
reply=$(chat chain-memory "Hello, are you there?")
echo "what the user is told: ${reply:0:300}"
check "the bot says so, in the gateway's words" has "$reply" "free allowance for today is used up and it comes back at midnight UTC"
check "and says what to do now" has "$reply" "add your own model key in Settings on the Fugal Bot page"

step "cap across all bots"
table 5 '["tools"]' 0.000001 >/dev/null
reply=$(chat chain-memory "Hello, are you there?")
echo "what the user is told: ${reply:0:300}"
check "the bot says so, in the gateway's words" has "$reply" "free allowance shared by all bots is used up for today"
gu2=$(admin GET /admin/usage)
echo "gateway refused since start: $(num "$gu2" rejected_since_start)"
check "the three refusals cost three requests and no tokens" \
  test "$(num "$gu2" rejected_since_start.model)$(num "$gu2" rejected_since_start.tenant_allowance)$(num "$gu2" rejected_since_start.platform_allowance)" = 111 \
  -a "$(num "$gu2" "tenants.$BOT_ID.day.total_tokens")" = "$(num "$gu" "tenants.$BOT_ID.day.total_tokens")"

step "allowances raised again: the very next message"
table 5 '["tools"]' 100 >/dev/null
reply=$(chat chain-memory "What was the code word I asked you to remember? Answer with just the code word.")
echo "reply: ${reply:0:300}"
check "answered at once, conversation intact (no lockout in the agent)" has "$reply" "MAGPIE-17"

echo; echo "== result: $PASS passed, $FAIL failed"
if [ "$KEEP" = 0 ]; then "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1; echo "(stopped; containers and the test volume removed)"; fi
[ "$FAIL" = 0 ]
