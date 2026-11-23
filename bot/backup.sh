#!/bin/bash
# One encrypted backup of the bot's data directory, sent to a presigned URL.
# Nothing here runs on a timer: whoever operates the bot starts it.
#
#   backup.sh hot    while the bot runs: OpenClaw's own consistent export
#                    (`openclaw backup create --verify`: its SQLite databases
#                    are snapshotted, not copied while being written) for its
#                    state directory and the workspace, plus the other files
#                    in the home directory.
#   backup.sh cold   with the bot stopped: a plain tar of the home directory.
#                    Refuses to run while the gateway answers.
#
# stdin:  ONE line, a presigned HTTPS PUT URL. It is never printed, logged or
#         put on a command line (curl reads it from a private config file).
# env:    BACKUP_AGE_RECIPIENT   one or more age PUBLIC keys ("age1..."),
#                                separated by commas or spaces. The matching
#                                private key is never in this container.
#         BACKUP_DATA_DIR        what to back up (default: $HOME, /home/node)
#         BACKUP_TMPDIR          scratch space (default: $TMPDIR or /tmp); a
#                                hot backup needs about twice the size of the
#                                data it covers, a cold one the size of the
#                                compressed archive
#         BACKUP_UPLOAD_TIMEOUT  seconds allowed for the upload (default 900)
# stdout: one line of JSON:
#         {"ok":true,"kind":"hot","format":1,"bytes":N,"sha256":"...","seconds":S,"openclaw":"..."}
#         (bytes and sha256 describe the encrypted object that was uploaded),
#         or {"ok":false,"error":"..."} with a non-zero exit status and the
#         same message on stderr.
#
# The object is a gzip-compressed tar encrypted with age. Inside, paths are
# relative to the home directory (./.openclaw/..., ./workspace/...), plus one
# extra member, ./FUGAL-BOT-BACKUP.json. restore.sh puts it back.
#
# Left out, because they are caches or are made again at start (the list is
# below): browser caches, ~/.cache, ~/.npm, OpenClaw's tmp/, logs/, generated
# skill index and private update captures, the VNC password, lock files.
# Never in a backup because they are not in the bot's home directory at all:
# the model proxy's usage counters, the proxy's TLS state, TUNNEL_URL.txt, and
# every secret that lives only in the environment.
set -euo pipefail
umask 077

KIND=${1:-}
DATA=${BACKUP_DATA_DIR:-${HOME:-/home/node}}
FORMAT=1
MANIFEST=FUGAL-BOT-BACKUP.json
T0=$(date +%s%N)
WORK=

json_str() { node -e 'process.stdout.write(JSON.stringify(process.argv[1]))' "$1"; }
die() {
  echo "backup: $*" >&2
  printf '{"ok":false,"error":%s}\n' "$(json_str "$*")"
  exit 1
}
cleanup() { [ -n "$WORK" ] && rm -rf "$WORK"; return 0; }
trap cleanup EXIT

case "$KIND" in hot|cold) ;; *) die "usage: backup.sh hot|cold  (with a presigned PUT URL on stdin)" ;; esac
[ -d "$DATA" ] || die "the data directory $DATA does not exist"
DATA=$(cd "$DATA" && pwd -P)
# "The directory exists" proves nothing: Docker creates a missing bind-mount
# source as an empty directory. An empty archive must never be reported as a
# backup, so the directory has to hold a bot's state.
[ -s "$DATA/.openclaw/openclaw.json" ] || die "$DATA does not hold a bot's data (no .openclaw/openclaw.json): nothing to back up"

RECIPIENTS=()
for r in $(printf '%s' "${BACKUP_AGE_RECIPIENT:-}" | tr ',' ' '); do
  [[ "$r" =~ ^age1[0-9a-z]{58}$ ]] || die "BACKUP_AGE_RECIPIENT must hold age public keys (age1...)"
  RECIPIENTS+=(-r "$r")
done
[ "${#RECIPIENTS[@]}" -gt 0 ] || die "BACKUP_AGE_RECIPIENT is not set (an age public key, age1...)"

IFS= read -r URL || true
URL=${URL%$'\r'}
# Plain http is for the smoke test's local server only.
if [ "${BACKUP_ALLOW_INSECURE_URL:-}" = "1" ]; then
  url_re='^https?://[^[:space:]"\\]+$'; PROTO='=https,http'
else
  url_re='^https://[^[:space:]"\\]+$'; PROTO='=https'
fi
[[ "$URL" =~ $url_re ]] && [ "${#URL}" -le 8192 ] || die "stdin must hold one presigned https:// PUT URL"

