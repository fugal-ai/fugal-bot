#!/bin/bash
# Puts a backup made by backup.sh back into the bot's data directory. Run it
# with the bot STOPPED, as the bot's own user, in a one-shot container made
# from the bot image with the data directory mounted at /home/node.
#
#   restore.sh [--force] <age identity file>
#
# stdin:  ONE line, a presigned HTTPS GET URL of the backup object. It is
#         never printed, logged or put on a command line.
# arg:    the path of a file holding the age identity (the PRIVATE key,
#         "AGE-SECRET-KEY-1..."). Whoever restores brings it for this one run
#         and takes it away again (in a hosted room: a file under /work, which
#         is wiped with the room). It is read by age only and never copied.
# env:    BACKUP_DATA_DIR   where to restore to (default: $HOME, /home/node)
#         BACKUP_TMPDIR     scratch space (default: $TMPDIR or /tmp): the
#                           encrypted object plus the decrypted archive
#         BACKUP_OWNER      uid:gid for the restored files when run as root
#                           (default 1000:1000, the bot's user)
# stdout: one line of JSON:
#         {"ok":true,"kind":"cold","created":"...","bytes":N,"sha256":"...","files":N,"seconds":S}
#         or {"ok":false,"error":"..."} with a non-zero exit status.
#
# It refuses to run unless the data directory is empty (holds no files; see
# not_empty below). With --force it
# deletes what is there first, but only after the backup has been downloaded,
# decrypted and checked, so a wrong URL or key never costs the existing data.
# The archive is unpacked as it is: after the next start OpenClaw's own
# `doctor --fix` (run by the image at every start) adopts the state.
#
# A backup is encrypted to a public key, which proves nothing about who made
# it. It is the bot's own data directory, so it is exactly as trustworthy as
# that directory was; nothing from it is executed here, and it is only ever
# unpacked inside the data directory.
set -euo pipefail
umask 077

DATA=${BACKUP_DATA_DIR:-${HOME:-/home/node}}
MANIFEST=FUGAL-BOT-BACKUP.json
T0=$(date +%s%N)
WORK=

json_str() { node -e 'process.stdout.write(JSON.stringify(process.argv[1]))' "$1"; }
die() {
  echo "restore: $*" >&2
  printf '{"ok":false,"error":%s}\n' "$(json_str "$*")"
  exit 1
}
cleanup() { [ -n "$WORK" ] && rm -rf "$WORK"; return 0; }
trap cleanup EXIT

FORCE=0
IDENTITY=
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    -*) die "unknown option $a" ;;
    *) IDENTITY=$a ;;
  esac
done
[ -n "$IDENTITY" ] || die "usage: restore.sh [--force] <age identity file>  (with a presigned GET URL on stdin)"
[ -f "$IDENTITY" ] && [ -r "$IDENTITY" ] || die "the age identity file $IDENTITY cannot be read"
[ -d "$DATA" ] && [ -w "$DATA" ] || die "the data directory $DATA does not exist or is not writable"
DATA=$(cd "$DATA" && pwd -P)

if curl -q -s -o /dev/null --max-time 3 http://127.0.0.1:18789/healthz 2>/dev/null; then
  die "the bot is running (its gateway answers on 127.0.0.1:18789): stop it before restoring"
fi
# Empty means: no files. A new disk's lost+found, empty directories, and the
# three shell start-up files Docker copies from the image into a new named
# volume (.bashrc, .profile, .bash_logout) do not count.
not_empty() {
  [ -n "$(find "$DATA" -mindepth 1 -path "$DATA/lost+found" -prune -o ! -type d \
    ! -path "$DATA/.bashrc" ! -path "$DATA/.profile" ! -path "$DATA/.bash_logout" -print -quit)" ]
}
if not_empty && [ "$FORCE" = 0 ]; then
  die "the data directory $DATA is not empty; restoring would mix two states. Use an empty one, or --force to replace what is there"
fi

IFS= read -r URL || true
URL=${URL%$'\r'}
if [ "${BACKUP_ALLOW_INSECURE_URL:-}" = "1" ]; then
  url_re='^https?://[^[:space:]"\\]+$'; PROTO='=https,http'
else
  url_re='^https://[^[:space:]"\\]+$'; PROTO='=https'
