# shellcheck shell=bash
# The first conversation with the scripted model, end to end. Sourced by
# scripts/hosted-test.sh and scripts/smoke.sh after their own checks (it
# retires BOOTSTRAP.md, so it must come after every check of the starter files).
#
# The scripted model (scripts/scripted-brain) carries the conversation the way
# bot/seed/BOOTSTRAP.md tells a real model to: the greeting (copied out of the
# BOOTSTRAP.md OpenClaw put in front of it), the briefing offer accepted, one
# real `taoq briefing` run in the bot's shell (the chain live or recorded; the
# news from the recorded feed answers in bot/tools/fixtures, as the brain's
# BRAIN_TAOQ_ENV names TAOQ_FEED_FIXTURE=/tmp/taoq-feed.json), a schedule made
# with OpenClaw's own `automations` tool after listing the jobs there are: a
# script job that delivers taoq's text with no model call, run once at once (the
# delivered text is checked against a replay of taoq), the notes, `rm` of
# BOOTSTRAP.md; then a forced run of that schedule, the jobs asked about in new
# conversations (listed, never made twice) and a watch whose time zone comes
# from USER.md.
#
# The caller provides: chat <session-user> <message>, bot_sh <shell command>
# (in the bot container, as the bot's user), brain_log, usage + a JSON getter
# `jnum <json> <dotted key>`, ok/bad/check/step, and:
#   FC_NAME FC_EMOJI FC_WALLET   what the bot was seeded with
#   FC_TAOQ_MODE                 live (finney, network) | fixture (the recorded read
#                                bot/tools/fixtures/finney.json.gz; no network)
# In fixture mode the brain's BRAIN_TAOQ_ENV must also name TAOQ_FIXTURE=/tmp/taoq-fixture.json.gz.

