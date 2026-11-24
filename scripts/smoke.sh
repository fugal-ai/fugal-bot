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
#   In between, the gate and the proxy are restarted once in hosted mode
#   (GATE_SSO=on, with a throwaway password): sign-in by one-time token, the
#   bot in a frame on another origin of the same site, who may frame it, what
#   a sibling host can do with the cookie, sign-out. SMOKE_SSO=0 skips this.
#
# First conversation (scripted brain, new data directory): greeting, a `taoq`
#   chain read in the bot's shell (SMOKE_TAOQ=live, or fixture for no network),
#   a schedule made with OpenClaw's automations tool and run once, BOOTSTRAP.md
#   retired (scripts/first-conversation.sh).
#
# Also (bot/seed/, bot/backup.sh, bot/restore.sh): with the scripted brain the
#   bot gets a name, an emoji, an accent colour, first-run files, a model
#   extra-body field and BACKGROUND_MODEL_USE=off, and the test checks each of
#   them (the name in the real web UI); with any brain it takes an encrypted
#   hot backup to a throwaway local object store, restores it into an empty
#   volume, and takes a cold backup there (scripts/backup-check.sh).
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

# ---- identity, first-run files, model extras (scripted brain only) -------------
# Test values; a real bot gets these from .env. Not with a real model: it would
# act on the first-conversation script instead of answering the test.
SEED_TEST=0
if [ "$BRAIN" = scripted ]; then
  SEED_TEST=1
  export BOT_DISPLAY_NAME="Smoke Otter" BOT_EMOJI="🦦" UI_ACCENT_COLOR="#2f6fed" SEED_WORKSPACE=1
  # SN70's owner coldkey (public, on chain as SubnetOwner(70)), so the first
  # conversation reads a real stake position; the recorded read in
  # bot/tools/fixtures/finney.json.gz is for this address.
  export OWNER_WALLET_SS58=5GbydRVj98hQZvR2cf4t8WtcoUVjAuJykxxuqauiJpJqwB7m
  # The chain tool in the first conversation: live (finney) or fixture (no network).
  SMOKE_TAOQ=${SMOKE_TAOQ:-live}
  # The news in the briefing always comes from the recorded feed answers (first-conversation.sh writes them).
  export BRAIN_TAOQ_ENV="TAOQ_RECORD=/tmp/taoq-record.json TAOQ_FEED_FIXTURE=/tmp/taoq-feed.json"
  [ "$SMOKE_TAOQ" = fixture ] && export BRAIN_TAOQ_ENV="$BRAIN_TAOQ_ENV TAOQ_FIXTURE=/tmp/taoq-fixture.json.gz TAOQ_ENDPOINTS=ws://127.0.0.1:9"
  export MODEL_INPUT=text MODEL_EXTRA_BODY='{"chat_template_kwargs":{"enable_thinking":false}}'
  export BACKGROUND_MODEL_USE=off TOOLS_DENY=
fi
# --------------------------------------------------------------------------------
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

# ---- first-run files as the entrypoint left them, before any conversation ------
seedfiles() { "${DC[@]}" exec -T bot sh -c 'cd /home/node/workspace 2>/dev/null && sha256sum IDENTITY.md SOUL.md USER.md AGENTS.md BOOTSTRAP.md 2>&1'; }
seedlog() { "${DC[@]}" logs bot 2>&1 | grep -a 'workspace seed:' | tail -1 | sed 's/.*workspace seed: //'; }
# pf <output>: count the PASS/FAIL lines a check script printed, show the rest.
pf() {
  echo "$1" | grep -v "^PASS\|^FAIL"
  local line
  while IFS= read -r line; do
    case "$line" in PASS*) ok "${line#PASS  }" ;; FAIL*) bad "${line#FAIL  }" ;; esac
  done <<< "$1"
}
SEED_LOG=$(seedlog); SEED_SUMS0=$(seedfiles)
# --------------------------------------------------------------------------------

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