fi
[[ "$URL" =~ $url_re ]] && [ "${#URL}" -le 8192 ] || die "stdin must hold one presigned https:// GET URL"

WORK=$(mktemp -d "${BACKUP_TMPDIR:-${TMPDIR:-/tmp}}/fugal-bot-restore.XXXXXXXX") || die "cannot create a scratch directory"

printf 'url = "%s"\n' "$URL" > "$WORK/curl.cfg"
set +e
HTTP=$(curl -q -K "$WORK/curl.cfg" -sS -o "$WORK/backup.tar.age" -w '%{http_code}' --connect-timeout 20 \
  --max-time "${BACKUP_DOWNLOAD_TIMEOUT:-900}" --retry 2 --retry-delay 3 --proto "$PROTO" 2> /dev/null)
RC=$?
set -e
URL=; rm -f "$WORK/curl.cfg"
[ "$RC" = 0 ] || die "download failed (curl exit $RC, HTTP ${HTTP:-000}): the storage could not be reached, or the transfer timed out"
case "$HTTP" in 2??) ;; *) die "download refused: HTTP $HTTP (an expired or mismatched presigned URL answers 403, a missing object 404)" ;; esac
BYTES=$(stat -c %s "$WORK/backup.tar.age")
SHA=$(sha256sum "$WORK/backup.tar.age" | cut -d' ' -f1)

age -d -i "$IDENTITY" -o "$WORK/backup.tar.gz" "$WORK/backup.tar.age" 2> "$WORK/age.err" \
  || die "could not decrypt the backup with this identity: $(tail -c 200 "$WORK/age.err" | tr '\n' ' ')"
rm -f "$WORK/backup.tar.age"

# Check all of it before anything in the data directory is touched.
tar -tzf "$WORK/backup.tar.gz" > "$WORK/members" 2> "$WORK/tar.err" \
  || die "the decrypted object is not a readable tar.gz archive: $(tail -c 200 "$WORK/tar.err" | tr '\n' ' ')"
if grep -q -v -E '^\./' "$WORK/members" || grep -q -E '(^|/)\.\.(/|$)' "$WORK/members"; then
  die "the archive holds a member outside the data directory; refusing it"
fi
tar -xzf "$WORK/backup.tar.gz" -O "./$MANIFEST" > "$WORK/$MANIFEST" 2> /dev/null \
  || die "this is not a Fugal Bot backup (no $MANIFEST inside)"
INFO=$(node -e '
const m = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
if (m.format !== 1) { console.error("format " + m.format); process.exit(1); }
if (m.kind !== "hot" && m.kind !== "cold") process.exit(1);
const created = /^[0-9TZ:.-]{10,40}$/.test(m.created || "") ? m.created : "";
process.stdout.write(m.kind + " " + (created || "unknown"));' "$WORK/$MANIFEST" 2> /dev/null) \
  || die "this backup's format is not one this version can restore"
KIND=${INFO%% *}; CREATED=${INFO#* }
FILES=$(grep -c -v -E '/$' "$WORK/members" || true)

if [ "$FORCE" = 1 ]; then
  find "$DATA" -mindepth 1 -maxdepth 1 ! -name lost+found -exec rm -rf -- {} + \
    || die "could not clear the data directory $DATA"
fi
# -p keeps the modes (state files are 0600). Owners in the archive are not
# applied: everything belongs to whoever runs this, then to BACKUP_OWNER.
tar -xzpf "$WORK/backup.tar.gz" -C "$DATA" --no-same-owner --exclude="./$MANIFEST" 2> "$WORK/tar.err" \
  || die "unpacking failed part-way; the data directory $DATA is incomplete (run again with --force): $(tail -c 300 "$WORK/tar.err" | tr '\n' ' ')"
if [ "$(id -u)" = 0 ]; then
  chown -R -h "${BACKUP_OWNER:-1000:1000}" "$DATA" || die "could not set the owner of the restored files"
fi
chmod 700 "$DATA" 2> /dev/null || true

MS=$(( ($(date +%s%N) - T0) / 1000000 ))
printf '{"ok":true,"kind":"%s","created":%s,"bytes":%s,"sha256":"%s","files":%s,"seconds":%s.%03d}\n' \
  "$KIND" "$(json_str "$CREATED")" "$BYTES" "$SHA" "${FILES:-0}" "$((MS / 1000))" "$((MS % 1000))"