WORK=$(mktemp -d "${BACKUP_TMPDIR:-${TMPDIR:-/tmp}}/fugal-bot-backup.XXXXXXXX") || die "cannot create a scratch directory"

# Caches and things that are made again at start. These are tar member names,
# matched from the start of the name; "*" also matches "/".
cat > "$WORK/excludes" <<'EOF'
./.cache
./.npm
./.vnc
./.openclaw.update-captures
./.openclaw/tmp
./.openclaw/logs
./.openclaw/plugin-skills
./.openclaw/*.tmp-*
./.openclaw/browser/*/Cache
./.openclaw/browser/*/Code Cache
./.openclaw/browser/*/GPUCache
./.openclaw/browser/*/GPUPersistentCache
./.openclaw/browser/*/DawnWebGPUCache
./.openclaw/browser/*/DawnGraphiteCache
./.openclaw/browser/*/GrShaderCache
./.openclaw/browser/*/GraphiteDawnCache
./.openclaw/browser/*/ShaderCache
./.openclaw/browser/*/Service Worker/CacheStorage
./.openclaw/browser/*/Service Worker/ScriptCache
./.openclaw/browser/*/component_crx_cache
./.openclaw/browser/*/extensions_crx_cache
./.openclaw/browser/*/OptimizationGuideModelsManifest
./.openclaw/browser/*/optimization_guide_model_store
./.openclaw/browser/*/Safe Browsing
./.openclaw/browser/*/Crashpad
./.openclaw/browser/*/BrowserMetrics*
./.openclaw/browser/*/Singleton*
EOF

OPENCLAW_VERSION=$(node -e 'try{process.stdout.write(require("/app/package.json").version)}catch{}' 2>/dev/null || true)
node -e '
const [file, kind, format, version] = process.argv.slice(1);
require("fs").writeFileSync(file, JSON.stringify({
  format: Number(format), kind, created: new Date().toISOString(), openclaw: version || null,
}) + "\n");' "$WORK/$MANIFEST" "$KIND" "$FORMAT" "$OPENCLAW_VERSION"

MEMBERS=(-C "$WORK" "./$MANIFEST")
TAR_LIVE=()
if [ "$KIND" = cold ]; then
  # A copy of SQLite files that are being written is not a backup. The
  # gateway's port is the same in every layout (loopback in the bot's
  # container, or the one network namespace all services share).
  if curl -q -s -o /dev/null --max-time 3 http://127.0.0.1:18789/healthz 2>/dev/null; then
    die "the bot is running (its gateway answers on 127.0.0.1:18789): stop it first, or take a hot backup"
  fi
  MEMBERS+=(-C "$DATA" .)
else
  [ -f /app/openclaw.mjs ] || die "a hot backup needs OpenClaw (/app/openclaw.mjs); take a cold one instead"
  mkdir "$WORK/oc" "$WORK/stage"
  # Its stderr may carry a warning that the outcome could not be recorded with
  # the running gateway (this CLI has no login there). That is harmless.
  if ! (cd "$WORK/oc" && node /app/openclaw.mjs backup create --output "$WORK/oc" --verify --json) \
      > "$WORK/oc.json" 2> "$WORK/oc.err"; then
    die "openclaw backup create failed: $(tail -c 400 "$WORK/oc.err" | tr '\n' ' ')"
  fi
  # Line 1: the archive. Then one line per archived asset: <path relative to
  # the data directory> TAB <path in the archive> TAB <components to strip>.
  node -e '
