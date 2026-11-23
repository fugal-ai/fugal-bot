# shellcheck shell=bash
# The first conversation with the scripted model, end to end. Sourced by
# scripts/hosted-test.sh and scripts/smoke.sh after their own checks (it
# retires BOOTSTRAP.md, so it must come after every check of the starter files).
#
# The scripted model (scripts/scripted-brain) carries the conversation the way
# bot/seed/BOOTSTRAP.md tells a real model to: the greeting (copied out of the
# BOOTSTRAP.md OpenClaw put in front of it), one real `taoq stake` run in the
# bot's shell, a schedule made with OpenClaw's own `automations` tool, the
# notes, `rm` of BOOTSTRAP.md; then a forced run of that schedule.
#
# The caller provides: chat <session-user> <message>, bot_sh <shell command>
# (in the bot container, as the bot's user), brain_log, usage + a JSON getter
# `jnum <json> <dotted key>`, ok/bad/check/step, and:
#   FC_NAME FC_EMOJI FC_WALLET   what the bot was seeded with
#   FC_TAOQ_MODE                 live (finney, network) | fixture (the recorded read
#                                bot/tools/fixtures/finney.json.gz; no network)
# In fixture mode the brain's BRAIN_TAOQ_ENV must be TAOQ_FIXTURE=/tmp/taoq-fixture.json.gz.

fc_job() {   # the scheduled jobs, one JSON object per line
  bot_sh 'node -e '\''
const { DatabaseSync } = require("node:sqlite");
const db = new DatabaseSync("/home/node/.openclaw/state/openclaw.sqlite", { readOnly: true });
for (const r of db.prepare("select job_id, name, enabled, payload_kind, job_json, state_json from cron_jobs").all())
  console.log(JSON.stringify({ id: r.job_id, name: r.name, enabled: r.enabled, kind: r.payload_kind,
    job: JSON.parse(r.job_json || "{}"), state: JSON.parse(r.state_json || "{}") }));'\'''
}

# fc_rows <text>: rows of the agent's session database that contain <text>.
fc_rows() {
  bot_sh "node - '$1'" <<'JS'
const { DatabaseSync } = require("node:sqlite");
const db = new DatabaseSync("/home/node/.openclaw/agents/main/agent/openclaw-agent.sqlite", { readOnly: true });
const str = (v) => typeof v === "string" ? v : Buffer.isBuffer(v) || v instanceof Uint8Array ? Buffer.from(v).toString("utf8") : String(v);
let n = 0;
for (const t of db.prepare("select name from sqlite_master where type = 'table'").all())
  for (const r of db.prepare(`select * from "${t.name}"`).all())
    if (Object.values(r).some((v) => v != null && str(v).includes(process.argv[2]))) n++;
console.log(n);
JS
}
# fc_setup_done: OpenClaw's own record that the workspace finished its first run.
fc_setup_done() {
  bot_sh 'node -' <<'JS'
const { DatabaseSync } = require("node:sqlite");
const db = new DatabaseSync("/home/node/.openclaw/state/openclaw.sqlite", { readOnly: true });
const r = db.prepare("select setup_completed_at from workspace_setup_state where workspace_path = ?").get("/home/node/workspace");
console.log((r && r.setup_completed_at) || "");
JS
}

