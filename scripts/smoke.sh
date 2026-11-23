#!/usr/bin/env bash
# Stage-0 smoke test. Builds and runs the bot in Docker only (separate compose
# project + volume from your real bot), then checks, through the password proxy:
#   login gate, web chat UI, a model reply (which travels bot -> model proxy ->
#   model), the model proxy's token counters, one shell command + one file
#   created by the agent, one page loaded in the bot's visible browser, the
#   desktop (noVNC) endpoint, that the model API key cannot be read from the
#   agent container, then stop + start and that the file, the conversation and
#   the counters survived, and last that a very low budget makes the model
#   proxy refuse and that the next message is answered once it is raised.
#
# Brain (SMOKE_BRAIN):
#   scripted (default)  the test-only scripted model (scripts/scripted-brain/)
#                       is the model proxy's upstream: no paid call, no real
#                       key, no local model. A random stand-in key is used, and
#                       the scripted model refuses requests without it.
#   configured          the model from .env. A hosted model costs money per
#                       request: one run makes roughly 5-10 requests. The
#                       computer-use checks are skipped if that model cannot
#                       call tools (MODEL_SUPPORTS_TOOLS=false).
#
# Optional environment:
#   SMOKE_SECRETS_FILE=path   extra env file with secrets (MODEL_API_KEY,
#                             TELEGRAM_BOT_TOKEN); read by compose, never printed
#   MODEL_BASE_URL / MODEL_NAME / MODEL_SUPPORTS_TOOLS   override .env (configured brain)
# Usage: scripts/smoke.sh [--keep] [--fresh]
set -uo pipefail
cd "$(dirname "$0")/.."

PROJECT=${SMOKE_PROJECT:-fugal-bot-smoke}
export PORT=${SMOKE_PORT:-18080} BIND_ADDRESS=127.0.0.1 PUBLIC_HOSTNAME=
ENVF=(--env-file .env)
[ -n "${SMOKE_SECRETS_FILE:-}" ] && ENVF+=(--env-file "$SMOKE_SECRETS_FILE")
FILES=()
IFS=: read -r -a CF <<< "${SMOKE_COMPOSE_FILES:-compose.yaml:scripts/smoke.compose.yaml}"
for f in "${CF[@]}"; do FILES+=(-f "$f"); done
DC=(docker compose -p "$PROJECT" "${ENVF[@]}" "${FILES[@]}")
envval() { local v; v=$(printenv "$1"); [ -n "$v" ] && { echo "$v"; return; }; grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }
BASE="http://127.0.0.1:${PORT}"
JAR=$(mktemp); KEYFILE=$(mktemp); chmod 600 "$KEYFILE"; trap 'rm -f "$JAR" "$KEYFILE"' EXIT
KEEP=0; FRESH=0
for a in "$@"; do case "$a" in --keep) KEEP=1 ;; --fresh) FRESH=1 ;; esac; done

[ -f .env ] || { echo "no .env (cp .env.example .env)"; exit 2; }
PW=$(grep -E '^ACCESS_PASSWORD=' .env | head -1 | cut -d= -f2-)
[ -n "$PW" ] || { echo "ACCESS_PASSWORD empty in .env"; exit 2; }

BRAIN=${SMOKE_BRAIN:-scripted}
case "$BRAIN" in
  scripted)
    # The scripted model is the model proxy's upstream; shell variables win
    # over .env in compose. The stand-in key is random and worth nothing.
    SMOKE_CANARY_KEY="sk-smoke-$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
    export SMOKE_CANARY_KEY MODEL_API_KEY="$SMOKE_CANARY_KEY"
    export MODEL_BASE_URL=${SMOKE_BRAIN_URL:-http://brain:11500/v1} MODEL_NAME=tools MODEL_SUPPORTS_TOOLS=true
    export MODEL_CONTEXT_WINDOW=131072 MODEL_MAX_TOKENS=4096 MODEL_REASONING=false
    export BUDGET_TOKENS_PER_DAY= BUDGET_TOKENS_TOTAL= BUDGET_PRICES= BUDGET_USD_PER_DAY= BUDGET_USD_TOTAL= BUDGET_REQUESTS_PER_MINUTE=
    export MODELPROXY_EXHAUSTED_REPLY=
    ;;
  configured) ;;
  *) echo "SMOKE_BRAIN must be 'scripted' or 'configured'"; exit 2 ;;
