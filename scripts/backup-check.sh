#!/bin/bash
# TEST ONLY: the second half of the backup round trip in scripts/smoke.sh.
# Runs inside a throwaway container made from the bot image, as the bot's
# user, with an EMPTY volume at /home/node and no network but loopback. The
# smoke test hands it one directory (argument 1) holding:
#   hot.tar.age     the object backup.sh uploaded from the running bot
#   identity.txt    the age identity it was encrypted to (a throwaway test key)
#   canary.txt      the contents of ~/workspace/backup-canary.txt at backup time
#   object-store.py the throwaway PUT/GET server
# It checks that the object is unreadable without the identity, restores it,
# then takes a cold backup of the restored (stopped) data directory and
# restores that one over the top with --force.
# Prints PASS/FAIL lines; exits 1 if any check failed.
set -u
IN=$1
cd "$IN" || exit 1
DATA=${HOME:-/home/node}
BIN=/opt/fugal-bot
export BACKUP_ALLOW_INSECURE_URL=1   # the store is plain http on loopback
STORE=http://127.0.0.1:18999
FAILED=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILED=1; }
check() { local name=$1; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
ok_json() { [[ "$1" == '{"ok":true,'*'}' ]] && [ "$(printf '%s\n' "$1" | wc -l)" = 1 ]; }
# Files in the data directory, not counting the shell start-up files Docker
# copies from the image into a new named volume.
files() { find "$DATA" ! -type d ! -name .bashrc ! -name .profile ! -name .bash_logout | wc -l; }

mkdir store && cp hot.tar.age store/hot.tar.age
python3 -I object-store.py 127.0.0.1 18999 "$IN/store" &
SERVER=$!
trap 'kill "$SERVER" 2>/dev/null' EXIT
for _ in $(seq 1 50); do curl -q -s -o /dev/null "$STORE/none" && break; sleep 0.2; done
CANARY=$(cat canary.txt)

# --- the object cannot be read without the identity ---------------------------
check "the uploaded object is an age file, not a plain archive" \
  test "$(head -c 21 hot.tar.age)" = "age-encryption.org/v1"
check "a file written before the backup cannot be found in the object as stored" \
  bash -c '! grep -a -q -F -- "$0" hot.tar.age' "$CANARY"
age-keygen -o other-identity.txt 2>/dev/null
check "another age identity cannot decrypt it" \
  bash -c '! age -d -i other-identity.txt hot.tar.age >/dev/null 2>&1'
out=$(printf '%s\n' "$STORE/hot.tar.age" | "$BIN/restore.sh" other-identity.txt 2>/dev/null)
echo "      restore with the wrong identity -> ${out:0:160}"
check "restore.sh with the wrong identity fails and leaves the empty volume empty" \
  bash -c '[[ "$0" == *"\"ok\":false"*"decrypt"* ]] && [ "$1" = 0 ]' "$out" "$(files)"

# --- restore into the empty volume ---------------------------------------------
out=$(printf '%s\n' "$STORE/hot.tar.age?X-Amz-Signature=smoke-signature" | "$BIN/restore.sh" identity.txt 2>&1)
echo "      restore -> ${out:0:300}"
check "restore.sh into the empty volume: one line of JSON, ok" ok_json "$out"
check "restore.sh did not print the URL" bash -c '[[ "$0" != *smoke-signature* && "$0" != *127.0.0.1:18999* ]]' "$out"
check "the file written before the backup is back, unchanged ($(cat "$DATA/workspace/backup-canary.txt" 2>&1))" \
  test "$(cat "$DATA/workspace/backup-canary.txt" 2>/dev/null)" = "$CANARY"
check "OpenClaw's config and workspace files are back" \
  test -s "$DATA/.openclaw/openclaw.json" -a -s "$DATA/workspace/AGENTS.md"
integrity=$(node -e '
const { DatabaseSync } = require("node:sqlite");
const fs = require("fs"), path = require("path");
const found = [];
(function walk(d) { for (const e of fs.readdirSync(d, { withFileTypes: true })) {
  const p = path.join(d, e.name);
  if (e.isDirectory()) { if (e.name !== "browser") walk(p); } else if (e.name.endsWith(".sqlite")) found.push(p);
} })(process.argv[1]);
let bad = 0;
for (const f of found) {
  const r = new DatabaseSync(f, { readOnly: true }).prepare("PRAGMA integrity_check").get();
  if (Object.values(r)[0] !== "ok") bad++;
}
console.log(found.length + " databases, " + bad + " damaged");' "$DATA/.openclaw" 2>/dev/null)
check "the restored OpenClaw databases pass SQLite's integrity check ($integrity)" \
  bash -c '[[ "$0" =~ ^[1-9][0-9]*\ databases,\ 0\ damaged$ ]]' "$integrity"
check "caches and the VNC password were not carried over" \
  bash -c '[ ! -e "$0/.cache" ] && [ ! -e "$0/.vnc" ] && [ ! -e "$0/.openclaw/logs" ] && [ ! -e "$0/FUGAL-BOT-BACKUP.json" ]' "$DATA"
check "every restored file belongs to the bot's user" \
  test -z "$(find "$DATA" ! -uid "$(id -u)" -print -quit)"

out=$(printf '%s\n' "$STORE/hot.tar.age" | "$BIN/restore.sh" identity.txt 2>/dev/null)
check "a second restore into the now filled directory is refused without --force" \
  bash -c '[[ "$0" == *"\"ok\":false"*"not empty"* ]]' "$out"
out=$(printf '%s\n' "$STORE/missing.tar.age" | "$BIN/restore.sh" --force identity.txt 2>/dev/null)
check "a missing object fails cleanly, and --force has deleted nothing by then" \
  bash -c '[[ "$0" == *"\"ok\":false"*"HTTP 404"* ]] && [ "$(cat "$1/workspace/backup-canary.txt" 2>/dev/null)" = "$2" ]' "$out" "$DATA" "$CANARY"

# --- cold backup of the restored, stopped data directory, and back again -------
out=$(printf '%s\n' "$STORE/cold.tar.age?X-Amz-Signature=smoke-signature" | "$BIN/backup.sh" cold 2>&1)
echo "      cold backup -> ${out:0:300}"
check "backup.sh cold on the stopped data directory: one line of JSON, ok" ok_json "$out"
sum=$(sha256sum store/cold.tar.age 2>/dev/null | cut -d' ' -f1)
check "its sha256 and size are those of the stored object" \
  bash -c '[ -n "$1" ] && [[ "$0" == *"\"sha256\":\"$1\""*  && "$0" == *"\"bytes\":$2,"* ]]' "$out" "$sum" "$(stat -c %s store/cold.tar.age 2>/dev/null)"
echo "stray" > "$DATA/stray-file.txt"
out=$(printf '%s\n' "$STORE/cold.tar.age" | "$BIN/restore.sh" --force identity.txt 2>&1)
echo "      restore --force from the cold backup -> ${out:0:300}"
check "restore.sh --force from the cold backup: ok, kind cold" bash -c '[[ "$0" == "{\"ok\":true,\"kind\":\"cold\","* ]]' "$out"
check "--force replaced what was there (a stray file is gone, the earlier file is back)" \
  bash -c '[ ! -e "$0/stray-file.txt" ] && [ "$(cat "$0/workspace/backup-canary.txt" 2>/dev/null)" = "$1" ]' "$DATA" "$CANARY"

exit "$FAILED"
