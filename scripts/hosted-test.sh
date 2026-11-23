#!/usr/bin/env bash
# Hosted-mode test: the bot as a site runs it for an owner, from the files of
# the room bundle only. Docker only, the test-only scripted model, no paid
# call, no real key or token, nothing from your .env.
#
# What it does, in a throwaway directory that stands in for the room:
#   app/    compose.yaml, compose.cathedral.yaml, compose.hosted.yaml and a
#           .env written the way a controller writes it (every value comes
#           from that file: compose runs with an otherwise empty environment
#           and with --no-build, so nothing else of this repository is used
#           by the three files);
#   data/   DATA_DIR (bind mounts, as in a room);
#   store/  a local stand-in for the presigned-URL object storage;
#   restore/identity.txt   where the age identity is put for the restore.
# The services share one network namespace instead of the real host network
# (scripts/hosted-test.compose.yaml), and GATE_SSO=on.
#
# Checks: sign-in by a one-time token posted as a form, the password refused,
# a chat round trip with the scripted model through the bot's own model
# proxy, the agent's shell and browser, the name, emoji and accent colour in
# the real web page inside a frame on the site's page, the starter files,
# /desktop/, who may frame the bot; then a hot backup, a cold backup through
# the compose "backup" service, the stack removed and DATA_DIR deleted, a
# restore through the compose "restore" service, a new start, and that a file
# and a conversation from before the backup are back. Last, the first
# conversation (scripts/first-conversation.sh): the greeting, a real `taoq`
# chain read in the bot's shell, a schedule made with OpenClaw's automations
# tool and run once, BOOTSTRAP.md retired; a hotkey watch; a chain question
# that names a dashboard site, answered with taoq instead of the browser.
#
# NOT run here: the "tunnel" service (a Cloudflare named tunnel needs a real
# token). Its definition is only checked with `docker compose config`.
#
# Usage: scripts/hosted-test.sh [--keep] [--no-build]
#   BOT_IMAGE, MODELPROXY_IMAGE, GATE_IMAGE, PROXY_IMAGE, BRAIN_IMAGE   image
#     names to build and run (defaults as in the compose files; the proxy is
#     built from proxy/Dockerfile.nocap, as published)
#   --no-build: nothing of the product is built; the four images are used as
#     named (pulled if missing), e.g. the digests of an `images` workflow run:
#       BOT_IMAGE=ghcr.io/fugal-ai/fugal-bot@sha256:... MODELPROXY_IMAGE=... \
#       GATE_IMAGE=... PROXY_IMAGE=... scripts/hosted-test.sh --no-build
#     Only the test-only scripted model is built, if it is not there yet.
#   HOSTED_TEST_PROJECT (fugal-bot-hostedtest), HOSTED_TEST_PORT (18097)
#   HOSTED_TEST_TAOQ=live (default: the first conversation reads finney, needs
#     the network) | fixture (a recorded read; no network for the chain tool)
set -uo pipefail
cd "$(dirname "$0")/.."
REPO=$PWD

PROJECT=${HOSTED_TEST_PROJECT:-fugal-bot-hostedtest}
PORT=${HOSTED_TEST_PORT:-18097}
BOT_IMG=${BOT_IMAGE:-fugal-bot-bot:stage0}
MODELPROXY_IMG=${MODELPROXY_IMAGE:-fugal-bot-modelproxy:stage0}
GATE_IMG=${GATE_IMAGE:-fugal-bot-gate:stage0}
PROXY_IMG=${PROXY_IMAGE:-fugal-bot-proxy:stage0}
BRAIN_IMG=${BRAIN_IMAGE:-fugal-bot-scripted-brain:test}
KEEP=0; BUILD=1
for a in "$@"; do case "$a" in --keep) KEEP=1 ;; --no-build) BUILD=0 ;; *) echo "unknown option $a"; exit 2 ;; esac; done

BASE="http://127.0.0.1:${PORT}"
SITE=https://fugal.test:8443                 # stand-in for the site (scripts/sso-edge.Caddyfile)
BOT_URL=https://b-smoke.fugal.test:8443      # the bot as a browser reaches it, same-site with it
STORE=http://127.0.0.1:18999                 # the object store, on the stack's loopback
SIGNATURE=hosted-test-signature              # stands for the secret part of a presigned URL
NAME="Smoke Otter"; EMOJI="🦦"; ACCENT="#2f6fed"
EXTRA_BODY='{"chat_template_kwargs":{"enable_thinking":false}}'
# The owner's address: SN70's owner coldkey (public, on chain as SubnetOwner(70)),
# so the first conversation reads a real stake position. The recorded read in
# bot/tools/fixtures/finney.json.gz is for this address.
WALLET=5GbydRVj98hQZvR2cf4t8WtcoUVjAuJykxxuqauiJpJqwB7m
TAOQ_MODE=${HOSTED_TEST_TAOQ:-live}
EDGE="${PROJECT}-sso-edge"

HT=$(mktemp -d "${TMPDIR:-/tmp}/fugal-bot-hosted-test.XXXXXX") || exit 1
chmod 700 "$HT"
APP=$HT/app; DATA=$HT/data; JAR=$HT/jar
mkdir "$APP" "$HT/store" "$HT/restore" "$HT/tmp"

PASS=0; FAIL=0
ok()   { echo "PASS  $*"; PASS=$((PASS + 1)); }
bad()  { echo "FAIL  $*"; FAIL=$((FAIL + 1)); }
step() { echo; echo "== $*"; }
check(){ local name=$1; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }
pf() {   # count the PASS/FAIL lines a check script printed, show the rest
  echo "$1" | grep -v "^PASS\|^FAIL"
  local line
  while IFS= read -r line; do
    case "$line" in PASS*) ok "${line#PASS  }" ;; FAIL*) bad "${line#FAIL  }" ;; esac
  done <<< "$1"
}

# Compose as the room runs it: in the app directory, which holds the compose
# files and .env, and with nothing else in the environment.
CLEAN=(PATH="$PATH" HOME="$HOME")
for v in DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG XDG_RUNTIME_DIR; do
  [ -n "${!v:-}" ] && CLEAN+=("$v=${!v}")