# Model calls as the scripted model saw them: conversation calls ("[brain] -> ...")
# and Activity recaps ("[brain] recap -> ...", one small call after each finished
# run, answered apart; see scripts/scripted-brain/brain.py). The model proxy's own
# counters hold both.
fc_calls() { brain_log | grep -a -c '\[brain\] -> '; }
fc_recaps() { brain_log | grep -a -c '\[brain\] recap -> '; }

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
# fc_texts <text>: every message text in the agent's session database that contains
# <text>, JSON-decoded however deep it is stored, as one JSON array.
fc_texts() {
  bot_sh "node - '$1'" <<'JS'
const { DatabaseSync } = require("node:sqlite");
const db = new DatabaseSync("/home/node/.openclaw/agents/main/agent/openclaw-agent.sqlite", { readOnly: true });
const str = (v) => typeof v === "string" ? v : Buffer.isBuffer(v) || v instanceof Uint8Array ? Buffer.from(v).toString("utf8") : String(v);
const want = process.argv[2], out = new Set();
const walk = (v, depth) => {
  if (depth > 6 || v == null) return;
  if (typeof v === "string") {
    if (!v.includes(want)) return;
    out.add(v);
    if (/^\s*[\[{"]/.test(v)) { try { walk(JSON.parse(v), depth + 1); } catch {} }
  } else if (Array.isArray(v)) v.forEach((x) => walk(x, depth + 1));
  else if (typeof v === "object") Object.values(v).forEach((x) => walk(x, depth + 1));
};
for (const t of db.prepare("select name from sqlite_master where type = 'table'").all())
  for (const r of db.prepare(`select * from "${t.name}"`).all())
    for (const v of Object.values(r)) if (v != null) walk(str(v), 0);
console.log(JSON.stringify([...out]));
JS
}
# fc_wait_rows <text> <count before>: wait until more session rows hold <text>.
fc_wait_rows() {
  local _ r
  for _ in $(seq 1 60); do
    r=$(fc_rows "$1"); [ "${r:-0}" -gt "$2" ] && { echo "$r"; return 0; }
    sleep 2
  done
  echo "${r:-0}"; return 1
}
# fc_lists <brain log lines>: how many of them are automations list calls.
fc_lists() { grep -a -c -E -- '-> (automations|cron|dispatch_action) .*"action": "list"' <<< "$1"; }

# fc_setup_done: OpenClaw's own record that the workspace finished its first run.
fc_setup_done() {
  bot_sh 'node -' <<'JS'
const { DatabaseSync } = require("node:sqlite");
const db = new DatabaseSync("/home/node/.openclaw/state/openclaw.sqlite", { readOnly: true });
const r = db.prepare("select setup_completed_at from workspace_setup_state where workspace_path = ?").get("/home/node/workspace");
console.log((r && r.setup_completed_at) || "");
JS
}

# The first message need not be "Hi": OpenClaw's own starter button sends
# "What can you do?", and a user may start with a real task. Each of these, in
# a session of its own while BOOTSTRAP.md is still there, must be answered
# plainly, with no word about the instruction files, plus a one-line offer, and
# must leave BOOTSTRAP.md in place.
first_messages() {
  step "first message is not a greeting (scripted model): \"What can you do?\" and a real task"
  local reply n0 n1 ask sess calls rc0
  for pair in "fc-wcyd|What can you do?|1" "fc-task|What is the price of subnet 70 right now?|2"; do
    IFS='|' read -r sess ask calls <<< "$pair"
    n0=$(brain_log | grep -a -c '\[brain\] -> '); rc0=$(fc_recaps)
    reply=$(chat "$sess" "$ask")
    n1=$(brain_log | grep -a -c '\[brain\] -> ')
    echo "\"$ask\" ->"; printf '%s\n' "$reply" | sed 's/^/    /' | head -8
    check "\"$ask\" as the first message: answered, the daily briefing offered in one line, no word about instruction files or reasoning" \
      bash -c '[[ "$0" == *"daily briefing"* && "$0" != *SCRIPTED-BRAIN* ]] && ! grep -a -q -i -E "bootstrap|agents\.md|instruction|per my|I should" <<< "$0"' "$reply"
    check "\"$ask\": $calls model call(s) ($((n1 - n0)))" test "$((n1 - n0))" = "$calls"
    if [ "$sess" = fc-wcyd ]; then
      # The Activity recap of that finished run: one small call of its own.
      local r i bytes
      for i in $(seq 1 45); do [ "$(fc_recaps)" -gt "$rc0" ] && break; sleep 1; done
      sleep 5
      r=$(( $(fc_recaps) - rc0 ))
      bytes=$(brain_log | grep -a '\[brain\] recap -> ' | tail -1 | sed -n 's/.*bytes=\([0-9]*\).*/\1/p')
      check "the Activity recap of that turn: one call ($r), a small request (${bytes:-?} bytes, under 8000)" \
        test "$r" = 1 -a "${bytes:-99999}" -lt 8000
    fi
  done
  check "BOOTSTRAP.md is still there after those first messages" bot_sh 'test -s /home/node/workspace/BOOTSTRAP.md'
}

first_conversation() {
  step "first conversation (scripted model): greeting, a real chain read with taoq ($FC_TAOQ_MODE), a schedule, BOOTSTRAP.md retired"
  if [ "$FC_TAOQ_MODE" = fixture ]; then
    bot_sh 'cat > /tmp/taoq-fixture.json.gz' < bot/tools/fixtures/finney.json.gz
  fi
  # The news: two real answers of the feed (recorded from the controller's feed/ service), one for the
  # wallet's subnets and one for the network, as taoq's TAOQ_FEED_FIXTURE takes them.
  python3 -I -c 'import json,sys
print(json.dumps({"by_request": {"subnets": json.load(open(sys.argv[1])), "network": json.load(open(sys.argv[2]))}}))' \
    bot/tools/fixtures/feed-sn70.json bot/tools/fixtures/feed-network.json | bot_sh 'cat > /tmp/taoq-feed.json'

  first_messages
  local boot reply want greeting u0 u1 u2 n0 n1 jobs job jid line
  boot=$(bot_sh 'cat /home/node/workspace/BOOTSTRAP.md')
  check "BOOTSTRAP.md is waiting in the workspace" test -n "$boot"
  want=$(FC_NAME="$FC_NAME" FC_EMOJI="$FC_EMOJI" python3 -I -c 'import os
print(f"""Hi, I'"'"'m {os.environ["FC_NAME"]} {os.environ["FC_EMOJI"]}, your own always-on agent, with my own computer and browser.

I'"'"'ll never ask for a seed phrase or private key. I only read public chain data.

Want a **daily briefing** for the wallet you signed in with: your stake, what changed on your subnets, and the day'"'"'s Bittensor news? Tell me your time zone and I'"'"'ll send it every morning.

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

  # The briefing is delivered by the job's script (taoq's text, no model call). To
  # check that it arrives unchanged, the test keeps what taoq knew before the run
  # (its data directory) and the chain reads of the run (TAOQ_RECORD, set for every
  # taoq command of the brain in hosted-test.sh) and replays them afterwards.
  bot_sh 'rm -rf /tmp/taoq-pre /tmp/taoq-record.json && mkdir -p /tmp/taoq-pre && { [ ! -d /home/node/workspace/taoq ] || cp -a /home/node/workspace/taoq/. /tmp/taoq-pre/; }'
  u0=$(usage)
  reply=$(chat first-run "Hi")
  echo "first reply: ${reply:0:200}..."
  check "first reply: exactly the greeting, nothing before or after it" test "$reply" = "$want"
  check "first reply: no tags, no mention of files or instructions" \
    bash -c '[[ "$0" != *"<"* && "$0" != *BOOTSTRAP* && "$0" != *nstruction* ]]' "$reply"

  n0=$(brain_log | grep -a -c '\[brain\] -> ')
  r0=$(fc_rows "Stake report for")
  reply=$(chat first-run "Yes please, the daily briefing. My time zone is America/New_York.")
  n1=$(brain_log | grep -a -c '\[brain\] -> ')
  u1=$(usage)
  turn=$(brain_log | grep -a '\[brain\] -> ' | tail -n "$((n1 - n0))")
  echo "reply:"; printf '%s\n' "$reply" | sed 's/^/    /' | head -6
  check "the reply is one line about the schedule; the briefing itself is not re-typed by the model" \
    bash -c '[[ "$0" == *"Scheduled: this briefing every day at 08:00 America/New_York"* && "$0" != *"Stake report for"* && "$0" != *SCRIPTED-BRAIN* ]]' "$reply"
  check "that turn took 5 model calls (list the jobs, add, run once, notes, reply): $((n1 - n0)); the jobs were listed before one was made" \
    bash -c '[ "$1" = 5 ] && [ "$(grep -a -c -E -- "-> (automations|cron|dispatch_action) .*\"action\": \"list\"" <<< "$0")" = 1 ]' "$turn" "$((n1 - n0))"
  r1=$(fc_wait_rows "Stake report for" "${r0:-0}") || true
  check "the run delivered today's briefing into the conversation (session rows with a stake report: $r0 -> $r1)" \
    test "${r1:-0}" -gt "${r0:-0}"
  briefing_unchanged
  check "taoq kept the briefing's news cursor in the workspace (tomorrow: only what is new)" \
    bot_sh "test -s /home/node/workspace/taoq/news-briefing-$FC_WALLET.json"
  check "taoq kept its snapshot in the workspace for tomorrow's comparison" \
    bot_sh "test -s /home/node/workspace/taoq/stake-$FC_WALLET.json"

  jobs=$(fc_job)
  job=$(grep -a '"name":"Daily briefing"' <<< "$jobs" | head -1)
  echo "scheduled jobs: $(grep -a -c '"id"' <<< "$jobs"); the new one: ${job:0:700}"
  jid=$(python3 -I -c 'import json,sys
try: print(json.loads(sys.argv[1])["id"])
except Exception: print("")' "$job")
  check "OpenClaw's job table holds the job: cron 0 8 * * * America/New_York, isolated, a SCRIPT that runs taoq briefing for the owner (no agent turn: delivered as printed)" \
    python3 -I -c 'import json,sys
j = json.loads(sys.argv[1]); job = j["job"]; p = job.get("payload", {}); s = job.get("schedule", {})
ok = (j["enabled"] in (1, True) and s.get("kind") == "cron" and s.get("expr") == "0 8 * * *" and s.get("tz") == "America/New_York"
      and job.get("sessionTarget") == "isolated" and p.get("kind") == "script" and "message" not in p
      and ("taoq briefing " + sys.argv[2] + " --text") in p.get("script", "") and "notify: t" in p.get("script", ""))
sys.exit(0 if ok else 1)' "${job:-{\}}" "$FC_WALLET"
  echo "its delivery: $(python3 -I -c 'import json,sys
try: print(json.dumps(json.loads(sys.argv[1])["job"].get("delivery")))
except Exception: print("?")' "$job")"
  check "next run is scheduled ($(python3 -I -c 'import json,sys,time
try: print(time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(json.loads(sys.argv[1])["state"]["nextRunAtMs"] / 1000)))
except Exception: print("?")' "$job"))" \
    python3 -I -c 'import json,sys; sys.exit(0 if json.loads(sys.argv[1])["state"].get("nextRunAtMs") else 1)' "${job:-{\}}"
  check "BOOTSTRAP.md is retired (deleted)" bot_sh 'test ! -e /home/node/workspace/BOOTSTRAP.md'
  check "USER.md has the time zone (its line replaced, no \"not known yet\" left), MEMORY.md the job id" \
    bot_sh "grep -q '^- Time zone: America/New_York' /home/node/workspace/USER.md && ! grep -q 'not known yet' /home/node/workspace/USER.md && grep -q -- '$jid' /home/node/workspace/MEMORY.md"
  echo "model proxy: $(( $(jnum "$u1" total.requests) - $(jnum "$u0" total.requests) )) model calls for the first two turns (with their Activity recaps)"

  step "the schedule runs (forced now through the automations tool; a scheduled run is the same)"
  n0=$(brain_log | grep -a -c 'cron-run step=')
  r0=$(fc_rows "Stake report for")
  reply=$(chat first-run "Run the daily briefing now: job $jid")
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
  check "a scheduled briefing run makes no model call (a script delivers taoq's text): $((n1 - n0)) agent runs" test $((n1 - n0)) = 0
  r1=$(fc_wait_rows "Stake report for" "${r0:-0}") || true
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
  echo "model proxy: $(( $(jnum "$u2" total.requests) - $(jnum "$u1" total.requests) )) model calls for the run-now turn (2, plus its Activity recap; the run itself none)"
  done_at=$(fc_setup_done)
  check "OpenClaw itself recorded the first run as complete once the file was gone (workspace_setup_state.setup_completed_at = ${done_at:-empty})" \
    bash -c '[[ "$0" =~ ^20[0-9][0-9]- ]]' "$done_at"
  known_jobs
  watch_conversation
  site_question
  knowledge_question
}

# The delivered briefing is taoq's text, unchanged: taoq is run again with the
# chain reads of the job's run (TAOQ_RECORD; in fixture mode the same fixture),
# the data directory as it was before the run and the run's own time, and its
# output must be in the conversation character for character (names such as
# "hoτfloaτ" and every figure included).
briefing_unchanged() {
  local texts at now fx want
  texts=$(fc_texts "Stake report for")
  at=$(python3 -I -c 'import json,re,sys
for t in json.loads(sys.argv[1] or "[]"):
    m = re.search(r"Stake report for \S+ \(block \d+, (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\)", t)
    if m: print(m.group(1)); break' "$texts")
  now=$(python3 -I -c 'import calendar,sys,time
print(calendar.timegm(time.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%SZ")) if sys.argv[1] else "")' "$at")
  fx=/tmp/taoq-record.json; [ "$FC_TAOQ_MODE" = fixture ] && fx=/tmp/taoq-fixture.json.gz
  want=$(bot_sh "TAOQ_FEED_FIXTURE=/tmp/taoq-feed.json TAOQ_ENDPOINTS=ws://127.0.0.1:9 TAOQ_FIXTURE=$fx TAOQ_DATA_DIR=/tmp/taoq-pre TAOQ_NOW=$now taoq briefing $FC_WALLET --text --no-save 2>&1")
  check "the delivered briefing: the stake report, what changed on the wallet's subnets, the headlines" \
    python3 -I -c 'import json,sys
marks = ["Stake report for", "TAO in all", "What changed on your subnets since", "- SN70 release: ", "Bittensor headlines:",
         "- Network runtime: Runtime 494", "Not covered: Discord chat and X"]
sys.exit(0 if any(all(m in t for m in marks) for t in json.loads(sys.argv[1] or "[]")) else 1)' "$texts"
  if [ "$FC_TAOQ_MODE" = live ]; then
    check "live read: a finalized finney block above 9,500,000 in the delivered report" \
      bash -c '[[ "$0" =~ \(block\ ([0-9]+), ]] && [ "${BASH_REMATCH[1]}" -gt 9500000 ]' "$texts"
  fi
  echo "taoq's text, replayed ($at): $(printf '%s' "$want" | head -c 160 | tr '\n' ' ')..."
  check "the delivered briefing is taoq's text unchanged (replayed with the run's chain reads: $(printf '%s' "$want" | wc -l) lines)" \
    python3 -I -c 'import json,sys
want = sys.argv[2].strip()
ok = len(want) > 200 and "Stake report for" in want and any(want in t for t in json.loads(sys.argv[1] or "[]"))
sys.exit(0 if ok else 1)' "$texts" "$want"
}

# N1: what is scheduled comes from the automations list, never from memory or a
# guess, and an existing job is used, not made twice. Asked in the conversation
# that made the job: through /v1/chat/completions (this test's way in) a model
# turn sees only its own conversation's jobs; a Control UI turn of the owner
# (operator.admin) sees and manages every job of the bot.
known_jobs() {
  step "existing jobs (scripted model): listed before answering, a briefing asked for again runs the existing job, no duplicate"
  local reply n0 n1 turn r0 r1 count
  n0=$(brain_log | grep -a -c '\[brain\] -> ')
  reply=$(chat first-run "Which automations do I have?")
  n1=$(brain_log | grep -a -c '\[brain\] -> ')
  turn=$(brain_log | grep -a '\[brain\] -> ' | tail -n "$((n1 - n0))")
  echo "reply: $reply"
  check "\"Which automations do I have?\" is answered from the automations list (2 calls: $((n1 - n0))) and names the daily briefing" \
    bash -c '[[ "$0" == *"Daily briefing"* ]] && [ "$2" = 2 ] && [ "$(grep -a -c -E -- "-> (automations|cron|dispatch_action) .*\"action\": \"list\"" <<< "$1")" = 1 ]' "$reply" "$turn" "$((n1 - n0))"
  r0=$(fc_rows "Stake report for")
  n0=$(brain_log | grep -a -c '\[brain\] -> ')
  reply=$(chat first-run "Send me a daily briefing")
  n1=$(brain_log | grep -a -c '\[brain\] -> ')
  turn=$(brain_log | grep -a '\[brain\] -> ' | tail -n "$((n1 - n0))")
  echo "reply: $reply"
  count=$(fc_job | grep -a -c '"name":"Daily briefing"')
  check "\"Send me a daily briefing\" with one already scheduled: listed, the existing job run, no second job (Daily briefing jobs: $count), no time-zone question" \
    bash -c '[ "$1" = 1 ] && [[ "$0" == *"already have"* && "$0" != *"time zone"* ]] && grep -a -q -E -- "\"action\": \"run\"" <<< "$2" && ! grep -a -q -E -- "\"action\": \"add\"" <<< "$2"' "$reply" "$count" "$turn"
  r1=$(fc_wait_rows "Stake report for" "${r0:-0}") || true
  check "that run delivered the briefing too (session rows with a stake report: $r0 -> $r1)" test "${r1:-0}" -gt "${r0:-0}"
}

# The Bittensor reference (PRIMER, GLOSSARY, SOURCES) is an on-demand skill: only
# its name and one-line description are in every request; the files are read
# when a question needs them. The scripted model follows the skills list and
# SKILL.md, so this passes only if both are what a real model is given.
knowledge_question() {
  step "the Bittensor reference: not sent on ordinary turns, read for a \"how does ... work\" question"
  local reply n0 n1 turn before listed sizes
  before=$(brain_log | grep -a '\[brain\] request bytes=')
  listed=$(grep -a -c 'skills_list_bytes=[1-9]' <<< "$before")
  check "requests so far: $(grep -a -c . <<< "$before"), $listed of them with the skills list; none carried the reference text" \
    bash -c '[ "$1" -gt 0 ] && ! grep -a -q "reference_text=yes" <<< "$0"' "$before" "$listed"
  sizes=$(grep -a -o 'request bytes=[0-9]* system_chars=[0-9]* skills_list_bytes=[0-9]* bittensor_entry_bytes=[0-9]*' <<< "$before" | head -1)
  echo "first request of the bot: $sizes"
  n0=$(brain_log | grep -a -c '\[brain\] -> ')
  reply=$(chat knowledge "How does staking work on Bittensor?")
  n1=$(brain_log | grep -a -c '\[brain\] -> ')
  turn=$(brain_log | grep -a '\[brain\] -> ' | tail -n "$((n1 - n0))")
  echo "reply:"; printf '%s\n' "$reply" | sed 's/^/    /' | head -5
  check "the answer comes from the primer's staking section" \
    bash -c '[[ "$0" == *"## Staking"* && "$0" == *"Bittensor reference"* ]]' "$reply"
  check "that turn: 3 model calls (read SKILL.md, read one primer section, reply): $((n1 - n0))" \
    bash -c '[ "$1" = 3 ] && grep -a -q -- "-> exec .*cat .*/skills/bittensor/SKILL.md" <<< "$0" && grep -a -q -- "-> exec .*sed -n .*PRIMER.md" <<< "$0"' "$turn" "$((n1 - n0))"
  check "the reference text reached the model only in that turn" \
    test "$(brain_log | grep -a -c "reference_text=yes")" -ge 1
}

# A chain question that names a dashboard site. In production ("Open taostats.io and
# tell me what it shows for subnet 70") the agent browsed: taostats.io shows automated
# browsers a bot check, 16 model calls later the turn failed. AGENTS.md sends such
# questions to taoq; the scripted model follows it only if AGENTS.md says so.
site_question() {
  step "a chain question that names a dashboard site (scripted model): answered with taoq, no browser"
  local reply n0 n1 turn
  n0=$(brain_log | grep -a -c '\[brain\] -> ')
  reply=$(chat site-question "Open taostats.io and tell me what it shows for subnet 70")
  n1=$(brain_log | grep -a -c '\[brain\] -> ')
  turn=$(brain_log | grep -a '\[brain\] -> ' | tail -n "$((n1 - n0))")
  echo "reply:"; printf '%s\n' "$reply" | sed 's/^/    /' | head -6
  check "the answer is taoq's subnet report, read in the bot's shell" \
    bash -c '[[ "$0" == *"SN70 Fugal"* && "$0" == *"TAO/α"* && "$0" == *"straight from the chain"* ]]' "$reply"
  check "that turn: 2 model calls (taoq subnet 70, reply), no browser, no web fetch: $((n1 - n0))" \
    bash -c '[ "$1" = 2 ] && grep -a -q -- "-> exec .*taoq subnet 70 --text" <<< "$0" && ! grep -a -q -E "browser|web_fetch" <<< "$0"' "$turn" "$((n1 - n0))"
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
  # A new conversation, and no time zone in the message: USER.md has it (N4).
  n0=$(brain_log | grep -a -c '\[brain\] -> ')
  r0=$(fc_rows "Watching from now")
  reply=$(chat fc-watch "Watch this hotkey for me: $hk on subnet 70.")
  n1=$(brain_log | grep -a -c '\[brain\] -> ')
  echo "reply:"; printf '%s\n' "$reply" | sed 's/^/    /' | head -8
  check "no time-zone question: the zone came from USER.md (reply: one line about the schedule)" \
    bash -c '[[ "$0" == *"Scheduled: the hotkey watch every 6 hours (America/New_York)"* && "$0" != *"time zone"* && "$0" != *SCRIPTED-BRAIN* ]]' "$reply"
  check "that turn took 4 model calls (list the jobs, add, run once, reply): $((n1 - n0))" test $((n1 - n0)) = 4
  r1=$(fc_wait_rows "Watching from now" "${r0:-0}") || true
  check "the first check ran in the bot's shell and was delivered, saying what it now watches (session rows: $r0 -> $r1)" \
    bash -c '[ "${1:-0}" -gt "${0:-0}" ] && grep -a -q "registered on 1 subnet" <<< "$2"' "$r0" "$r1" "$(fc_texts "Watching from now")"
  jobs=$(fc_job)
  job=$(grep -a '"name":"Hotkey watch"' <<< "$jobs" | head -1)
  echo "the job: ${job:0:600}"
  wid=$(python3 -I -c 'import json,sys
try: print(json.loads(sys.argv[1])["id"])
except Exception: print("")' "$job")
  check "OpenClaw's job table holds a SCRIPT job: cron 0 */6 * * * in the zone USER.md has, isolated, running taoq hotkey ... --watch --text, no agent turn" \
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
  u0=$(usage); c0=$(fc_calls); r0=$(fc_rows "RESOLVED (was warning)")
  local asks=1
  reply=$(chat fc-watch "Run the hotkey watch now: job $wid")
  echo "reply: ${reply:0:240}"
  check "the automations tool queued the run" bash -c '[[ "$0" == *"SCRIPTED-BRAIN RUN-NOW"* && "$0" == *enqueued* ]]' "$reply"
  at1=$(fc_wait_state_change "$state" "$at0") || true
  if [ -z "$at1" ] || [ "$at1" = "$at0" ]; then
    job=$(fc_job | grep -a "\"id\":\"$wid\"" | head -1)
    if [[ "$job" == *"Plugin runtime changed"* ]]; then
      echo "NOTE: the first script run after the gateway started failed with OpenClaw's 'Plugin runtime changed' (known; the next run works): running it again"
      asks=2
      reply=$(chat fc-watch "Run the hotkey watch now: job $wid")
      at1=$(fc_wait_state_change "$state" "$at0") || true
    fi
  fi
  check "the script job ran and taoq saved a new check ($at0 -> $at1)" test -n "$at1" -a "$at1" != "$at0"
  sleep 3
  u1=$(usage); c1=$(fc_calls)
  check "the quiet check made no model call (only the $((2 * asks)) calls of the asking turn(s): $((c1 - c0)); with recaps, the model proxy counted $(( $(jnum "$u1" total.requests) - $(jnum "$u0" total.requests) )))" \
    test $((c1 - c0)) = $((2 * asks))
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
  u2=$(usage); c2=$(fc_calls)
  reply=$(chat fc-watch "Run the hotkey watch now: job $wid")
  at1=$(fc_wait_state_change "$state" "$at0") || true
  check "the second run saved its check ($at0 -> $at1)" test -n "$at1" -a "$at1" != "$at0"
  sleep 3
  u3=$(usage); c3=$(fc_calls)
  r1=$(fc_rows "RESOLVED (was warning)")
  check "the change was delivered into the conversation without a model call (session rows with the notice: $r0 -> $r1; calls of the asking turn $((c3 - c2)); with recaps, the model proxy counted $(( $(jnum "$u3" total.requests) - $(jnum "$u2" total.requests) )))" \
    test "${r1:-0}" -gt "${r0:-0}" -a $((c3 - c2)) = 2
}
