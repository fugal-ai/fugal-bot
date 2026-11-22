#!/usr/bin/env bash
# Stage-0 smoke test. Builds and runs the bot in Docker only (separate compose
# project + volume from your real bot), then checks, through the password proxy:
#   login gate, web chat UI, a model reply from the configured brain (.env),
#   one shell command + one file created by the agent (scripted tool brain),
#   one page loaded in the bot's visible browser, the desktop (noVNC) endpoint,
#   then stop + start and that the file and the conversation survived.
#
# Brain: whatever .env says. If that model cannot call tools
# (MODEL_SUPPORTS_TOOLS=false, e.g. local Ollama gemma3:4b), the computer-use
# step is driven by the test-only scripted brain instead (no paid call).
# A hosted model costs money per request: one run makes roughly 5-10 requests.
#
# Optional environment:
#   SMOKE_SECRETS_FILE=path   extra env file with secrets (MODEL_API_KEY,
#                             TELEGRAM_BOT_TOKEN); read by compose, never printed
#   MODEL_BASE_URL / MODEL_NAME / MODEL_SUPPORTS_TOOLS   override .env
# Usage: scripts/smoke.sh [--keep] [--fresh]
set -uo pipefail
cd "$(dirname "$0")/.."

PROJECT=fugal-bot-smoke
export PORT=${SMOKE_PORT:-18080} BIND_ADDRESS=127.0.0.1 PUBLIC_HOSTNAME=
ENVF=(--env-file .env)
[ -n "${SMOKE_SECRETS_FILE:-}" ] && ENVF+=(--env-file "$SMOKE_SECRETS_FILE")
DC=(docker compose -p "$PROJECT" "${ENVF[@]}" -f compose.yaml -f scripts/smoke.compose.yaml)
envval() { local v; v=$(printenv "$1"); [ -n "$v" ] && { echo "$v"; return; }; grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }
TOOLS=$(envval MODEL_SUPPORTS_TOOLS); TOOLS=${TOOLS:-true}
BASE="http://127.0.0.1:${PORT}"
JAR=$(mktemp); trap 'rm -f "$JAR"' EXIT
KEEP=0; FRESH=0
for a in "$@"; do case "$a" in --keep) KEEP=1 ;; --fresh) FRESH=1 ;; esac; done

[ -f .env ] || { echo "no .env (cp .env.example .env)"; exit 2; }
PW=$(grep -E '^ACCESS_PASSWORD=' .env | head -1 | cut -d= -f2-)
[ -n "$PW" ] || { echo "ACCESS_PASSWORD empty in .env"; exit 2; }

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

# chat <session-user> <message> [model-override] -> prints the assistant text
chat() {
  local hdr=()
  [ -n "${3:-}" ] && hdr=(-H "x-openclaw-model: $3")
  curl -s -b "$JAR" --max-time 600 "$BASE/v1/chat/completions" -H 'Content-Type: application/json' \
    "${hdr[@]}" -d "$(python3 -I -c 'import json,sys;print(json.dumps({"model":"openclaw/default","user":sys.argv[1],"messages":[{"role":"user","content":sys.argv[2]}]}))' "$1" "$2")" |
    python3 -I -c 'import json,sys
r=sys.stdin.read()
try:
  d=json.loads(r); print(d["choices"][0]["message"]["content"] or "")
except Exception: print("RAW:", r[:600])'
}

step "build (Docker only) + start"
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

step "model reply from the configured brain ($(envval MODEL_NAME))"
reply=$(chat smoke-memory "Please remember this code word for later: PELICAN-42. Reply with one short friendly sentence.")
echo "reply: ${reply:0:400}"
check "non-empty model reply" bash -c '[ -n "$0" ] && [[ "$0" != RAW:* ]]' "$reply"

if [ "$TOOLS" = true ]; then
  step "agent uses its computer: shell + file + browser (the configured model)"
  reply=$(chat smoke-tools "Please use your own computer for two small things. 1) With your shell (the exec tool), run exactly this command: echo \"created by the bot's shell at \$(date -u +%FT%TZ)\" > /home/node/workspace/smoke-file.txt && cat /home/node/workspace/smoke-file.txt  2) With your browser tool, open https://example.com and read the page title. Then reply in two short lines: the command output, and the page title.")
  echo "reply: ${reply:0:700}"
  check "agent turn completed" bash -c '[ -n "$0" ] && [[ "$0" != RAW:* ]]' "$reply"
else
  step "agent uses its computer: shell + file + browser (test-only scripted brain; model cannot call tools)"
  reply=$(chat smoke-tools "Use your computer: run a shell command that writes a file in your workspace, then open https://example.com in your browser." scripted/tools)
  echo "reply: ${reply:0:700}"
  check "agent turn completed with tool results" bash -c '[[ "$0" == *SCRIPTED-BRAIN\ DONE* ]]' "$reply"
fi
file1=$("${DC[@]}" exec -T bot cat /home/node/workspace/smoke-file.txt 2>&1)
echo "file: $file1"
check "file created by the agent's shell exists" bash -c '[[ "$0" == "created by the bot"* ]]' "$file1"
win=$("${DC[@]}" exec -T bot sh -c 'DISPLAY=:1 xdotool search --name "Example Domain" getwindowname %@ 2>/dev/null | head -3')
echo "desktop windows: $win"
check "page open in the bot's visible browser (window title)" bash -c '[[ "$0" == *"Example Domain"* ]]' "$win"

step "desktop view (noVNC) through the proxy"
code=$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" "$BASE/desktop/vnc.html")
check "noVNC page served (got $code)" test "$code" = 200
ws=$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" --max-time 4 --http1.1 \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: c21va2UtdGVzdC1rZXkxMg==' -H 'Sec-WebSocket-Protocol: binary' \
  "$BASE/desktop/websockify")
check "desktop websocket upgrades (got $ws)" test "$ws" = 101

step "stop, start again, persistence"
"${DC[@]}" down || true
"${DC[@]}" up -d || exit 1
check "healthy again after restart" wait_healthy
login >/dev/null
file2=$("${DC[@]}" exec -T bot cat /home/node/workspace/smoke-file.txt 2>&1)
check "earlier file still there: $file2" test "$file1" = "$file2"
hits=$("${DC[@]}" exec -T bot sh -c 'grep -rl "PELICAN-42" /home/node/.openclaw 2>/dev/null | grep -v "\.json\.tmp" | head -3')
echo "transcript files containing the code word: ${hits:-none}"
check "conversation stored on the volume" test -n "$hits"
reply=$(chat smoke-memory "What was the code word I asked you to remember? Answer with just the code word.")
echo "reply: ${reply:0:300}"
check "model recalls the code word after restart" bash -c '[[ "$0" == *PELICAN-42* || "$0" == *"PELICAN 42"* || "$0" == *Pelican-42* ]]' "$reply"

echo; echo "== result: $PASS passed, $FAIL failed"
if [ "$KEEP" = 0 ]; then "${DC[@]}" down >/dev/null 2>&1; echo "(stopped; volume ${PROJECT}_botdata kept; --fresh wipes it next run)"; fi
[ "$FAIL" = 0 ]