const fs = require("fs"), path = require("path");
const [jsonFile, home] = process.argv.slice(1);
const raw = fs.readFileSync(jsonFile, "utf8");
const r = JSON.parse(raw.slice(raw.indexOf("{")));
if (r.verified !== true) { console.error("OpenClaw did not verify the archive"); process.exit(1); }
const out = [r.archivePath];
for (const a of r.assets || []) {
  const rel = path.relative(home, fs.realpathSync(a.sourcePath));
  // Each asset must be one top-level entry of the data directory (the state
  // directory, the workspace): the rest of that directory is copied around them.
  if (!rel || rel.startsWith("..") || path.isAbsolute(rel) || /[\/\t\n]/.test(rel) || /[\t\n]/.test(a.archivePath)) {
    console.error(`OpenClaw archived ${a.sourcePath}, which is not a top-level entry of ${home}`); process.exit(1);
  }
  out.push([rel, a.archivePath, a.archivePath.split("/").length].join("\t"));
}
if (out.length < 2) { console.error("the archive holds no assets"); process.exit(1); }
console.log(out.join("\n"));' "$WORK/oc.json" "$DATA" > "$WORK/assets" 2> "$WORK/assets.err" \
    || die "could not use OpenClaw's backup result: $(tr '\n' ' ' < "$WORK/assets.err")"
  ARCHIVE=$(head -1 "$WORK/assets")
  [ -f "$ARCHIVE" ] || die "OpenClaw reported an archive that is not there"
  STAGED=()
  while IFS=$'\t' read -r rel member strip; do
    mkdir -p "$WORK/stage/$rel"
    tar -xzf "$ARCHIVE" -C "$WORK/stage/$rel" --strip-components="$strip" "$member" 2> "$WORK/untar.err" \
      || die "could not unpack OpenClaw's archive ($rel): $(tail -c 300 "$WORK/untar.err" | tr '\n' ' ')"
    STAGED+=("./${rel%%/*}")
  done < <(tail -n +2 "$WORK/assets")
  rm -f "$ARCHIVE"
  mapfile -t STAGED < <(printf '%s\n' "${STAGED[@]}" | sort -u)
  # The rest of the home directory (plain files: the desktop, dotfiles, things
  # the agent installed): every top-level entry OpenClaw's export did not cover.
  REST=()
  while IFS= read -r -d '' entry; do
    name="./${entry#"$DATA"/}"
    covered=0
    for s in "${STAGED[@]}"; do [ "$name" = "$s" ] && covered=1; done
    [ "$covered" = 1 ] || REST+=("$name")
  done < <(find "$DATA" -mindepth 1 -maxdepth 1 -print0 | sort -z)
  MEMBERS+=(-C "$WORK/stage" "${STAGED[@]}")
  [ "${#REST[@]}" -eq 0 ] || MEMBERS+=(-C "$DATA" "${REST[@]}")
  # Those plain files belong to a running bot: one may change or vanish while
  # it is read. Not an error here; it is one for a stopped bot.
  TAR_LIVE=(--ignore-failed-read --warning=no-file-changed --warning=no-file-removed)
fi

set +e
tar -cf - --anchored --wildcards --wildcards-match-slash --exclude-from="$WORK/excludes" \
    "${TAR_LIVE[@]}" "${MEMBERS[@]}" 2> "$WORK/tar.err" \
  | gzip -6 \
  | age "${RECIPIENTS[@]}" -o "$WORK/backup.tar.age" 2> "$WORK/age.err"
STATUS=("${PIPESTATUS[@]}")
set -e
# tar exits 1 for "a file changed as it was read".
if [ "${STATUS[0]}" -gt 1 ] || { [ "${STATUS[0]}" = 1 ] && [ "$KIND" = cold ]; }; then
  die "tar failed: $(tail -c 300 "$WORK/tar.err" | tr '\n' ' ')"
fi
[ "${STATUS[1]}" = 0 ] || die "compressing the archive failed"
[ "${STATUS[2]}" = 0 ] || die "age failed: $(tail -c 300 "$WORK/age.err" | tr '\n' ' ')"
rm -rf "$WORK/stage"

BYTES=$(stat -c %s "$WORK/backup.tar.age")
SHA=$(sha256sum "$WORK/backup.tar.age" | cut -d' ' -f1)

# The URL reaches curl in a config file (mode 600, deleted with the scratch
# directory), never as an argument. A regular file as the body gives the
# Content-Length a presigned PUT needs. curl's own messages are not passed on
# (they can quote the URL): only the HTTP status and curl's exit code are.
printf 'url = "%s"\n' "$URL" > "$WORK/curl.cfg"
set +e
HTTP=$(curl -q -K "$WORK/curl.cfg" -X PUT -T "$WORK/backup.tar.age" -H 'Content-Type: application/octet-stream' \
  -sS -o /dev/null -w '%{http_code}' --connect-timeout 20 --max-time "${BACKUP_UPLOAD_TIMEOUT:-900}" \
  --retry 2 --retry-delay 3 --proto "$PROTO" 2> /dev/null)
RC=$?
set -e
URL=; rm -f "$WORK/curl.cfg"
[ "$RC" = 0 ] || die "upload failed (curl exit $RC, HTTP ${HTTP:-000}): the storage could not be reached, or the transfer timed out"
case "$HTTP" in 2??) ;; *) die "upload refused: HTTP $HTTP (an expired or mismatched presigned URL answers 403)" ;; esac

MS=$(( ($(date +%s%N) - T0) / 1000000 ))
printf '{"ok":true,"kind":"%s","format":%s,"bytes":%s,"sha256":"%s","seconds":%s.%03d,"openclaw":%s}\n' \
  "$KIND" "$FORMAT" "$BYTES" "$SHA" "$((MS / 1000))" "$((MS % 1000))" "$(json_str "$OPENCLAW_VERSION")"