first_conversation() {
  step "first conversation (scripted model): greeting, a real chain read with taoq ($FC_TAOQ_MODE), a schedule, BOOTSTRAP.md retired"
  if [ "$FC_TAOQ_MODE" = fixture ]; then
    bot_sh 'cat > /tmp/taoq-fixture.json.gz' < bot/tools/fixtures/finney.json.gz
  fi
  local boot reply want greeting u0 u1 u2 n0 n1 jobs job jid line
  boot=$(bot_sh 'cat /home/node/workspace/BOOTSTRAP.md')
  check "BOOTSTRAP.md is waiting in the workspace" test -n "$boot"
  want=$(FC_NAME="$FC_NAME" FC_EMOJI="$FC_EMOJI" python3 -I -c 'import os
print(f"""Hi, I'"'"'m {os.environ["FC_NAME"]} {os.environ["FC_EMOJI"]}, your own always-on agent, with my own computer and browser.

I'"'"'ll never ask for a seed phrase or private key. I only read public chain data.

Want a **daily stake report** for the wallet you signed in with? Tell me your time zone and I'"'"'ll send it every morning.

I can also:
- **watch a subnet** and alert you to big moves or new releases
- **watch a miner or validator hotkey**
- **research** a subnet, or the week on Bittensor

Or just tell me what you need.""")')
  check "BOOTSTRAP.md carries the approved greeting word for word, with the bot's name and emoji" bash -c '[[ "$0" == *"$1"* ]]' "$boot" "$want"
  greeting=$(printf '%s' "$boot" | python3 -I -c 'import re,sys
m = re.search(r"(Hi, I.m .*?Or just tell me what you need\.)", sys.stdin.read(), re.S); print(m.group(1) if m else "")')
  check "the greeting does not show the owner's address" bash -c '[ -n "$0" ] && [[ "$0" != *"$1"* ]]' "$greeting" "$FC_WALLET"
  check "no tags or template marks left in BOOTSTRAP.md" bash -c '[[ "$0" != *"<message>"* && "$0" != *"{{"* ]]' "$boot"

  u0=$(usage)
  reply=$(chat first-run "Hi")
  echo "first reply: ${reply:0:200}..."
  check "first reply: exactly the greeting, nothing before or after it" test "$reply" = "$want"
  check "first reply: no tags, no mention of files or instructions" \
    bash -c '[[ "$0" != *"<"* && "$0" != *BOOTSTRAP* && "$0" != *nstruction* ]]' "$reply"

  n0=$(brain_log | grep -a -c '\[brain\] -> ')
  reply=$(chat first-run "Yes please, the daily stake report. My time zone is America/New_York.")
  n1=$(brain_log | grep -a -c '\[brain\] -> ')
  u1=$(usage)
  echo "reply:"; printf '%s\n' "$reply" | sed 's/^/    /' | head -12
  check "the stake report came from a real taoq run in the bot's shell" \
    bash -c '[[ "$0" == *"Stake report for"* && "$0" == *"TAO in all"* && "$0" == *"Scheduled: this report every day at 08:00 America/New_York"* ]]' "$reply"
  check "the shell ran taoq for the owner's address" bash -c 'grep -a -q -- "-> exec .*taoq stake $1 --text" <<< "$0"' "$(brain_log)" "$FC_WALLET"
  if [ "$FC_TAOQ_MODE" = live ]; then
    check "live read: a finalized finney block above 9,500,000 in the report" \
      bash -c '[[ "$0" =~ \(block\ ([0-9]+), ]] && [ "${BASH_REMATCH[1]}" -gt 9500000 ]' "$reply"
  fi
  check "taoq kept its snapshot in the workspace for tomorrow's comparison" \
    bot_sh "test -s /home/node/workspace/taoq/stake-$FC_WALLET.json"
  check "that turn took 4 model calls (taoq, schedule, notes, reply): $((n1 - n0))" test $((n1 - n0)) = 4

  jobs=$(fc_job)
  job=$(grep -a '"name":"Daily stake report"' <<< "$jobs" | head -1)
  echo "scheduled jobs: $(grep -a -c '"id"' <<< "$jobs"); the new one: ${job:0:700}"
  jid=$(python3 -I -c 'import json,sys
try: print(json.loads(sys.argv[1])["id"])
except Exception: print("")' "$job")
  check "OpenClaw's job table holds the job: cron 0 8 * * * America/New_York, isolated agent turn, light context, exec only, the taoq command" \
    python3 -I -c 'import json,sys
j = json.loads(sys.argv[1]); job = j["job"]; p = job.get("payload", {}); s = job.get("schedule", {})
ok = (j["enabled"] in (1, True) and s.get("kind") == "cron" and s.get("expr") == "0 8 * * *" and s.get("tz") == "America/New_York"
      and job.get("sessionTarget") == "isolated" and p.get("kind") == "agentTurn" and p.get("lightContext") is True
      and p.get("toolsAllow") == ["exec"] and ("taoq stake " + sys.argv[2] + " --text") in p.get("message", ""))
sys.exit(0 if ok else 1)' "${job:-{\}}" "$FC_WALLET"
  echo "its delivery: $(python3 -I -c 'import json,sys
try: print(json.dumps(json.loads(sys.argv[1])["job"].get("delivery")))
except Exception: print("?")' "$job")"
  check "next run is scheduled ($(python3 -I -c 'import json,sys,time
try: print(time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(json.loads(sys.argv[1])["state"]["nextRunAtMs"] / 1000)))
except Exception: print("?")' "$job"))" \
    python3 -I -c 'import json,sys; sys.exit(0 if json.loads(sys.argv[1])["state"].get("nextRunAtMs") else 1)' "${job:-{\}}"
  check "BOOTSTRAP.md is retired (deleted)" bot_sh 'test ! -e /home/node/workspace/BOOTSTRAP.md'
  check "USER.md has the time zone, MEMORY.md the job id" \
    bot_sh "grep -q 'America/New_York' /home/node/workspace/USER.md && grep -q -- '$jid' /home/node/workspace/MEMORY.md"
  echo "model proxy: $(( $(jnum "$u1" total.requests) - $(jnum "$u0" total.requests) )) model calls for the first two turns"

  step "the schedule runs (forced now through the automations tool; a scheduled run is the same)"
  n0=$(brain_log | grep -a -c 'cron-run step=')
  r0=$(fc_rows "Stake report for")
  reply=$(chat first-run "Run the daily stake report now: job $jid")
  echo "reply: ${reply:0:300}"
  check "the automations tool queued the run" bash -c '[[ "$0" == *"SCRIPTED-BRAIN RUN-NOW"* && "$0" == *enqueued* ]]' "$reply"
  # The run is queued and runs on its own; wait for its result in the job's state.
  for _ in $(seq 1 60); do
    job=$(fc_job | grep -a "\"id\":\"$jid\"" | head -1)
    [[ "$job" == *'"lastDelivered"'* || "$job" == *'"lastDeliveryStatus"'* ]] && break
    sleep 2
  done
  sleep 2
  n1=$(brain_log | grep -a -c 'cron-run step=')
  u2=$(usage)
  check "one scheduled report run = 2 model calls (one to run taoq, one to phrase the report): $((n1 - n0))" test $((n1 - n0)) = 2
  r1=$(fc_rows "Stake report for")
  check "the run's report was delivered into the conversation that made the job (session rows with a stake report: $r0 -> $r1)" \
    test "${r1:-0}" -gt "${r0:-0}"
  jobs=$(fc_job)
  job=$(grep -a "\"id\":\"$jid\"" <<< "$jobs" | head -1)
  echo "job state after the run: $(python3 -I -c 'import json,sys
try: print(json.dumps(json.loads(sys.argv[1])["state"])[:600])
except Exception: print("?")' "$job")"
  check "the run is recorded as ok and delivered" python3 -I -c 'import json,sys
st = json.loads(sys.argv[1])["state"]
ok = st.get("lastRunStatus", st.get("lastStatus")) == "ok"
sys.exit(0 if ok else 1)' "${job:-{\}}"
  echo "model proxy: $(( $(jnum "$u2" total.requests) - $(jnum "$u1" total.requests) )) model calls for the run-now turn and the run (2 + 2)"
  done_at=$(fc_setup_done)
  check "OpenClaw itself recorded the first run as complete once the file was gone (workspace_setup_state.setup_completed_at = ${done_at:-empty})" \
    bash -c '[[ "$0" =~ ^20[0-9][0-9]- ]]' "$done_at"
  watch_conversation
}

# A hotkey watch: a script job (no model call per check). The script is the recipe
# text in AGENTS.md, copied by the scripted model. A quiet check delivers nothing;
# a change is delivered, and neither costs a model call.
fc_watch_state() {   # the "at" of the watch's saved state, or empty
  bot_sh "node - '$1'" <<'JS'
const fs = require("node:fs");
try { console.log(JSON.parse(fs.readFileSync(process.argv[2], "utf8")).at); } catch { console.log(""); }
JS
}
fc_watch_plant() {   # pretend the last check knew a warning that is gone now
  bot_sh "node - '$1'" <<'JS'
const fs = require("node:fs");
const j = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
j.alerts["weights_stale:SN70"] = { severity: "warning", text: "SN70 uid 1: planted by the test", since: "2026-01-01T00:00:00Z" };
fs.writeFileSync(process.argv[2], JSON.stringify(j));
JS
}
fc_wait_state_change() {   # <path> <old at>: wait for the job's run to save a new state
  local _ at
  for _ in $(seq 1 45); do
    at=$(fc_watch_state "$1")
    [ -n "$at" ] && [ "$at" != "$2" ] && { echo "$at"; return 0; }
    sleep 2
  done
  echo "$at"; return 1
}

watch_conversation() {
  step "a hotkey watch (scripted model): a script job, a quiet check sends nothing and costs no model call, a change is delivered"
  local hk=5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u reply n0 n1 u0 u1 u2 u3 jobs job wid state at0 at1 r0 r1 st
  state="/home/node/workspace/taoq/watch-hotkey-$hk-sn70.json"
  n0=$(brain_log | grep -a -c '\[brain\] -> ')
  reply=$(chat first-run "Watch this hotkey for me: $hk on subnet 70. My time zone is America/New_York.")
  n1=$(brain_log | grep -a -c '\[brain\] -> ')
  echo "reply:"; printf '%s\n' "$reply" | sed 's/^/    /' | head -8
  check "the first check ran in the bot's shell and says what it now watches" \
    bash -c '[[ "$0" == *"registered on 1 subnet"* && "$0" == *"Watching from now"* && "$0" == *"Scheduled: the hotkey watch every 6 hours"* ]]' "$reply"
  check "that turn took 3 model calls (taoq, schedule, reply): $((n1 - n0))" test $((n1 - n0)) = 3
  jobs=$(fc_job)
  job=$(grep -a '"name":"Hotkey watch"' <<< "$jobs" | head -1)
  echo "the job: ${job:0:600}"
  wid=$(python3 -I -c 'import json,sys
try: print(json.loads(sys.argv[1])["id"])
except Exception: print("")' "$job")
  check "OpenClaw's job table holds a SCRIPT job: cron 0 */6 * * *, isolated, running taoq hotkey ... --watch --text, no agent turn" \
    python3 -I -c 'import json,sys
j = json.loads(sys.argv[1]); job = j["job"]; p = job.get("payload", {}); s = job.get("schedule", {})
ok = (j["enabled"] in (1, True) and s.get("expr") == "0 */6 * * *" and s.get("tz") == "America/New_York"
      and job.get("sessionTarget") == "isolated" and p.get("kind") == "script"
      and ("taoq hotkey " + sys.argv[2] + " --netuid 70 --watch --text") in p.get("script", "") and "message" not in p)
sys.exit(0 if ok else 1)' "${job:-{\}}" "$hk"
  check "taoq saved the watch's state in the workspace" bot_sh "test -s $state"

  # A quiet check: same chain, nothing new. OpenClaw 2026.11.6 fails the FIRST script run
  # after the gateway process starts with "Plugin runtime changed" (seen on every fresh
  # start, forced runs); the next run works. So the check allows that one failure, says so,
  # and runs again. Model calls: 2 for each asking turn (run, reply), none for the runs.
  at0=$(fc_watch_state "$state")
  u0=$(usage); r0=$(fc_rows "RESOLVED (was warning)")
  local asks=1
  reply=$(chat first-run "Run the hotkey watch now: job $wid")
  echo "reply: ${reply:0:240}"
  check "the automations tool queued the run" bash -c '[[ "$0" == *"SCRIPTED-BRAIN RUN-NOW"* && "$0" == *enqueued* ]]' "$reply"
  at1=$(fc_wait_state_change "$state" "$at0") || true
  if [ -z "$at1" ] || [ "$at1" = "$at0" ]; then
    job=$(fc_job | grep -a "\"id\":\"$wid\"" | head -1)
    if [[ "$job" == *"Plugin runtime changed"* ]]; then
      echo "NOTE: the first script run after the gateway started failed with OpenClaw's 'Plugin runtime changed' (known; the next run works): running it again"
      asks=2
      reply=$(chat first-run "Run the hotkey watch now: job $wid")
      at1=$(fc_wait_state_change "$state" "$at0") || true
    fi
  fi
  check "the script job ran and taoq saved a new check ($at0 -> $at1)" test -n "$at1" -a "$at1" != "$at0"
  sleep 3
  u1=$(usage)
  check "the quiet check made no model call (only the $((2 * asks)) calls of the asking turn(s): $(( $(jnum "$u1" total.requests) - $(jnum "$u0" total.requests) )))" \
    test $(( $(jnum "$u1" total.requests) - $(jnum "$u0" total.requests) )) = $((2 * asks))
  job=$(fc_job | grep -a "\"id\":\"$wid\"" | head -1)
  st=$(python3 -I -c 'import json,sys
try: print(json.dumps(json.loads(sys.argv[1])["state"])[:500])
except Exception: print("?")' "$job")
  echo "job state after the quiet check: $st"
  check "the last run is recorded as ok" python3 -I -c 'import json,sys
st = json.loads(sys.argv[1])["state"]
sys.exit(0 if st.get("lastRunStatus", st.get("lastStatus")) == "ok" else 1)' "${job:-{\}}"

  # A change: the last check knew a warning that has gone.
  fc_watch_plant "$state"
  at0=$(fc_watch_state "$state")
  u2=$(usage)
  reply=$(chat first-run "Run the hotkey watch now: job $wid")
  at1=$(fc_wait_state_change "$state" "$at0") || true
  check "the second run saved its check ($at0 -> $at1)" test -n "$at1" -a "$at1" != "$at0"
  sleep 3
  u3=$(usage)
  r1=$(fc_rows "RESOLVED (was warning)")
  check "the change was delivered into the conversation without a model call (session rows with the notice: $r0 -> $r1; calls $(( $(jnum "$u3" total.requests) - $(jnum "$u2" total.requests) )))" \
    test "${r1:-0}" -gt "${r0:-0}" -a $(( $(jnum "$u3" total.requests) - $(jnum "$u2" total.requests) )) = 2
}