# ---- identity, first-run files, model extras ----------------------------------
if [ "$SEED_TEST" = 1 ]; then
  step "identity, first-run files, model extras (BOT_DISPLAY_NAME, SEED_WORKSPACE, MODEL_EXTRA_BODY, BACKGROUND_MODEL_USE)"
  echo "entrypoint: $SEED_LOG"
  case "$SEED_LOG" in
    *"BOOTSTRAP.md=written"*)
      check "new data directory: all five starter files were seeded" \
        bash -c '[[ "$0" == "IDENTITY.md=written, SOUL.md=written, USER.md=written, AGENTS.md=written, BOOTSTRAP.md=written" ]]' "$SEED_LOG"
      boot=$("${DC[@]}" exec -T bot cat /home/node/workspace/BOOTSTRAP.md 2>&1)
      check "BOOTSTRAP.md: the first message opens with the bot's name and emoji, says it never asks for a seed phrase or private key, and offers the daily briefing for the wallet the owner signed in with" \
        bash -c '[[ "$0" == *"Hi, I'"'"'m $1 $2, your own always-on agent, with my own computer and browser."* && "$0" == *"I'"'"'ll never ask for a seed phrase or private key. I only read public chain data."* && "$0" == *"Want a **daily briefing** for the wallet you signed in with: your stake, what changed on your subnets, and the day'"'"'s Bittensor news? Tell me your time zone"* && "$0" != *"{{"* ]]' \
        "$boot" "$BOT_DISPLAY_NAME" "$BOT_EMOJI"
      userfile=$("${DC[@]}" exec -T bot cat /home/node/workspace/USER.md 2>&1)
      greet=$(printf '%s' "$boot" | sed -n "/^Hi, I'm/,/^Or just tell me what you need\./p")
      check "the first message does not print the owner's address; the bot has it in USER.md" \
        bash -c '[ -n "$0" ] && [[ "$0" != *"$2"* && "$1" == *"$2"* ]]' "$greet" "$userfile" "$OWNER_WALLET_SS58"
      check "after the conversations above OpenClaw has left all five exactly as seeded (BOOTSTRAP.md still waiting)" \
        test -n "$SEED_SUMS0" -a "$(seedfiles)" = "$SEED_SUMS0"
      ;;
    *) echo "(not a new data directory, so BOOTSTRAP.md was not seeded at this start; --fresh checks the first-run files)" ;;
  esac
  blog=$("${DC[@]}" logs brain 2>&1)
  with=$(grep -a -c 'extra_fields={"chat_template_kwargs":{"enable_thinking":false}}' <<< "$blog")
  without=$(grep -a -c 'extra_fields=none' <<< "$blog")
  check "every model request so far carried the MODEL_EXTRA_BODY field ($with with it, $without without)" test "$with" -gt 0 -a "$without" = 0
  jobs=$("${DC[@]}" exec -T bot node -e '
const { DatabaseSync } = require("node:sqlite");
try {
  const db = new DatabaseSync("/home/node/.openclaw/state/openclaw.sqlite", { readOnly: true });
  const all = db.prepare("select name, declaration_key from cron_jobs").all();
  const dreaming = all.filter((j) => /dreaming/i.test((j.name || "") + " " + (j.declaration_key || "")));
  console.log("scheduled jobs: " + all.length + ", dreaming: " + dreaming.length);
} catch (e) { console.log("could not read the job table: " + e.message); }' 2>/dev/null)
  check "BACKGROUND_MODEL_USE=off: no memory \"dreaming\" job is scheduled ($jobs)" bash -c '[[ "$0" == *", dreaming: 0" ]]' "$jobs"
  idout=$(ACCESS_PASSWORD="$PW" UI_BASE="$UI_BASE" UI_EXPECT_NAME="$BOT_DISPLAY_NAME" UI_EXPECT_EMOJI="$BOT_EMOJI" UI_EXPECT_ACCENT="$UI_ACCENT_COLOR" \
    timeout 240 docker run --rm -i --network "$UI_NET" --memory 1g --cap-drop ALL --security-opt no-new-privileges \
    -e ACCESS_PASSWORD -e UI_BASE -e UI_EXPECT_NAME -e UI_EXPECT_EMOJI -e UI_EXPECT_ACCENT --entrypoint node "$BOT_IMG" --input-type=module - \
    < scripts/ui-identity-check.mjs 2>&1)
  pf "$idout"
fi
# --------------------------------------------------------------------------------

step "desktop view (noVNC) through the proxy"
code=$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" "$BASE/desktop/vnc.html")
check "noVNC page served (got $code)" test "$code" = 200
ws=$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" --max-time 4 --http1.1 \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: c21va2UtdGVzdC1rZXkxMg==' -H 'Sec-WebSocket-Protocol: binary' \
  "$BASE/desktop/websockify")
check "desktop websocket upgrades (got $ws)" test "$ws" = 101

# ==== hosted sign-in (GATE_SSO=on) ================================================
# Self-contained: it restarts the gate and the proxy with a throwaway password
# in hosted mode, runs its checks, and puts the password gate back.
if [ "${SMOKE_SSO:-1}" = 1 ]; then
  step "hosted mode (GATE_SSO=on): sign-in by one-time token, password refused, framing"
  SSO_PW=$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 43)     # throwaway, never in .env
  SSO_BOT=b$(head -c 256 /dev/urandom | base64 | tr -dc 'a-z2-7' | head -c 10)
  SSO_SITE=https://fugal.test:8443            # stand-in for the site (scripts/sso-edge.Caddyfile)
  SSO_BOT_URL=https://b-smoke.fugal.test:8443 # the bot as a browser reaches it, same-site with the above
  SSO_TMP=$(mktemp -d)
  sso_mode() { ACCESS_PASSWORD="$SSO_PW" GATE_SSO=on GATE_BOT_ID="$SSO_BOT" GATE_LOGIN_URL="$SSO_SITE/bot" \
    GATE_PARENT_ORIGIN="$SSO_SITE" SESSION_HOURS=24 "$@"; }
  # sso_token [ttl] [bot id] -> a token as the site's controller mints it. The
  # password is read from the environment, never from a command line.
  sso_token() { SSO_PW="$SSO_PW" python3 -I -c '
import base64, hashlib, hmac, os, secrets, sys, time
signed = "v1.%s.%d.%s" % (sys.argv[2], int(time.time()) + int(sys.argv[1]), secrets.token_hex(16))
key = hashlib.sha256(b"fugal-bot-gate-sso-v1\0" + os.environ["SSO_PW"].encode()).digest()
mac = hmac.new(key, signed.encode(), hashlib.sha256).digest()
print(signed + "." + base64.urlsafe_b64encode(mac).rstrip(b"=").decode())' "${1:-60}" "${2:-$SSO_BOT}"; }
  # sso_post <token> [Origin or "-" for none] -> HTTP code; reply headers in $SSO_TMP/h, cookie in $JAR
  sso_post() { local o=(-H "Origin: ${2:-$SSO_SITE}"); [ "${2:-}" = - ] && o=()
    printf '%s' "$1" | curl -s -o /dev/null -D "$SSO_TMP/h" -c "$JAR" -w '%{http_code}' "${o[@]}" \
      --data-urlencode token@- --data-urlencode "next=/" "$BASE/_gate/sso"; }
  # hdr <name> -> the values of that header in the last reply ($SSO_TMP/h), one per line
  hdr() { tr -d '\r' < "$SSO_TMP/h" | grep -i "^$1:" | sed -E 's/^[^:]*: *//' || true; }
  # frames_ok <Content-Security-Policy values> <X-Frame-Options value>: every policy
  # carries our rule, none forbids framing, and there is no X-Frame-Options.
  frames_ok() { [ -n "$1" ] && [ -z "$2" ] || return 1
    [ "$(printf '%s\n' "$1" | grep -c -F "frame-ancestors 'self' $SSO_SITE")" = "$(printf '%s\n' "$1" | grep -c .)" ] &&
      ! printf '%s\n' "$1" | grep -q -F "frame-ancestors 'none'"; }

  sso_mode "${DC[@]}" up -d --no-deps gate proxy 2>&1 | tail -1
  check "gate and proxy healthy in hosted mode" wait_healthy
  rm -f "$JAR"

  code=$(curl -s -o "$SSO_TMP/b" -D "$SSO_TMP/h" -w '%{http_code}' -H 'Accept: text/html' "$BASE/")
  check "no session, browser: a page, not a redirect (got $code, Location: '$(hdr location)')" \
    bash -c '[ "$0" = 401 ] && [ -z "$1" ] && grep -q "Signing you in" "$2" && ! grep -q "name=\"password\"" "$2"' "$code" "$(hdr location)" "$SSO_TMP/b"
  check "that page may be framed by this host and $SSO_SITE only: $(hdr content-security-policy | grep -o "frame-ancestors[^;]*" | head -1)" \
    frames_ok "$(hdr content-security-policy)" "$(hdr x-frame-options)"
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/desktop/")
  check "no session, not a page: /desktop/ refused (got $code)" test "$code" = 401
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 --http1.1 -H 'Accept: text/html' \
    -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
    -H 'Sec-WebSocket-Key: c21va2UtdGVzdC1rZXkxMg==' "$BASE/desktop/websockify")
  check "no session: websocket refused (got $code)" test "$code" = 401
  code=$(printf '%s' "$SSO_PW" | curl -s -o /dev/null -D "$SSO_TMP/h" -w '%{http_code}' --data-urlencode password@- --data-urlencode "next=/" "$BASE/_gate/login")
  check "the right password is refused: no password login in hosted mode (got $code)" bash -c '[ "$0" = 403 ] && [ -z "$1" ]' "$code" "$(hdr set-cookie)"
  # Four refused tokens in this step, on purpose not more: after five from one
  # address the gate answers 429 to a bad token (gate/test_gate.py covers that).
  code=$(sso_post "$(sso_token 60 baaaaaaaaaa)")
  check "a token for another bot is refused (got $code)" test "$code" = 403
  code=$(sso_post "$(sso_token -5)")
  check "an expired token is refused (got $code)" test "$code" = 403
  code=$(sso_post "$(sso_token 3600)")
  check "a token dated an hour ahead is refused (got $code)" test "$code" = 403
  tok=$(sso_token)
  code=$(sso_post "$tok" https://evil.test:8443)
  check "a good token posted from another origin is refused (got $code)" test "$code" = 403
  code=$(sso_post "$tok" null)
  check "... and from an origin of 'null' (got $code)" test "$code" = 403
  code=$(sso_post "$tok")
  setc=$(hdr set-cookie)
  check "the token posted from the site signs in (got $code, Location: $(hdr location))" test "$code" = 303 -a "$(hdr location)" = /
  echo "cookie: $(printf '%s' "$setc" | sed -E 's/(pb_session=)[^;]*/\1(value)/')"
  check "cookie: __Host- name, Secure, HttpOnly, SameSite=Strict, Path=/, no Domain" \
    bash -c '[[ "$0" == *"__Host-pb_session="* && "$0" == *"; Secure"* && "$0" == *"; HttpOnly"* && "$0" == *"; SameSite=Strict"* && "$0" == *"; Path=/;"* && "${0,,}" != *domain* ]]' "$setc"
  cp "$JAR" "$SSO_TMP/jar"
  code=$(sso_post "$tok")
  check "the same token a second time is refused (got $code)" test "$code" = 403
  cp "$SSO_TMP/jar" "$JAR"

  code=$(curl -s -o "$SSO_TMP/b" -D "$SSO_TMP/h" -w '%{http_code}' -b "$JAR" -H 'Accept: text/html' "$BASE/")
  check "with the session: control UI served (got $code) $(grep -o '<title>[^<]*</title>' "$SSO_TMP/b" | head -1)" test "$code" = 200
  echo "its framing headers: $(hdr content-security-policy | grep -o "frame-ancestors[^;]*" | tr '\n' '|') X-Frame-Options: '$(hdr x-frame-options)'"
  check "control UI: every policy says frame-ancestors 'self' $SSO_SITE; no X-Frame-Options; no frame-ancestors 'none'" \
    frames_ok "$(hdr content-security-policy)" "$(hdr x-frame-options)"
  code=$(curl -s -o /dev/null -D "$SSO_TMP/h" -w '%{http_code}' -b "$JAR" "$BASE/desktop/vnc.html")
  check "desktop page served (got $code)" test "$code" = 200
  check "desktop page: the same framing rule: $(hdr content-security-policy | grep -o "frame-ancestors[^;]*" | head -1)" \
    frames_ok "$(hdr content-security-policy)" "$(hdr x-frame-options)"
  ws_code() { curl -s -o /dev/null -w '%{http_code}' -b "$JAR" --max-time 4 --http1.1 "$@" \
    -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
    -H 'Sec-WebSocket-Key: c21va2UtdGVzdC1rZXkxMg==' -H 'Sec-WebSocket-Protocol: binary' "$BASE/desktop/websockify"; }
  ws=$(ws_code -H "Origin: $BASE")
  check "desktop websocket from the bot's own origin upgrades (got $ws)" test "$ws" = 101
  ws=$(ws_code -H "Origin: https://other.fugal.test:8443")
  check "desktop websocket from a sibling host, with the cookie, is refused (got $ws)" test "$ws" = 403
  ws=$(ws_code -H "Origin: $SSO_SITE")
  check "... and from the site itself (got $ws)" test "$ws" = 403
  code=$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" -H "Origin: https://other.fugal.test:8443" -H 'Content-Type: text/plain' \
    -d '{"model":"openclaw/default","messages":[{"role":"user","content":"from a sibling host"}]}' "$BASE/v1/chat/completions")
  check "POST to the bot's API from a sibling host, with the cookie, is refused (got $code)" test "$code" = 403
  code=$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" -H 'Sec-Fetch-Site: same-site' -H 'Sec-Fetch-Mode: no-cors' "$BASE/")
  check "a same-site request that is not a navigation is refused (got $code)" test "$code" = 403

  step "hosted mode, real browser: the bot in a frame on the site's page, over HTTPS (one model request)"
  # The stand-in for the site and for what ends TLS in front of the bot.
  SSO_EDGE="${PROJECT}-sso-edge"
  PROXY_IMG=$(envval PROXY_IMAGE); PROXY_IMG=${PROXY_IMG:-fugal-bot-proxy:stage0}   # same default as compose.yaml
  docker rm -f "$SSO_EDGE" >/dev/null 2>&1
  EDGE_CADDYFILE=$(cat scripts/sso-edge.Caddyfile) SSO_UPSTREAM="${UI_BASE#http://}" \
    docker run -d --name "$SSO_EDGE" --network "$UI_NET" --network-alias fugal.test --network-alias b-smoke.fugal.test \
    --network-alias other.fugal.test --network-alias evil.test --memory 128m --cap-drop ALL --security-opt no-new-privileges \
    --read-only --tmpfs /tmp -e XDG_DATA_HOME=/tmp/data -e XDG_CONFIG_HOME=/tmp/config -e EDGE_CADDYFILE -e SSO_UPSTREAM \
    --entrypoint /bin/sh "$PROXY_IMG" -c 'printf "%s\n" "$EDGE_CADDYFILE" > /tmp/Caddyfile && exec caddy run --config /tmp/Caddyfile --adapter caddyfile' >/dev/null
  uiout=$(ACCESS_PASSWORD="$SSO_PW" GATE_BOT_ID="$SSO_BOT" UI_BASE="$UI_BASE" UI_SSO=1 UI_SSO_SITE="$SSO_SITE" UI_SSO_BOT="$SSO_BOT_URL" \
    UI_SSO_SIBLING=https://other.fugal.test:8443 UI_SSO_STRANGER=https://evil.test:8443 \
    UI_MESSAGE="Hello! Please reply with one short sentence." \
    timeout 480 docker run --rm -i --network "$UI_NET" --memory 1g --cap-drop ALL --security-opt no-new-privileges \
    -e ACCESS_PASSWORD -e GATE_BOT_ID -e UI_BASE -e UI_SSO -e UI_SSO_SITE -e UI_SSO_BOT -e UI_SSO_SIBLING -e UI_SSO_STRANGER -e UI_MESSAGE \
    --entrypoint node "$BOT_IMG" --input-type=module - < scripts/ui-check.mjs 2>&1)
  echo "$uiout" | grep -v "^PASS\|^FAIL"
  while IFS= read -r line; do
    case "$line" in PASS*) ok "${line#PASS  }" ;; FAIL*) bad "${line#FAIL  }" ;; esac
  done <<< "$uiout"
  check "the browser checks ran to the end" bash -c '[[ "$0" == *"GATE_LOGIN_URL"* ]]' "$uiout"
  docker rm -f "$SSO_EDGE" >/dev/null 2>&1

  step "hosted mode: sign-out, then back to the password gate"
  code=$(curl -s -o /dev/null -D "$SSO_TMP/h" -w '%{http_code}' -b "$JAR" -c "$JAR" -X POST -H "Origin: $SSO_SITE" "$BASE/_gate/logout")
  check "POST /_gate/logout clears the cookie (got $code; $(hdr set-cookie))" \
    bash -c '[ "$0" = 200 ] && [[ "$1" == *"__Host-pb_session=; "*"Max-Age=0"* ]]' "$code" "$(hdr set-cookie)"
  code=$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" "$BASE/desktop/vnc.html")
  check "after sign-out the desktop page is refused (got $code)" test "$code" = 401
  rm -rf "$SSO_TMP"
  "${DC[@]}" up -d --no-deps gate proxy 2>&1 | tail -1
  check "gate and proxy healthy again with the password gate" wait_healthy
  code=$(login)
  check "password login works again (got $code)" test "$code" = 303