done
dc() { (cd "$APP" && exec env -i "${CLEAN[@]}" docker compose -p "$PROJECT" "$@"); }
# The three files of the bundle alone, as `up` would see them in a room.
dc_room() { (cd "$APP" && exec env -i "${CLEAN[@]}" COMPOSE_FILE=compose.yaml:compose.cathedral.yaml:compose.hosted.yaml \
  COMPOSE_PROFILES=hosted docker compose -p "$PROJECT" "$@"); }
# A throwaway root shell for files that belong to the containers' users.
as_root() { docker run --rm --network none --user 0:0 --cap-drop ALL --cap-add DAC_OVERRIDE --cap-add CHOWN --cap-add FOWNER \
  --security-opt no-new-privileges -v "$HT:/t" --entrypoint sh "$BRAIN_IMG" -c "$1"; }
# The bot's home directory on disk, read as the bot's user (the bot may be stopped).
in_home() { docker run --rm --network none --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges \
  -v "$DATA/home:/h:ro" --entrypoint sh "$BRAIN_IMG" -c "$1" 2>&1; }

cleanup() {
  if [ "$KEEP" = 1 ]; then
    echo "(kept: project $PROJECT, directory $HT; remove with: docker compose -p $PROJECT down -v, then delete the directory as root)"
    return
  fi
  docker rm -f "$EDGE" >/dev/null 2>&1
  [ -f "$APP/.env" ] && dc --profile backup --profile restore --profile hosted down -v --remove-orphans >/dev/null 2>&1
  as_root 'rm -rf /t/data /t/store /t/restore' >/dev/null 2>&1
  rm -rf -- "$HT"
}
trap cleanup EXIT

wait_mem() {
  local avail
  for _ in $(seq 1 60); do
    avail=$(free -m | awk '/^Mem:/ {print $7}')
    [ "$avail" -ge 3000 ] && { echo "available memory: ${avail} MB"; return 0; }
    echo "available memory ${avail} MB < 3000 MB, waiting..."; sleep 20
  done
  echo "not enough memory"; exit 3
}
HEALTHY_S=
wait_healthy() {
  local t0=$SECONDS b= p=
  while [ $((SECONDS - t0)) -lt 600 ]; do
    b=$(docker inspect -f '{{.State.Health.Status}}' "$(dc ps -q bot)" 2>/dev/null)
    p=$(docker inspect -f '{{.State.Health.Status}}' "$(dc ps -q proxy)" 2>/dev/null)
    [ "$b" = healthy ] && [ "$p" = healthy ] && { HEALTHY_S=$((SECONDS - t0)); echo "healthy after ${HEALTHY_S}s"; return 0; }
    sleep 3
  done
  echo "bot=$b proxy=$p"; dc logs --tail 40 bot; return 1
}