esac
TOOLS=$(envval MODEL_SUPPORTS_TOOLS); TOOLS=${TOOLS:-true}
# All services in one network namespace (compose.cathedral.yaml)?
case "${SMOKE_COMPOSE_FILES:-}" in *cathedral*) ONE_NETNS=1 ;; *) ONE_NETNS=0 ;; esac

PASS=0; FAIL=0
ok()   { echo "PASS  $*"; PASS=$((PASS + 1)); }
bad()  { echo "FAIL  $*"; FAIL=$((FAIL + 1)); }
step() { echo; echo "== $*"; }
check(){ local name=$1; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }

wait_mem() {
  for _ in $(seq 1 60); do
    avail=$(free -m | awk '/^Mem:/ {print $7}')
    [ "$avail" -ge 3000 ] && { echo "available memory: ${avail} MB"; return 0; }
    echo "available memory ${avail} MB < 3000 MB, waiting..."; sleep 20
  done
  echo "not enough memory"; exit 3
}

wait_healthy() {
  local t0=$SECONDS
  while [ $((SECONDS - t0)) -lt 300 ]; do
    b=$(docker inspect -f '{{.State.Health.Status}}' "$("${DC[@]}" ps -q bot)" 2>/dev/null)
    p=$(docker inspect -f '{{.State.Health.Status}}' "$("${DC[@]}" ps -q proxy)" 2>/dev/null)
    [ "$b" = healthy ] && [ "$p" = healthy ] && { echo "healthy after $((SECONDS - t0))s"; return 0; }
    sleep 3
  done
  echo "bot=$b proxy=$p"; "${DC[@]}" logs --tail 40 bot; return 1
}

login() {
  rm -f "$JAR"
  curl -s -o /dev/null -w '%{http_code}' -c "$JAR" --data-urlencode "password=$PW" \
    --data-urlencode "next=/" "$BASE/_gate/login"
}

# chat <session-user> <message> -> prints the assistant text.
# Retries for up to ~60 s while the gateway reports it is still starting.
chat() {
  local out i
  for i in $(seq 1 12); do
    out=$(chat_once "$@")
    case "$out" in *"Please retry"*|*"is starting"*) echo "(gateway still starting, retrying)" >&2; sleep 5 ;; *) break ;; esac
  done
  printf '%s\n' "$out"
}
chat_once() {
  curl -s -b "$JAR" --max-time 600 "$BASE/v1/chat/completions" -H 'Content-Type: application/json' \
    -d "$(python3 -I -c 'import json,sys;print(json.dumps({"model":"openclaw/default","user":sys.argv[1],"messages":[{"role":"user","content":sys.argv[2]}]}))' "$1" "$2")" |
    python3 -I -c 'import json,sys
r=sys.stdin.read()
try:
  d=json.loads(r); print(d["choices"][0]["message"]["content"] or "")
except Exception: print("RAW:", r[:600])'
}