fi
# ==== end of hosted sign-in =========================================================

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

# ---- first-run files after the restart ------------------------------------------
if [ "$SEED_TEST" = 1 ]; then
  step "first-run files after the restart"
  seedlog2=$(seedlog); echo "entrypoint: $seedlog2"
  check "the second start wrote none of them again" \
    bash -c '[[ "$0" == "IDENTITY.md=current, SOUL.md=current, USER.md=current, AGENTS.md=current, BOOTSTRAP.md="* && "$0" != *written* && "$0" != *updated* ]]' "$seedlog2"
  check "and they are byte for byte what they were before the first conversation" test "$(seedfiles)" = "$SEED_SUMS0"
fi

# ---- backup and restore ----------------------------------------------------------
# A throwaway age key, a throwaway object store on loopback inside the bot's
# container (no cloud), and for the restore a throwaway container with an
# empty volume and no network (scripts/backup-check.sh).
step "backup: encrypted hot backup of the running bot, restore into an empty volume, cold backup (bot/backup.sh, bot/restore.sh)"
BK=$(mktemp -d) || exit 1
chmod 700 "$BK"
docker run --rm --network none --entrypoint age-keygen "$BOT_IMG" > "$BK/identity.txt" 2>/dev/null
BK_RECIPIENT=$(grep -a -o 'age1[0-9a-z]*' "$BK/identity.txt" | head -1)
printf 'backup-canary-%s-%s\n' "$RANDOM" "$RANDOM" > "$BK/canary.txt"
"${DC[@]}" exec -T bot sh -c 'cat > /home/node/workspace/backup-canary.txt' < "$BK/canary.txt"
"${DC[@]}" exec -T bot sh -c 'cat > /tmp/object-store.py' < scripts/object-store.py
"${DC[@]}" exec -d bot python3 -I /tmp/object-store.py 127.0.0.1 18999 /tmp/smoke-store
sleep 2
# bkrun <hot|cold> <object name>: the URL goes in on stdin, as the controller sends it.
bkrun() {
  printf 'http://127.0.0.1:18999/%s?X-Amz-Signature=smoke-signature\n' "$2" |
    "${DC[@]}" exec -T -e BACKUP_AGE_RECIPIENT="$BK_RECIPIENT" -e BACKUP_ALLOW_INSECURE_URL=1 bot /opt/fugal-bot/backup.sh "$1" 2>&1
}
out=$(bkrun hot hot.tar.age); echo "backup.sh hot -> $out"
check "hot backup while the gateway runs: one line of JSON, ok" \
  bash -c '[[ "$0" == "{\"ok\":true,\"kind\":\"hot\","*"}" ]] && [ "$(printf "%s\n" "$0" | wc -l)" = 1 ]' "$out"