# token [ttl] [bot id] -> a token as the site's controller mints it. The
# signing secret is read from the environment, never from a command line.
token() { SSO_PW="$GATE_PW" python3 -I -c '
import base64, hashlib, hmac, os, secrets, sys, time
signed = "v1.%s.%d.%s" % (sys.argv[2], int(time.time()) + int(sys.argv[1]), secrets.token_hex(16))
key = hashlib.sha256(b"fugal-bot-gate-sso-v1\0" + os.environ["SSO_PW"].encode()).digest()
mac = hmac.new(key, signed.encode(), hashlib.sha256).digest()
print(signed + "." + base64.urlsafe_b64encode(mac).rstrip(b"=").decode())' "${1:-60}" "${2:-$BOT_ID}"; }
# sso_post <token> [Origin] -> HTTP code; reply headers in $HT/h, cookie in $JAR
sso_post() { printf '%s' "$1" | curl -s -o /dev/null -D "$HT/h" -c "$JAR" -w '%{http_code}' -H "Origin: ${2:-$SITE}" \
  --data-urlencode token@- --data-urlencode "next=/" "$BASE/_gate/sso"; }
hdr() { tr -d '\r' < "$HT/h" | grep -i "^$1:" | sed -E 's/^[^:]*: *//' || true; }
# frames_ok <Content-Security-Policy values> <X-Frame-Options value>: every policy
# names this host and the site and nothing else, and there is no X-Frame-Options.
frames_ok() { [ -n "$1" ] && [ -z "$2" ] || return 1
  [ "$(printf '%s\n' "$1" | grep -c -E "frame-ancestors 'self' ${SITE//./\\.}(;|\$)")" = "$(printf '%s\n' "$1" | grep -c .)" ]; }

chat() {   # chat <session-user> <message> -> the assistant's text
  local out i
  for i in $(seq 1 12); do
    out=$(curl -s -b "$JAR" --max-time 600 "$BASE/v1/chat/completions" -H 'Content-Type: application/json' \
      -d "$(python3 -I -c 'import json,sys;print(json.dumps({"model":"openclaw/default","user":sys.argv[1],"messages":[{"role":"user","content":sys.argv[2]}]}))' "$1" "$2")" |
      python3 -I -c 'import json,sys
r=sys.stdin.read()
try:
  d=json.loads(r); print(d["choices"][0]["message"]["content"] or "")
except Exception: print("RAW:", r[:600])')
    case "$out" in *"Please retry"*|*"is starting"*) echo "(gateway still starting, retrying)" >&2; sleep 5 ;; *) break ;; esac
  done
  printf '%s\n' "$out"
}
usage() {
  local out i
  for i in $(seq 1 20); do
    out=$(dc exec -T modelproxy python3 -I -c 'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:9001/usage",timeout=5).read().decode())' 2>/dev/null) && [ -n "$out" ] && break
    sleep 1
  done
  printf '%s\n' "$out"
}
jget() { python3 -I -c 'import json,sys
try: d=json.loads(sys.argv[1])
except Exception: d={}
for k in sys.argv[2].split("."): d=d.get(k, "") if isinstance(d, dict) else ""
print(d)' "$1" "$2"; }
one_json_line() { [ "$(printf '%s\n' "$1" | wc -l)" = 1 ] && [[ "$1" == "{\"ok\":"*"}" ]]; }
wait_store() {   # the stand-in storage answers (404 for a name it does not hold)
  for _ in $(seq 1 40); do
    dc exec -T store python3 -I -c 'import urllib.request, urllib.error
try: urllib.request.urlopen("http://127.0.0.1:18999/none", timeout=2)
except urllib.error.HTTPError: pass' 2>/dev/null && return 0
    sleep 0.5
  done
  echo "the object store did not start"; return 1
}
seedfiles() { dc exec -T bot sh -c 'cd /home/node/workspace 2>/dev/null && sha256sum IDENTITY.md SOUL.md USER.md AGENTS.md BOOTSTRAP.md 2>&1'; }
seedlog() { dc logs bot 2>&1 | grep -a 'workspace seed:' | tail -1 | sed 's/.*workspace seed: //'; }

# COMPOSE_PROFILES=hosted is left out of this .env, and only that: it would
# start the tunnel, which cannot connect with a made-up token. The token must
# still be set, because compose.hosted.yaml refuses to be read without one.
# ---- the room: app directory, .env, age key ------------------------------------
step "the room: three compose files, a .env as a controller writes it"
cp compose.yaml compose.cathedral.yaml compose.hosted.yaml "$APP/"
GATE_PW=$(head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 43)     # throwaway
BOT_ID=b$(head -c 256 /dev/urandom | base64 | tr -dc 'a-z2-7' | head -c 10)
CANARY_KEY="sk-smoke-$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"    # stand-in model key
# The age key pair is made with the bot image's own age-keygen, so only after
# the images exist (built below, or pulled for --no-build); until then .env
# holds a placeholder for the public key.
RECIPIENT=age1-placeholder-replaced-once-the-images-exist
( umask 077; cat > "$APP/.env" <<EOF
# ---- layout ----
COMPOSE_FILE=compose.yaml:compose.cathedral.yaml:compose.hosted.yaml:$REPO/scripts/smoke.compose.yaml:$REPO/scripts/hosted-test.compose.yaml
DATA_DIR=$DATA
BIND_ADDRESS=127.0.0.1
PORT=$PORT
PUBLIC_HOSTNAME=
BOT_IMAGE=$BOT_IMG
MODELPROXY_IMAGE=$MODELPROXY_IMG
GATE_IMAGE=$GATE_IMG
PROXY_IMAGE=$PROXY_IMG
BOT_MEM_LIMIT=4g
TZ=UTC
# ---- access ----
ACCESS_PASSWORD=$GATE_PW
GATE_SSO=on
GATE_BOT_ID=$BOT_ID
GATE_LOGIN_URL=$SITE/bot
GATE_PARENT_ORIGIN=
SESSION_HOURS=24
# ---- model ----
MODEL_BASE_URL=http://127.0.0.1:11500/v1
MODEL_API_KEY=$CANARY_KEY
MODEL_NAME=tools
MODEL_CONTEXT_WINDOW=131072
MODEL_MAX_TOKENS=4096
MODEL_REASONING=false
MODEL_SUPPORTS_TOOLS=true
MODEL_INPUT=text
MODEL_EXTRA_BODY=$EXTRA_BODY
# ---- identity ----
BOT_DISPLAY_NAME=$NAME
BOT_EMOJI=$EMOJI
UI_ACCENT_COLOR=$ACCENT
SEED_WORKSPACE=1
OWNER_WALLET_SS58=$WALLET
BACKGROUND_MODEL_USE=off
TOOLS_DENY=
EXEC_MODE=full
# ---- plumbing ----
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_IDS=
TUNNEL_TOKEN=not-a-real-token-the-tunnel-is-not-started
BACKUP_AGE_RECIPIENT=$RECIPIENT
RESTORE_IDENTITY_FILE=$HT/restore/identity.txt
# ---- this test only ----
SMOKE_CANARY_KEY=$CANARY_KEY
BRAIN_IMAGE=$BRAIN_IMG
BRAIN_TAOQ_ENV=$([ "$TAOQ_MODE" = fixture ] && echo "TAOQ_FIXTURE=/tmp/taoq-fixture.json.gz TAOQ_ENDPOINTS=ws://127.0.0.1:9")
HOSTED_TEST_UID=$(id -u):$(id -g)
HOSTED_TEST_STORE_SCRIPT=$REPO/scripts/object-store.py
HOSTED_TEST_STORE_DIR=$HT/store
EOF
)

if [ "$BUILD" = 1 ]; then
  wait_mem
  (cd "$REPO" && env -i "${CLEAN[@]}" docker compose -p "$PROJECT" --env-file "$APP/.env" -f compose.yaml -f compose.cathedral.yaml \
    -f compose.hosted.yaml -f scripts/smoke.compose.yaml -f scripts/hosted-test.compose.yaml build -q) || { echo "build failed"; exit 1; }
else
  # --no-build: the four images as published (e.g. BOT_IMAGE=ghcr.io/...@sha256:...
  # from a CI run), pulled when missing. Only the test-only scripted model is
  # built here, if it is not there yet (a small Python image).
  for img in "$BOT_IMG" "$MODELPROXY_IMG" "$GATE_IMG" "$PROXY_IMG"; do
    docker image inspect "$img" >/dev/null 2>&1 || docker pull -q "$img" >/dev/null || { echo "cannot pull $img"; exit 1; }
  done
  docker image inspect "$BRAIN_IMG" >/dev/null 2>&1 ||
    docker build -q -t "$BRAIN_IMG" scripts/scripted-brain >/dev/null || { echo "cannot build the scripted model"; exit 1; }
fi
# The age key pair. The identity goes where the restore reads it, readable by
# uid 1000 only; the bot only ever gets the public key.
RECIPIENT=$(docker run --rm --network none --user 0:0 --cap-drop ALL --cap-add DAC_OVERRIDE --cap-add CHOWN --cap-add FOWNER \
  -v "$HT/restore:/k" --entrypoint sh "$BOT_IMG" -c \
  'umask 077 && age-keygen -o /k/identity.txt 2>/dev/null && chown 1000:1000 /k/identity.txt && age-keygen -y /k/identity.txt')
check "an age key pair for this bot was made (public key ${RECIPIENT:0:12}...)" bash -c '[[ "$0" =~ ^age1[0-9a-z]{58}$ ]]' "$RECIPIENT"
[[ "$RECIPIENT" =~ ^age1[0-9a-z]{58}$ ]] || { echo "no age key, stopping"; exit 1; }
sed -i "s/^BACKUP_AGE_RECIPIENT=.*/BACKUP_AGE_RECIPIENT=$RECIPIENT/" "$APP/.env"

# ---- what the three files say, before anything runs ----------------------------
step "the three compose files with the profile \"hosted\" (docker compose config)"
cfg=$(dc_room config --format json 2>"$HT/tmp/config.err") || { cat "$HT/tmp/config.err"; bad "docker compose config failed"; cfg='{}'; }
allcfg=$(dc_room --profile backup --profile restore config --format json 2>/dev/null) || allcfg='{}'
cfgcheck=$(DATA="$DATA" IDENTITY="$HT/restore/identity.txt" NAME="$NAME" ACCENT="$ACCENT" EXTRA_BODY="$EXTRA_BODY" EMOJI="$EMOJI" \
  python3 -I -c '
import json, os, sys
cfg, allcfg = json.loads(sys.argv[1]), json.loads(sys.argv[2])
def line(ok, text): print(("PASS  " if ok else "FAIL  ") + text)
names = sorted(cfg.get("services", {}))
line(names == ["bot", "gate", "modelproxy", "proxy", "tunnel", "volume-init"],
     "`up` with the profile starts exactly: " + ", ".join(names))
extra = sorted(set(allcfg.get("services", {})) - set(names))
line(extra == ["backup", "restore"], "declared but never started by `up`: " + ", ".join(extra))
t = cfg.get("services", {}).get("tunnel", {})
cmd = t.get("command") or []
line(cmd == ["tunnel", "--no-autoupdate", "--metrics", "127.0.0.1:2000", "run", "--protocol", "http2"]
     and t.get("network_mode") == "host" and "TUNNEL_TOKEN" in (t.get("environment") or {})
     and "@sha256:" in t.get("image", ""),
     "tunnel: a named tunnel (run --protocol http2), token from the environment only, metrics on loopback, image pinned by digest")
line(not any("not-a-real-token" in str(c) for c in cmd), "tunnel: the token is not on its command line")
env = cfg.get("services", {}).get("bot", {}).get("environment") or {}
want = {"BOT_DISPLAY_NAME": os.environ["NAME"], "BOT_EMOJI": os.environ["EMOJI"], "UI_ACCENT_COLOR": os.environ["ACCENT"],
        "MODEL_EXTRA_BODY": os.environ["EXTRA_BODY"], "SEED_WORKSPACE": "1", "BACKGROUND_MODEL_USE": "off"}
wrong = [k for k, v in want.items() if env.get(k) != v]
line(not wrong, "values with a space, a #, an emoji and JSON reach the bot from .env unchanged" + (": NOT " + ", ".join(wrong) if wrong else ""))
# Every bind mount comes from DATA_DIR or is the restore identity; no build is needed.
binds, others = [], []
for name, s in allcfg.get("services", {}).items():
    for v in s.get("volumes") or []:
        (binds if v.get("type") == "bind" else others).append((name, v.get("source", "")))
data, ident = os.environ["DATA"], os.environ["IDENTITY"]
stray = [b for b in binds if not (b[1] == data or b[1].startswith(data + "/") or b[1] == ident)]
line(binds and not stray and not others,
     "every mount is a directory under DATA_DIR or the restore identity file (%d mounts)" % len(binds)
     + (": NOT " + str(stray + others) if stray or others else ""))
images = {n: s.get("image", "") for n, s in allcfg.get("services", {}).items()}
line(all(images.values()), "every service names an image, so `up --no-build` needs no source tree")
b, r = allcfg.get("services", {}).get("backup", {}), allcfg.get("services", {}).get("restore", {})
bm = [v for v in b.get("volumes") or [] if v.get("target") == "/home/node"]
line(b.get("entrypoint") == ["/opt/fugal-bot/backup.sh"] and b.get("command") == ["cold"] and b.get("user") == "1000:1000"
     and len(bm) == 1 and bm[0].get("read_only") is True and bm[0].get("source") == data + "/home"
     and not b.get("read_only") and not b.get("tmpfs")
     and sorted(b.get("environment") or {}) == ["BACKUP_AGE_RECIPIENT", "BACKUP_UPLOAD_TIMEOUT", "HOME", "TZ"],
     "backup: backup.sh cold as uid 1000, the home directory read-only, scratch space on disk (no tmpfs), no secret but the public key")
line(r.get("entrypoint") == ["/opt/fugal-bot/restore.sh"] and r.get("command") == ["/run/restore/identity.txt"] and r.get("user") == "1000:1000"
     and "ACCESS_PASSWORD" not in (r.get("environment") or {}) and "volume-init" in (r.get("depends_on") or {}),
     "restore: restore.sh as uid 1000 with the identity file mounted, after volume-init")
' "$cfg" "$allcfg" 2>&1)
pf "$cfgcheck"
echo "NOT RUN: the tunnel service (a Cloudflare named tunnel needs a real token); it is left out of this start."
missing=$(for v in $(grep -ho '\${[A-Z_0-9]*' compose.yaml compose.cathedral.yaml compose.hosted.yaml | tr -d '${' | sort -u); do
  grep -q -E "^# ?$v=|^$v=" .env.example || printf '%s ' "$v"; done)
check "every variable the three files read is described in .env.example${missing:+ (missing: $missing)}" test -z "$missing"
dupes=$(grep -o -E '^[A-Z_0-9]+=' .env.example | sort | uniq -d | tr '\n' ' ')
check ".env.example sets no variable twice${dupes:+ (twice: $dupes)}" test -z "$dupes"

# ---- start ---------------------------------------------------------------------
step "start from the app directory: docker compose up -d --no-build (GATE_SSO=on, scripted model)"
wait_mem
dc up -d --no-build 2>&1 | tail -3
check "bot and proxy healthy" wait_healthy
START1=$HEALTHY_S
wait_store
SEED_LOG=$(seedlog); SEED_SUMS0=$(seedfiles)
echo "entrypoint: $SEED_LOG"
check "new data directory: all five starter files were seeded" \
  test "$SEED_LOG" = "IDENTITY.md=written, SOUL.md=written, USER.md=written, AGENTS.md=written, BOOTSTRAP.md=written"
doctor_log() { dc logs bot 2>&1 | grep -a -o '\[fugal-bot\] openclaw doctor[^"]*' | tail -1; }
doctor_mark() { dc exec -T bot sh -c 'cat /home/node/.openclaw/fugal-doctor.ok 2>/dev/null | md5sum; md5sum < /etc/fugal-bot/image-id' | cut -c1-12 | tr '\n' ' '; }
IMAGE_ID=$(dc exec -T bot cat /etc/fugal-bot/image-id 2>/dev/null | tr '\n' ' ')
echo "image id: $IMAGE_ID"; echo "gateway start: $(doctor_log)"
check "new data directory: openclaw doctor skipped (nothing to repair), the directory marked as made by this image" \
  bash -c '[[ "$0" == *"already run by this image"*"skipped"* ]] && m=($1) && [ -n "${m[0]}" ] && [ "${m[0]}" = "${m[1]}" ]' "$(doctor_log)" "$(doctor_mark)"
layout=$(as_root 'cd /t/data && stat -c "%n %u:%g %a" home proxy modelproxy 2>&1' | tr '\n' ' ')
check "DATA_DIR layout: $layout" test "$layout" = "home 1000:1000 700 proxy 1001:1001 700 modelproxy 1002:1002 700 "
pages=$(dc exec -T bot node -e '
const c = JSON.parse(require("fs").readFileSync("/home/node/.openclaw/openclaw.json", "utf8"));
console.log(JSON.stringify(((c.tools || {}).web || {}).fetch) + " " + JSON.stringify((c.browser || {}).snapshotDefaults));' 2>&1)
check "page sizes: web_fetch 6,000 characters by default and 12,000 at most, compact browser snapshots ($pages)" \
  test "$pages" = '{"maxChars":6000,"maxCharsCap":12000} {"mode":"efficient"}'

# ---- sign-in -------------------------------------------------------------------
step "sign-in: a one-time token posted as a form; no password"
code=$(curl -s -o "$HT/b" -D "$HT/h" -w '%{http_code}' -H 'Accept: text/html' "$BASE/")
check "no session: a \"Signing you in\" page, no password field, no redirect (got $code)" \
  bash -c '[ "$0" = 401 ] && [ -z "$1" ] && grep -q "Signing you in" "$2" && ! grep -q "name=\"password\"" "$2"' "$code" "$(hdr location)" "$HT/b"
code=$(printf '%s' "$GATE_PW" | curl -s -o /dev/null -w '%{http_code}' --data-urlencode password@- --data-urlencode "next=/" "$BASE/_gate/login")
check "the signing secret is refused as a password (got $code)" test "$code" = 403
code=$(sso_post "$(token 60 baaaaaaaaaa)")
check "a token for another bot is refused (got $code)" test "$code" = 403
tok=$(token)
code=$(sso_post "$tok" https://evil.test:8443)
check "a good token posted from another origin is refused (got $code)" test "$code" = 403
code=$(sso_post "$tok")
setc=$(hdr set-cookie)
check "the token posted from the site's origin signs in (got $code, Location: $(hdr location))" test "$code" = 303 -a "$(hdr location)" = /
check "cookie: __Host-pb_session, Secure, HttpOnly, SameSite=Strict, Path=/, no Domain" \
  bash -c '[[ "$0" == *"__Host-pb_session="* && "$0" == *"; Secure"* && "$0" == *"; HttpOnly"* && "$0" == *"; SameSite=Strict"* && "$0" == *"; Path=/;"* && "${0,,}" != *domain* ]]' "$setc"
cp "$JAR" "$HT/jar.keep"
code=$(sso_post "$tok")
check "the same token a second time is refused (got $code)" test "$code" = 403
cp "$HT/jar.keep" "$JAR"

# ---- chat, tools ---------------------------------------------------------------
step "chat round trip: page -> gate -> agent -> the bot's model proxy -> scripted model"
u0=$(usage)
reply=$(chat hosted-memory "Please remember this code word for later: OSPREY-77. Reply with one short friendly sentence.")
echo "reply: ${reply:0:300}"
check "the scripted model answered (so the model proxy put the real key on the request)" bash -c '[[ "$0" == *"code word OSPREY-77"* ]]' "$reply"
u1=$(usage)
check "the model proxy counted it (requests $(jget "$u0" total.requests) -> $(jget "$u1" total.requests), tokens $(jget "$u0" total.total_tokens) -> $(jget "$u1" total.total_tokens))" \
  test "$(jget "$u1" total.requests)" -gt "$(jget "$u0" total.requests)" -a "$(jget "$u1" total.total_tokens)" -gt "$(jget "$u0" total.total_tokens)"
reply=$(chat hosted-tools "Use your computer: run a shell command that writes a file in your workspace, then open https://example.com in your browser.")
echo "reply: ${reply:0:400}"
check "the agent used its shell and its browser" bash -c '[[ "$0" == *SCRIPTED-BRAIN\ DONE* ]]' "$reply"
blog=$(dc logs brain 2>&1)
with=$(grep -a -c "extra_fields=$EXTRA_BODY" <<< "$blog"); without=$(grep -a -c 'extra_fields=none' <<< "$blog")
check "every model request carried the MODEL_EXTRA_BODY field from .env ($with with it, $without without)" test "$with" -gt 0 -a "$without" = 0
printenv GATE_PW CANARY_KEY > "$HT/tmp/secrets"
dc exec -T bot sh -c 'env; cat /proc/[0-9]*/environ /proc/[0-9]*/cmdline 2>/dev/null' > "$HT/tmp/botenv" 2>/dev/null
check "the agent's container holds neither the signing secret nor the model key (environment, process list)" \
  bash -c '[ -s "$1" ] && ! grep -a -q -F -f "$0" "$1"' "$HT/tmp/secrets" "$HT/tmp/botenv"
rm -f "$HT/tmp/secrets" "$HT/tmp/botenv"

# ---- framing, desktop ----------------------------------------------------------
step "framing and desktop, by request"
code=$(curl -s -o "$HT/b" -D "$HT/h" -w '%{http_code}' -b "$JAR" -H 'Accept: text/html' "$BASE/")
check "with the session: the web chat is served (got $code) $(grep -o '<title>[^<]*</title>' "$HT/b" | head -1)" test "$code" = 200
echo "framing headers: $(hdr content-security-policy | grep -o "frame-ancestors[^;]*" | tr '\n' '|') X-Frame-Options: '$(hdr x-frame-options)'"
check "web chat: only this host and $SITE may frame it (GATE_PARENT_ORIGIN empty: taken from GATE_LOGIN_URL)" \
  frames_ok "$(hdr content-security-policy)" "$(hdr x-frame-options)"
code=$(curl -s -o /dev/null -D "$HT/h" -w '%{http_code}' -b "$JAR" "$BASE/desktop/vnc.html")
check "/desktop/ is served (got $code)" test "$code" = 200
check "desktop page: only this host and $SITE may frame it" frames_ok "$(hdr content-security-policy)" "$(hdr x-frame-options)"
ws_code() { curl -s -o /dev/null -w '%{http_code}' -b "$JAR" --max-time 4 --http1.1 "$@" \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: c21va2UtdGVzdC1rZXkxMg==' -H 'Sec-WebSocket-Protocol: binary' "$BASE/desktop/websockify"; }
ws=$(ws_code -H "Origin: $BASE")
check "desktop websocket from the bot's own origin upgrades (got $ws)" test "$ws" = 101
ws=$(ws_code -H "Origin: https://other.fugal.test:8443")
check "desktop websocket from a sibling host, with the cookie, is refused (got $ws)" test "$ws" = 403

step "real browser: the bot in a frame on the site's page, over HTTPS; name, emoji, accent colour; /desktop/ (one model request)"
UI_NET="${PROJECT}_loopnet"
docker rm -f "$EDGE" >/dev/null 2>&1
EDGE_CADDYFILE=$(cat scripts/sso-edge.Caddyfile) SSO_UPSTREAM="netns:$PORT" \
  docker run -d --name "$EDGE" --network "$UI_NET" --network-alias fugal.test --network-alias b-smoke.fugal.test \
  --network-alias other.fugal.test --network-alias evil.test --memory 128m --cap-drop ALL --security-opt no-new-privileges \
  --read-only --tmpfs /tmp -e XDG_DATA_HOME=/tmp/data -e XDG_CONFIG_HOME=/tmp/config -e EDGE_CADDYFILE -e SSO_UPSTREAM \
  --entrypoint /bin/sh "$PROXY_IMG" -c 'printf "%s\n" "$EDGE_CADDYFILE" > /tmp/Caddyfile && exec caddy run --config /tmp/Caddyfile --adapter caddyfile' >/dev/null
uiout=$(ACCESS_PASSWORD="$GATE_PW" GATE_BOT_ID="$BOT_ID" UI_BASE="http://netns:$PORT" UI_SSO=1 UI_SSO_SITE="$SITE" UI_SSO_BOT="$BOT_URL" \
  UI_SSO_SIBLING=https://other.fugal.test:8443 UI_SSO_STRANGER=https://evil.test:8443 \
  UI_EXPECT_NAME="$NAME" UI_EXPECT_EMOJI="$EMOJI" UI_EXPECT_ACCENT="$ACCENT" \
  UI_MESSAGE="Hello! Please reply with one short sentence." \
  timeout 540 docker run --rm -i --network "$UI_NET" --memory 1g --cap-drop ALL --security-opt no-new-privileges \
  -e ACCESS_PASSWORD -e GATE_BOT_ID -e UI_BASE -e UI_SSO -e UI_SSO_SITE -e UI_SSO_BOT -e UI_SSO_SIBLING -e UI_SSO_STRANGER \
  -e UI_EXPECT_NAME -e UI_EXPECT_EMOJI -e UI_EXPECT_ACCENT -e UI_MESSAGE \
  --entrypoint node "$BOT_IMG" --input-type=module - < scripts/ui-check.mjs 2>&1)
pf "$uiout"
check "the browser checks ran to the end, and the name was looked for" \
  bash -c '[[ "$0" == *"GATE_LOGIN_URL"* && "$0" == *"shows the bot'"'"'s name"* ]]' "$uiout"
docker rm -f "$EDGE" >/dev/null 2>&1
check "after all those conversations the five starter files are exactly as seeded" test -n "$SEED_SUMS0" -a "$(seedfiles)" = "$SEED_SUMS0"

# ---- backups -------------------------------------------------------------------
step "hot backup of the running bot: docker compose exec -T bot /opt/fugal-bot/backup.sh hot < url"
MARK="hosted-marker-$RANDOM-$RANDOM-$RANDOM"
printf '%s\n' "$MARK" | dc exec -T bot sh -c 'cat > /home/node/workspace/hosted-marker.txt'
file1=$(dc exec -T bot cat /home/node/workspace/smoke-file.txt 2>&1)
# -e BACKUP_ALLOW_INSECURE_URL=1: the stand-in store is plain http on loopback.
out=$(printf '%s\n' "$STORE/hot.tar.age?X-Amz-Signature=$SIGNATURE" |
  dc exec -T -e BACKUP_ALLOW_INSECURE_URL=1 bot /opt/fugal-bot/backup.sh hot 2>"$HT/tmp/err"); rc=$?
echo "exit $rc, stdout: $out"
check "exit 0 and exactly one line of JSON on standard output, ok, kind hot" \
  bash -c '[ "$1" = 0 ] && [[ "$0" == "{\"ok\":true,\"kind\":\"hot\","* ]]' "$out" "$rc"
check "one line only" one_json_line "$out"
check "its sha256 and size are those of the object the store received ($(stat -c %s "$HT/store/hot.tar.age" 2>/dev/null) bytes)" \
  test -n "$(jget "$out" sha256)" -a "$(jget "$out" sha256)" = "$(sha256sum "$HT/store/hot.tar.age" 2>/dev/null | cut -d' ' -f1)" \
  -a "$(jget "$out" bytes)" = "$(stat -c %s "$HT/store/hot.tar.age" 2>/dev/null)"
check "neither output names the URL" bash -c '! grep -q -F -e "$1" "$0" && [[ "$2" != *"$1"* ]]' "$HT/tmp/err" "$SIGNATURE" "$out"
HOT_JSON=$out

step "cold backup through the compose service: docker compose run --rm -T backup < url"
out=$(printf '%s\n' "$STORE/refused.tar.age?X-Amz-Signature=$SIGNATURE" |
  dc run --rm -T -e BACKUP_ALLOW_INSECURE_URL=1 backup 2>"$HT/tmp/err"); rc=$?
echo "while the bot runs: exit $rc, stdout: $out"
check "refused while the bot runs: exit 1, {\"ok\":false,...}, nothing stored" \
  bash -c '[ "$1" = 1 ] && [[ "$0" == "{\"ok\":false,\"error\":"*"the bot is running"* ]] && [ ! -e "$2" ]' "$out" "$rc" "$HT/store/refused.tar.age"
dc stop bot 2>&1 | tail -1
out=$(printf '%s\n' "$STORE/cold.tar.age?X-Amz-Signature=$SIGNATURE" |
  dc run --rm -T -e BACKUP_ALLOW_INSECURE_URL=1 backup 2>"$HT/tmp/err"); rc=$?
echo "bot stopped: exit $rc, stdout: $out"
[ "$rc" = 0 ] || tail -5 "$HT/tmp/err"
check "exit 0 and exactly one line of JSON on standard output, ok, kind cold" \
  bash -c '[ "$1" = 0 ] && [[ "$0" == "{\"ok\":true,\"kind\":\"cold\","* ]]' "$out" "$rc"
check "one line only" one_json_line "$out"
check "its sha256 and size are those of the object the store received ($(stat -c %s "$HT/store/cold.tar.age" 2>/dev/null) bytes)" \
  test -n "$(jget "$out" sha256)" -a "$(jget "$out" sha256)" = "$(sha256sum "$HT/store/cold.tar.age" 2>/dev/null | cut -d' ' -f1)" \
  -a "$(jget "$out" bytes)" = "$(stat -c %s "$HT/store/cold.tar.age" 2>/dev/null)"
check "neither output names the URL" bash -c '! grep -q -F -e "$1" "$0" && [[ "$2" != *"$1"* ]]' "$HT/tmp/err" "$SIGNATURE" "$out"
COLD_JSON=$out
check "both objects are age files, and the marker cannot be found in them as stored" \
  bash -c '[ "$(head -c 21 "$0/hot.tar.age")" = "age-encryption.org/v1" ] && [ "$(head -c 21 "$0/cold.tar.age")" = "age-encryption.org/v1" ] && ! grep -a -q -F -- "$1" "$0/hot.tar.age" "$0/cold.tar.age"' "$HT/store" "$MARK"
check "the backup wrote nothing into the data directory (it is mounted read-only)" \
  test -z "$(in_home 'find /h -maxdepth 1 -name "fugal-bot-backup*"')"
left=$(docker ps -a -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=backup" | wc -l)
check "no backup container is left behind ($left)" test "$left" = 0

# ---- the room is gone, the data too --------------------------------------------
step "the stack removed and DATA_DIR deleted; restore through the compose service: docker compose run --rm -T restore < url"
dc down 2>&1 | tail -1
as_root 'rm -rf /t/data'
check "DATA_DIR is gone" test ! -e "$DATA"
dc up -d --no-build store 2>&1 | tail -1   # the stand-in storage (and the shared loopback)
wait_store
out=$(printf '%s\n' "$STORE/empty.tar.age?X-Amz-Signature=$SIGNATURE" |
  dc run --rm -T -e BACKUP_ALLOW_INSECURE_URL=1 backup 2>/dev/null); rc=$?
echo "a cold backup now: exit $rc, stdout: ${out:0:160}"
check "a backup of the missing data directory fails and stores nothing (never an empty backup)" \
  bash -c '[ "$1" != 0 ] && [[ "$0" != *"\"ok\":true"* ]] && [ ! -e "$2" ]' "$out" "$rc" "$HT/store/empty.tar.age"
out=$(printf '%s\n' "$STORE/hot.tar.age?X-Amz-Signature=$SIGNATURE" |
  dc run --rm -T -e BACKUP_ALLOW_INSECURE_URL=1 restore 2>"$HT/tmp/err"); rc=$?
echo "from the hot backup: exit $rc, stdout: $out"
[ "$rc" = 0 ] || tail -5 "$HT/tmp/err"
check "exit 0 and exactly one line of JSON on standard output, ok, kind hot" \
  bash -c '[ "$1" = 0 ] && [[ "$0" == "{\"ok\":true,\"kind\":\"hot\","* ]]' "$out" "$rc"
check "one line only" one_json_line "$out"
check "neither output names the URL" bash -c '! grep -q -F -e "$1" "$0" && [[ "$2" != *"$1"* ]]' "$HT/tmp/err" "$SIGNATURE" "$out"
check "the marker written before the hot backup is on disk again" test "$(in_home 'cat /h/workspace/hosted-marker.txt')" = "$MARK"
check "every restored file belongs to the bot's user" test -z "$(in_home 'find /h ! -user 1000 | head -3')"
out=$(printf '%s\n' "$STORE/cold.tar.age?X-Amz-Signature=$SIGNATURE" |
  dc run --rm -T -e BACKUP_ALLOW_INSECURE_URL=1 restore 2>/dev/null); rc=$?
check "a second restore into the now filled directory is refused: exit $rc, ${out:0:90}..." \
  bash -c '[ "$1" = 1 ] && [[ "$0" == "{\"ok\":false,\"error\":"*"not empty"* ]]' "$out" "$rc"
out=$(printf '%s\n' "$STORE/cold.tar.age?X-Amz-Signature=$SIGNATURE" |
  dc run --rm -T -e BACKUP_ALLOW_INSECURE_URL=1 restore --force /run/restore/identity.txt 2>"$HT/tmp/err"); rc=$?
echo "from the cold backup, --force: exit $rc, stdout: $out"
[ "$rc" = 0 ] || tail -5 "$HT/tmp/err"
check "restore --force from the cold backup: exit 0, ok, kind cold" \
  bash -c '[ "$1" = 0 ] && [[ "$0" == "{\"ok\":true,\"kind\":\"cold\","* ]]' "$out" "$rc"
RESTORE_JSON=$out
as_root 'rm -f /t/restore/identity.txt'
out=$(printf '%s\n' "$STORE/cold.tar.age" | dc run --rm -T -e BACKUP_ALLOW_INSECURE_URL=1 restore --force /run/restore/identity.txt 2>/dev/null); rc=$?
echo "with the identity file taken away again: exit $rc, stdout: ${out:0:160}"
check "without the identity file a restore fails, even with --force, and the data is untouched" \
  bash -c '[ "$1" != 0 ] && [[ "$0" != *"\"ok\":true"* ]] && [ "$2" = "$3" ]' "$out" "$rc" "$(in_home 'cat /h/workspace/hosted-marker.txt')" "$MARK"
as_root 'rm -rf /t/restore/identity.txt'   # some Compose versions leave an empty directory there

step "start again on the restored data"
wait_mem
dc up -d --no-build 2>&1 | tail -3
check "bot and proxy healthy" wait_healthy
START2=$HEALTHY_S
rm -f "$JAR"
code=$(sso_post "$(token)")
check "a fresh token signs in (got $code)" test "$code" = 303
check "the marker written before the backup is back: $(dc exec -T bot cat /home/node/workspace/hosted-marker.txt 2>&1)" \
  test "$(dc exec -T bot cat /home/node/workspace/hosted-marker.txt 2>/dev/null)" = "$MARK"
file2=$(dc exec -T bot cat /home/node/workspace/smoke-file.txt 2>&1)
check "the file the agent's shell made is back: $file2" test -n "$file1" -a "$file1" = "$file2"
seedlog2=$(seedlog); echo "entrypoint: $seedlog2"
check "no starter file was written again (the first conversation does not come back)" \
  bash -c '[[ "$0" == "IDENTITY.md=current, SOUL.md=current, USER.md=current, AGENTS.md=current, BOOTSTRAP.md="* && "$0" != *written* && "$0" != *updated* ]]' "$seedlog2"
check "and they are byte for byte what they were before the first conversation" test "$(seedfiles)" = "$SEED_SUMS0"
who=$(dc exec -T bot node -e '
const c = JSON.parse(require("fs").readFileSync("/home/node/.openclaw/openclaw.json", "utf8"));
const i = (c.agents && c.agents.entries && c.agents.entries.main && c.agents.entries.main.identity) || {};
console.log(i.name + " " + i.emoji + " " + ((c.ui || {}).seamColor || ""));' 2>&1)
check "name, emoji and accent colour are still set: $who" test "$who" = "$NAME $EMOJI $ACCENT"
reply=$(chat hosted-memory "What was the code word I asked you to remember? Answer with just the code word.")
echo "reply: ${reply:0:200}"
check "the bot recalls the code word from before the backup" bash -c '[[ "$0" == *OSPREY-77* ]]' "$reply"
u2=$(usage)
echo "model proxy counters after the restore: requests $(jget "$u2" total.requests) (they are not part of a backup and start again)"

# ---- the first conversation ----------------------------------------------------
# shellcheck source=scripts/first-conversation.sh
. "$REPO/scripts/first-conversation.sh"
bot_sh() { dc exec -T bot sh -c "$1"; }
brain_log() { dc logs brain 2>&1; }
jnum() { jget "$@"; }
FC_NAME=$NAME FC_EMOJI=$EMOJI FC_WALLET=$WALLET FC_TAOQ_MODE=$TAOQ_MODE first_conversation

# ---- an image upgrade: doctor runs once ------------------------------------------
step "an image upgrade: openclaw doctor --fix runs at the first start of the new image, and only then"
as_root 'printf "openclaw 2026.11.5\nrevision an-older-image\nfiles 0000000000000000\n" > /t/data/home/.openclaw/fugal-doctor.ok && chown 1000:1000 /t/data/home/.openclaw/fugal-doctor.ok'
dc up -d --no-build --force-recreate bot 2>&1 | tail -1
check "bot and proxy healthy after the doctor run" wait_healthy
START3=$HEALTHY_S
echo "gateway start: $(doctor_log)"
check "doctor ran (the data directory was last checked by an older image) and the marker now names this image" \
  bash -c '[[ "$0" == *"doctor --fix: first start of this image"* ]] && m=($1) && [ "${m[0]}" = "${m[1]}" ]' "$(dc logs bot 2>&1 | grep -a 'openclaw doctor' | head -1)" "$(doctor_mark)"
reply=$(chat hosted-memory "What was the code word I asked you to remember? Answer with just the code word.")
check "a chat round trip after the doctor run: ${reply:0:40}" bash -c '[[ "$0" == *OSPREY-77* ]]' "$reply"
dc up -d --no-build --force-recreate bot 2>&1 | tail -1
check "bot and proxy healthy (same image again)" wait_healthy
START4=$HEALTHY_S
echo "gateway start: $(doctor_log)"
check "the next start of the same image skips doctor" bash -c '[[ "$0" == *"already run by this image"*"skipped"* ]]' "$(doctor_log)"

echo
echo "== numbers"
echo "start to healthy: ${START1}s (new data directory), ${START2}s (restored), ${START3}s (image upgrade: doctor), ${START4}s (same image)"
echo "hot backup:  $(jget "$HOT_JSON" bytes) bytes in $(jget "$HOT_JSON" seconds) s"
echo "cold backup: $(jget "$COLD_JSON" bytes) bytes in $(jget "$COLD_JSON" seconds) s"
echo "restore:     $(jget "$RESTORE_JSON" files) files in $(jget "$RESTORE_JSON" seconds) s"
echo "NOT RUN: the tunnel service (needs a real Cloudflare token); only its definition was checked."
echo; echo "== result: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
