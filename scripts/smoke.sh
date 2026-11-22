#!/usr/bin/env bash
# Stage-0 smoke test. Builds and runs the bot in Docker only (separate compose
# project + volume from your real bot), then checks, through the password proxy:
#   login gate, web chat UI, a model reply from the configured brain (.env),
#   one shell command + one file created by the agent (scripted tool brain),
#   one page loaded in the bot's visible browser, the desktop (noVNC) endpoint,
#   then stop + start and that the file and the conversation survived.
# No paid calls: the brain is whatever .env says (default: local Ollama) plus
# the test-only scripted brain. Usage: scripts/smoke.sh [--keep] [--fresh]
set -uo pipefail
cd "$(dirname "$0")/.."

PROJECT=privatebot-smoke
export PORT=${SMOKE_PORT:-18080} BIND_ADDRESS=127.0.0.1 PUBLIC_HOSTNAME=
DC=(docker compose -p "$PROJECT" -f compose.yaml -f scripts/smoke.compose.yaml)
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

step "model reply from the configured brain ($(grep -E '^MODEL_NAME=' .env | cut -d= -f2-))"
reply=$(chat smoke-memory "Please remember this code word for later: PELICAN-42. Reply with one short friendly sentence.")
echo "reply: ${reply:0:400}"
check "non-empty model reply" bash -c '[ -n "$0" ] && [[ "$0" != RAW:* ]]' "$reply"

step "agent uses its computer: shell + file + browser (scripted tool brain)"
reply=$(chat smoke-tools "Use your computer: run a shell command that writes a file in your workspace, then open https://example.com in your browser." scripted/tools)
echo "reply: ${reply:0:700}"
check "agent turn completed with tool results" bash -c '[[ "$0" == *SCRIPTED-BRAIN\ DONE* ]]' "$reply"
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