check "it did not print the URL it was given" bash -c '[[ "$0" != *smoke-signature* && "$0" != *18999* ]]' "$out"
sum=$("${DC[@]}" exec -T bot sha256sum /tmp/smoke-store/hot.tar.age 2>/dev/null | cut -d' ' -f1)
check "the sha256 it reports is that of the object the store received" bash -c '[ -n "$1" ] && [[ "$0" == *"\"sha256\":\"$1\""* ]]' "$out" "$sum"
out=$(bkrun cold cold.tar.age)
check "a cold backup is refused while the bot runs" bash -c '[[ "$0" == *"\"ok\":false"*"the bot is running"* ]]' "$out"
"${DC[@]}" exec -T bot cat /tmp/smoke-store/hot.tar.age > "$BK/hot.tar.age"
"${DC[@]}" exec -T bot pkill -f object-store.py
cp scripts/object-store.py scripts/backup-check.sh "$BK/"
BK_VOL="${PROJECT}_restoretest"
docker volume rm -f "$BK_VOL" >/dev/null 2>&1
docker volume create "$BK_VOL" >/dev/null
# The same layout compose gives the bot: an empty "home" directory owned by uid 1000.
docker run --rm --network none -v "$BK_VOL:/v" --user 0:0 --entrypoint sh "$BOT_IMG" -c 'mkdir -p /v/home && chown 1000:1000 /v/home && chmod 700 /v/home'
bkout=$(tar -C "$BK" -cf - . | timeout 300 docker run --rm -i --network none --memory 1g --cap-drop ALL --security-opt no-new-privileges \
  --mount "type=volume,src=$BK_VOL,dst=/home/node,volume-subpath=home" -e BACKUP_AGE_RECIPIENT="$BK_RECIPIENT" \
  --entrypoint bash "$BOT_IMG" -c 'mkdir /tmp/in && tar -xf - -C /tmp/in && exec bash /tmp/in/backup-check.sh /tmp/in' 2>&1)