# The model proxy has no published port: its counters are read from inside its
# own container. usage -> the /usage JSON; num <json> <dotted.key> -> one value.
usage() {
  local out i
  for i in $(seq 1 20); do
    out=$("${DC[@]}" exec -T modelproxy python3 -I -c 'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:9001/usage",timeout=5).read().decode())' 2>/dev/null) && [ -n "$out" ] && break
    sleep 1
  done
  printf '%s\n' "$out"
}
num() { python3 -I -c 'import json,sys
d=json.loads(sys.argv[1] or "{}")
for k in sys.argv[2].split("."): d=d.get(k, 0) if isinstance(d, dict) else 0
print(d if d is not None else "")' "$1" "$2"; }

# sees_key: "yes" if the model API key occurs in what is piped in, else "no".
# The key is read from the model proxy's own environment into a private temp
# file and compared on this side: it is never printed, never on a command
# line, and never passed into the agent container.
sees_key() { if grep -a -q -F -f "$KEYFILE"; then echo yes; else echo no; fi; }

step "build (Docker only) + start; brain: $BRAIN"
wait_mem
[ "$FRESH" = 1 ] && "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1
"${DC[@]}" build -q || { echo build failed; exit 1; }
"${DC[@]}" up -d || exit 1
check "bot and proxy healthy" wait_healthy

# Telegram: only when a token reaches the container. The token is read from
# the container environment by node; it never appears on a command line.
tg=$("${DC[@]}" exec -T bot node -e '
const t = process.env.TELEGRAM_BOT_TOKEN || "";
if (!t) { console.log("no-token"); process.exit(0); }
fetch("https://api.telegram.org/bot" + t + "/getMe", { signal: AbortSignal.timeout(10000) })
  .then((r) => r.json()).then((j) => console.log(j.ok ? "getMe ok: @" + j.result.username : "getMe failed: " + (j.description || "?")))
  .catch((e) => console.log("getMe error: " + e.message));' 2>&1)
if [ "$tg" != no-token ]; then
  step "telegram"
  echo "$tg"
  check "telegram token valid (getMe)" bash -c '[[ "$0" == "getMe ok"* ]]' "$tg"
  sleep 5
  tgl=$("${DC[@]}" logs bot 2>&1 | grep -i telegram | grep -viE "token|bot[0-9]+:" | tail -3 | cut -c1-200)
  echo "gateway log (telegram lines): ${tgl:-none}"
fi

step "password gate"
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Accept: text/html' "$BASE/")
check "no cookie: / redirects to login (got $code)" test "$code" = 302
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/desktop/")
check "no cookie: /desktop/ refused (got $code)" test "$code" = 401
code=$(curl -s -o /dev/null -w '%{http_code}' --data-urlencode "password=wrong-$RANDOM" "$BASE/_gate/login")
check "wrong password rejected (got $code)" test "$code" = 401
code=$(login)
check "right password accepted (got $code)" test "$code" = 303

step "web chat UI through the proxy"
page=$(curl -s -b "$JAR" "$BASE/")
check "control UI served: $(echo "$page" | grep -o '<title>[^<]*</title>' | head -1)" \
  bash -c '[[ "$0" == *"<title>"* ]]' "$page"

step "model reply through the model proxy ($BRAIN brain, model $(envval MODEL_NAME)) + token counters"
u0=$(usage)
reply=$(chat smoke-memory "Please remember this code word for later: PELICAN-42. Reply with one short friendly sentence.")
echo "reply: ${reply:0:400}"
check "non-empty model reply" bash -c '[ -n "$0" ] && [[ "$0" != RAW:* ]]' "$reply"
u1=$(usage)
echo "model proxy counters: requests $(num "$u0" total.requests) -> $(num "$u1" total.requests)," \
  "prompt tokens $(num "$u0" total.prompt_tokens) -> $(num "$u1" total.prompt_tokens)," \
  "completion tokens $(num "$u0" total.completion_tokens) -> $(num "$u1" total.completion_tokens)," \
  "today ($(num "$u1" today)) total $(num "$u1" day.total_tokens)"
check "request counter increased" test "$(num "$u1" total.requests)" -gt "$(num "$u0" total.requests)"
check "prompt and completion token counters increased" test "$(num "$u1" total.prompt_tokens)" -gt "$(num "$u0" total.prompt_tokens)" \
  -a "$(num "$u1" total.completion_tokens)" -gt "$(num "$u0" total.completion_tokens)"
check "today's counter increased too" test "$(num "$u1" day.total_tokens)" -gt "$(num "$u0" day.total_tokens)"
if [ "$BRAIN" = scripted ]; then
  check "the reply came from the scripted model (so the model proxy put the real key on the request)" \
    bash -c '[[ "$0" == *"code word PELICAN-42"* ]]' "$reply"
  check "tokens were taken from the upstream usage field, not estimated" \
    test "$(num "$u1" total.estimated_requests)" = "$(num "$u0" total.estimated_requests)"
fi

RAN_TOOLS=1
if [ "$BRAIN" = scripted ]; then
  step "agent uses its computer: shell + file + browser (scripted model, through the model proxy)"
  reply=$(chat smoke-tools "Use your computer: run a shell command that writes a file in your workspace, then open https://example.com in your browser.")
  echo "reply: ${reply:0:700}"
  check "agent turn completed with tool results" bash -c '[[ "$0" == *SCRIPTED-BRAIN\ DONE* ]]' "$reply"
elif [ "$TOOLS" = true ]; then
  step "agent uses its computer: shell + file + browser (the configured model)"
  reply=$(chat smoke-tools "Please use your own computer for two small things. 1) With your shell (the exec tool), run exactly this command: echo \"created by the bot's shell at \$(date -u +%FT%TZ)\" > /home/node/workspace/smoke-file.txt && cat /home/node/workspace/smoke-file.txt  2) With your browser tool, open https://example.com and read the page title. Then reply in two short lines: the command output, and the page title.")
  echo "reply: ${reply:0:700}"
  check "agent turn completed" bash -c '[ -n "$0" ] && [[ "$0" != RAW:* ]]' "$reply"
else
  RAN_TOOLS=0
  step "agent uses its computer: SKIPPED (the configured model cannot call tools; the default scripted brain covers this)"
fi
if [ "$RAN_TOOLS" = 1 ]; then
  file1=$("${DC[@]}" exec -T bot cat /home/node/workspace/smoke-file.txt 2>&1)
  echo "file: $file1"
  check "file created by the agent's shell exists" bash -c '[[ "$0" == "created by the bot"* ]]' "$file1"
  win=$("${DC[@]}" exec -T bot sh -c 'DISPLAY=:1 xdotool search --name "Example Domain" getwindowname %@ 2>/dev/null | head -3')
  echo "desktop windows: $win"
  check "page open in the bot's visible browser (window title)" bash -c '[[ "$0" == *"Example Domain"* ]]' "$win"
fi

step "real browser: web chat websocket + a message round trip (one model request)"
# A throwaway container from the bot image (Chromium + playwright-core) acts as
# the user's browser on the stack's network. The password is passed by name
# (-e ACCESS_PASSWORD), never on a command line.
UI_NET=${SMOKE_UI_NETWORK:-${PROJECT}_front}
UI_BASE=${SMOKE_UI_BASE:-http://proxy:8443}
BOT_IMG=$(envval BOT_IMAGE); BOT_IMG=${BOT_IMG:-fugal-bot-bot:stage0}   # same default as compose.yaml
uiout=$(ACCESS_PASSWORD="$PW" UI_BASE="$UI_BASE" UI_MESSAGE="Hello! Please reply with one short sentence." \
  timeout 420 docker run --rm -i --network "$UI_NET" --memory 1g --cap-drop ALL --security-opt no-new-privileges \
  -e ACCESS_PASSWORD -e UI_BASE -e UI_MESSAGE --entrypoint node "$BOT_IMG" --input-type=module - \
  < scripts/ui-check.mjs 2>&1)
echo "$uiout" | grep -v "^PASS\|^FAIL"
while IFS= read -r line; do
  case "$line" in PASS*) ok "${line#PASS  }" ;; FAIL*) bad "${line#FAIL  }" ;; esac
done <<< "$uiout"

step "desktop view (noVNC) through the proxy"
code=$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" "$BASE/desktop/vnc.html")
check "noVNC page served (got $code)" test "$code" = 200
ws=$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" --max-time 4 --http1.1 \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: c21va2UtdGVzdC1rZXkxMg==' -H 'Sec-WebSocket-Protocol: binary' \
  "$BASE/desktop/websockify")
check "desktop websocket upgrades (got $ws)" test "$ws" = 101

step "the model API key stays out of the agent container"
"${DC[@]}" exec -T modelproxy printenv MODEL_API_KEY > "$KEYFILE" 2>/dev/null
if [ "$(wc -c < "$KEYFILE")" -lt 9 ]; then
  echo "SKIPPED: no model API key is configured (nothing to hide)"
else
  bot_id=$("${DC[@]}" ps -q bot)
  check "control: this check does find the key where it lives (the model proxy's environment)" \
    test "$("${DC[@]}" exec -T modelproxy cat /proc/1/environ | sees_key)" = yes
  holder=$("${DC[@]}" exec -T bot printenv MODEL_API_KEY)
  check "the agent's MODEL_API_KEY is the placeholder ($holder)" test "$holder" = held-by-modelproxy
  check "not in the agent container's environment" test "$("${DC[@]}" exec -T bot env | sees_key)" = no
  check "not in any process environment or command line there (/proc)" \
    test "$("${DC[@]}" exec -T bot sh -c 'cat /proc/[0-9]*/environ /proc/[0-9]*/cmdline 2>/dev/null' | sees_key)" = no
  check "not in any file there: data directory /home/node (config, sessions, logs), /tmp, /run, /dev/shm, our scripts" \
    test "$("${DC[@]}" exec -T bot sh -c 'tar -cf - /home/node /tmp /run /dev/shm /opt/fugal-bot /etc/fugal-bot 2>/dev/null' | sees_key)" = no
  check "not in the agent container's Docker configuration" test "$(docker inspect "$bot_id" | sees_key)" = no
  check "not in the model proxy's replies to the agent (/usage, /healthz, an error)" \
    test "$("${DC[@]}" exec -T bot node -e '
const base = process.env.MODEL_BASE_URL.replace(/\/v1$/, "");
Promise.all(["/usage", "/healthz", "/v1/files"].map((p) => fetch(base + p).then(async (r) => r.status + " " + [...r.headers].join(" ") + " " + (await r.text()))))
  .then((all) => console.log(all.join("\n")), (e) => { console.log("fetch failed: " + e.message); process.exit(1); });' | sees_key)" = no
fi
: > "$KEYFILE"

step "the model proxy is not reachable from outside"
body=$(curl -s -b "$JAR" "$BASE/usage")
check "/usage through the public proxy is not the model proxy's" bash -c '[[ "$0" != *rejected_since_start* ]]' "$body"
if [ "$ONE_NETNS" = 0 ]; then
  check "the public proxy's container cannot connect to the model proxy" \
    bash -c '! "$@" >/dev/null 2>&1' _ "${DC[@]}" exec -T proxy wget -q -T 4 -O /dev/null http://modelproxy:9001/healthz
  check "the gate's container cannot either" \
    bash -c '! "$@" >/dev/null 2>&1' _ "${DC[@]}" exec -T gate python3 -I -c 'import urllib.request;urllib.request.urlopen("http://modelproxy:9001/healthz",timeout=4)'
  ports=$(docker inspect -f '{{json .NetworkSettings.Ports}}' "$("${DC[@]}" ps -q modelproxy)")
  check "the model proxy publishes no host port ($ports)" test "$ports" = "{}"
else
  echo "(one shared network namespace: every local process can call 127.0.0.1:9001; see SECURITY-NOTES.md)"
fi

step "stop, start again, persistence"
# Re-read: the browser check's turn may have rewritten the file.
file1=$("${DC[@]}" exec -T bot cat /home/node/workspace/smoke-file.txt 2>&1)
u4=$(usage)
"${DC[@]}" down || true
"${DC[@]}" up -d || exit 1
check "healthy again after restart" wait_healthy
login >/dev/null
if [ "$RAN_TOOLS" = 1 ]; then
  file2=$("${DC[@]}" exec -T bot cat /home/node/workspace/smoke-file.txt 2>&1)
  check "earlier file still there: $file2" test "$file1" = "$file2"
fi
u5=$(usage)
check "token counters survived the restart (total $(num "$u4" total.total_tokens) -> $(num "$u5" total.total_tokens))" \
  test "$(num "$u5" total.total_tokens)" -ge "$(num "$u4" total.total_tokens)" -a "$(num "$u5" total.total_tokens)" -gt 0
hits=$("${DC[@]}" exec -T bot sh -c 'grep -rl "PELICAN-42" /home/node/.openclaw 2>/dev/null | grep -v "\.json\.tmp" | head -3')
echo "transcript files containing the code word: ${hits:-none}"
check "conversation stored on the volume" test -n "$hits"
reply=$(chat smoke-memory "What was the code word I asked you to remember? Answer with just the code word.")
echo "reply: ${reply:0:300}"
check "model recalls the code word after restart" bash -c '[[ "$0" == *PELICAN-42* || "$0" == *"PELICAN 42"* || "$0" == *Pelican-42* ]]' "$reply"

# Last on purpose: a refusal changes how the agent treats its model provider.
# With the default reply (MODELPROXY_EXHAUSTED_REPLY=400) the next call goes
# through at once; the 429 and 402 forms make OpenClaw switch the provider off
# for ten minutes (docs/MODELPROXY.md), which would fail any step after this.
step "budget: with a very low cap the model proxy refuses (BUDGET_TOKENS_TOTAL=1)"
BUDGET_TOKENS_TOTAL=1 "${DC[@]}" up -d --no-deps modelproxy 2>&1 | tail -1
u6=$(usage)
check "the cap is in force (exceeded: $(num "$u6" exceeded))" test "$(num "$u6" exceeded)" = tokens_total
direct=$("${DC[@]}" exec -T bot node -e '
fetch(process.env.MODEL_BASE_URL + "/chat/completions", { method: "POST",
  headers: { "content-type": "application/json", authorization: "Bearer " + process.env.MODEL_API_KEY },
  body: JSON.stringify({ model: "any", messages: [{ role: "user", content: "hi" }] }) })
  .then(async (r) => { const e = (await r.json()).error; console.log(r.status + " " + e.code + " | " + e.message); })
  .catch((e) => console.log("fetch failed: " + e.message));' 2>&1)
echo "model proxy answered the agent container: $direct"
check "OpenAI-style error: HTTP 400, code allowance_exhausted" bash -c '[[ "$0" == "400 allowance_exhausted | "*"used up its whole inference allowance"* ]]' "$direct"
reply=$(chat smoke-memory "Hello, are you there?")
echo "what the user is told: ${reply:0:400}"
check "the bot tells the user why, in the model proxy's words, instead of answering" \
  bash -c '[[ "$0" != *"scripted test model"* ]] && [[ "$0" == *"used up its whole inference allowance"* ]]' "$reply"
u7=$(usage)
check "refused requests were not counted as usage" test "$(num "$u7" total.total_tokens)" = "$(num "$u6" total.total_tokens)"
check "each refused message cost one request, no retries (refused since start: $(num "$u7" rejected_since_start.budget))" \
  test "$(num "$u7" rejected_since_start.budget)" = 2
"${DC[@]}" up -d --no-deps modelproxy 2>&1 | tail -1    # back to the configured budget
u8=$(usage)
check "cap lifted again (exceeded: $(num "$u8" exceeded))" test -z "$(num "$u8" exceeded)"
reply=$(chat smoke-memory "What was the code word I asked you to remember? Answer with just the code word.")
echo "reply: ${reply:0:300}"
check "the very next message is answered (the agent did not lock its model provider out)" \
  bash -c '[[ "$0" == *PELICAN-42* ]]' "$reply"

echo; echo "== result: $PASS passed, $FAIL failed"
if [ "$KEEP" = 0 ]; then "${DC[@]}" down >/dev/null 2>&1; echo "(stopped; volume ${PROJECT}_botdata kept; --fresh wipes it next run)"; fi
[ "$FAIL" = 0 ]