pf "$bkout"
check "the restore checks ran to the end" bash -c '[[ "$0" == *"--force replaced what was there"* ]]' "$bkout"
docker volume rm -f "$BK_VOL" >/dev/null 2>&1
[ -d "$BK" ] && rm -rf -- "$BK"
# --------------------------------------------------------------------------------

# ---- the first conversation (scripts/first-conversation.sh) -----------------------
# After every check of the starter files: it retires BOOTSTRAP.md.
if [ "$SEED_TEST" = 1 ] && "${DC[@]}" exec -T bot test -e /home/node/workspace/BOOTSTRAP.md; then
  # shellcheck source=scripts/first-conversation.sh
  . scripts/first-conversation.sh
  bot_sh() { "${DC[@]}" exec -T bot sh -c "$1"; }
  brain_log() { "${DC[@]}" logs brain 2>&1; }
  jnum() { num "$@"; }
  FC_NAME=$BOT_DISPLAY_NAME FC_EMOJI=$BOT_EMOJI FC_WALLET=$OWNER_WALLET_SS58 FC_TAOQ_MODE=$SMOKE_TAOQ first_conversation
fi
# --------------------------------------------------------------------------------

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
check "OpenAI-style error: HTTP 400, code allowance_exhausted" bash -c '[[ "$0" == "400 allowance_exhausted | "*"free allowance for this bot is used up"* ]]' "$direct"
reply=$(chat smoke-memory "Hello, are you there?")
echo "what the user is told: ${reply:0:400}"
check "the bot tells the user why, in the model proxy's words, instead of answering" \
  bash -c '[[ "$0" != *"scripted test model"* ]] && [[ "$0" == *"free allowance for this bot is used up"* && "$0" == *"add your own model key in Settings on the Fugal Bot page"* ]]' "$reply"
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
