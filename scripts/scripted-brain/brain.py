"""TEST ONLY: a deterministic OpenAI-compatible "brain" for scripts/smoke.sh.

It stands in for the model endpoint behind the model proxy, so the smoke test
can drive the agent's REAL tool loop (OpenClaw's own exec / browser tools, on
the bot's own computer) and the real metering path without any paid call or
real key. It is not part of the bot; it runs only under
scripts/smoke.compose.yaml, on a network the bot itself is not attached to.

What it answers, by the person's last message (OpenClaw's own trailing
"runtime context" message is skipped):
  mentions "computer"  -> the tool script, counted by tool results so far:
       0 -> exec:    one shell command that creates ~/workspace/smoke-file.txt
       1 -> browser: open https://example.com in the bot's (visible) browser
            (directly, or via dispatch_action when OpenClaw hides it behind Tool Search)
       2 -> final text summarising both tool results.
  mentions "code word" -> acknowledges a code word like PELICAN-42, or repeats
       the one from an earlier user message (so the test can tell that the
       conversation history survived a restart).
  the first conversation (bot/seed/BOOTSTRAP.md in the system prompt; driven by
  scripts/first-conversation.sh):
       "Hi"                       -> the greeting, copied out of BOOTSTRAP.md
       "... daily stake report ... <Area/City>" -> exec `taoq stake <address
            from the workspace files> --text`; automations add (the daily
            job); exec (notes, rm BOOTSTRAP.md); the report + one line
       "Run the daily stake report now: job <id>" -> automations run (forced)
       "Watch this hotkey for me: <hotkey> on subnet 70 ... <Area/City>" -> exec
            `taoq hotkey <hotkey> --netuid 70 --watch --text`; automations add
            (a SCRIPT job: the script is copied out of the recipe in AGENTS.md,
            so the test runs the very text a real model is given); the reply
       "Run the hotkey watch now: job <id>" -> automations run (forced)
       "Open <site> ... subnet <N>" -> exec `taoq subnet <N> --text` (only if
            AGENTS.md sends such questions to taoq), then its output as the reply
       a job's own message "Run exactly: taoq ..." -> exec it; reply with its
            output (BRAIN_TAOQ_ENV, e.g. a recorded fixture, goes in front of
            every taoq command)
       "How does <topic> work ..." -> only if the `bittensor` skill is in the
            prompt's <available_skills>: exec `cat` of its SKILL.md (at the
            listed location), then the `sed` command SKILL.md gives for the
            primer section, then that section as the reply
  anything else        -> one fixed sentence.

Each request is logged with its size, the size of the prompt's skills list,
and whether a piece of the Bittensor reference files (skill bodies, not the
list) is in it ("request bytes=... skills_list_bytes=... reference_text=no"),
so a test can tell that the reference is sent only when the agent reads it.

Each request's log line also names the request-body fields that are not part
of the standard chat-completions set ("extra_fields=..."), which is how the
smoke test sees that MODEL_EXTRA_BODY reached the model. With BRAIN_DUMP=1 it
logs each whole request body as well (debugging only; off in the smoke test).

Like a real endpoint it reports token usage: always on plain replies, on
streams only when asked (stream_options.include_usage), as a last chunk with
empty `choices`. With BRAIN_EXPECT_KEY set it refuses any other API key, which
is how the test knows the model proxy replaced the agent's placeholder.
"""

import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EXPECT_KEY = os.environ.get("BRAIN_EXPECT_KEY", "")
DUMP = os.environ.get("BRAIN_DUMP", "") == "1"
TAOQ_ENV = os.environ.get("BRAIN_TAOQ_ENV", "")
STANDARD_FIELDS = {
    "model", "messages", "tools", "tool_choice", "stream", "stream_options", "max_tokens",
    "max_completion_tokens", "temperature", "top_p", "stop", "n", "user", "seed", "store",
    "presence_penalty", "frequency_penalty", "parallel_tool_calls", "response_format",
}
STAMP_CMD = (
    "echo \"created by the bot's shell at $(date -u +%FT%TZ) on $(hostname)\" "
    "> /home/node/workspace/smoke-file.txt && uname -sr && id -un && "
    "ls -l /home/node/workspace/smoke-file.txt && cat /home/node/workspace/smoke-file.txt"
)
CODE_WORD = re.compile(r"(?i:code word)\b[^.?!]*?\b([A-Z]{3,}-\d+)\b", re.S)


def log(*a):
    print("[brain]", *a, file=sys.stderr, flush=True)


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def call_tool(tools, name, args, *aliases):
    """A tool call by name; through dispatch_action when OpenClaw hides it behind Tool Search."""
    for n in (name, *aliases):
        if n in tools:
            return ("tool", n, args)
    if "dispatch_action" in tools:
        return ("tool", "dispatch_action", {"id": name, "args": args})
    return ("text", f"SCRIPTED-BRAIN: the {name} tool was not offered", None)


def taoq_cmd(cmd):
    """The command as the bot would run it; BRAIN_TAOQ_ENV (e.g. a recorded fixture) goes in front."""
    return f"{TAOQ_ENV} {cmd}".strip()


# The first conversation (bot/seed/BOOTSTRAP.md), as a model that follows it would
# carry it. The greeting is copied out of the BOOTSTRAP.md that OpenClaw put in front
# of the model, so the test sees exactly what a real model was given.
GREETING = re.compile(r"(Hi, I'm .*?Or just tell me what you need\.)", re.S)
ADDRESS = re.compile(r"address `(5[1-9A-HJ-NP-Za-km-z]{46,48})`")
ZONE = re.compile(r"\b([A-Z][a-z]+/[A-Z][A-Za-z_]+)\b")
JOB_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# OpenClaw may put a "[Mon 2026-11-23 09:01 UTC]" style stamp in front of a message.
STAMP = re.compile(r"^\s*\[[^\]\n]{0,80}\]\s*")


HOTKEY = re.compile(r"hotkey[^5]*?(5[1-9A-HJ-NP-Za-km-z]{46,48})")
# "Open taostats.io and tell me what it shows for subnet 70"
SITE_ASK = re.compile(r"(?i)\bopen ([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b.*?\bsubnet (\d{1,3})\b", re.S)
# The script payload of the watch recipe in AGENTS.md: payload: {kind: "script", script: "<JSON string>"}
WATCH_SCRIPT = re.compile(r'script: ("(?:[^"\\]|\\.)*")\}')


SKILL_ENTRY = re.compile(r"<name>bittensor</name>\s*<description>[^<]*</description>\s*<location>([^<]+)</location>")
PRIMER_SED = re.compile(r"sed -n '/\^## Staking/,/\^## /p' \{baseDir\}/PRIMER\.md")
HOW = re.compile(r"(?i)^how does (\w+) work")
# Phrases found only in the reference files (PRIMER.md, GLOSSARY.md), never in
# the always-sent prompt: if one is in a request, the agent read that file.
REFERENCE_MARKS = ("Balancer-style", "Activity cutoff")


def skills_list(system):
    a, b = system.find("<available_skills>"), system.find("</available_skills>")
    return system[a:b + len("</available_skills>")] if a >= 0 and b > a else ""


def first_conversation(ask, system, tools, results):
    step = len(results)
    ask = STAMP.sub("", ask)
    # ... and may add its own "Runtime: ..." lines after the person's words.
    said = ask.split("\n\nRuntime:")[0].strip()
    if "BOOTSTRAP.md" in system and said.lower().rstrip("!.") in ("hi", "hello", "hey"):
        g = GREETING.search(system)
        return ("text", g.group(1) if g else "SCRIPTED-BRAIN: no greeting found in BOOTSTRAP.md", None)
    if "daily stake report" in ask.lower() and ZONE.search(ask):
        addr = ADDRESS.search(system)
        if not addr:
            return ("text", "SCRIPTED-BRAIN: no address in the workspace files", None)
        addr, zone = addr.group(1), ZONE.search(ask).group(1)
        report = f"taoq stake {addr} --text"
        if step == 0:
            return call_tool(tools, "exec", {"command": taoq_cmd(report)})
        if step == 1:
            message = (f"Run exactly: {report}. Reply with its output unchanged, then at most one plain sentence about any "
                       "line starting with \"!\". If it fails twice, reply only: Daily stake report: the chain could not be read this morning.")
            job = {"name": "Daily stake report", "schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": zone},
                   "sessionTarget": "isolated",
                   "payload": {"kind": "agentTurn", "lightContext": True, "toolsAllow": ["exec"], "timeoutSeconds": 300,
                               "message": message}}
            return call_tool(tools, "automations", {"action": "add", "job": job}, "cron")
        if step == 2:
            jid = JOB_ID.search(text_of(results[1].get("content")))
            jid = jid.group(0) if jid else "unknown"
            cmd = (f"printf '%s\\n' '- Time zone: {zone}' '- Wants: daily stake report at 08:00' >> ~/workspace/USER.md && "
                   f"printf '%s\\n' '# MEMORY.md' '- Job \"Daily stake report\" id {jid}, cron 0 8 * * * {zone}, command: {report}' "
                   f">> ~/workspace/MEMORY.md && rm -f ~/workspace/BOOTSTRAP.md && echo retired")
            return call_tool(tools, "exec", {"command": cmd})
        first = text_of(results[0].get("content"))
        return ("text", f"{first.strip()}\n\nScheduled: this report every day at 08:00 {zone}. "
                        "Ask me to change or stop it.", None)
    if ask.lower().startswith("watch this hotkey") and HOTKEY.search(ask) and ZONE.search(ask):
        hk, zone = HOTKEY.search(ask).group(1), ZONE.search(ask).group(1)
        watch = f"taoq hotkey {hk} --netuid 70 --watch --text"
        if step == 0:
            return call_tool(tools, "exec", {"command": taoq_cmd(watch)})
        if step == 1:
            recipe = WATCH_SCRIPT.search(system)
            if not recipe:
                return ("text", "SCRIPTED-BRAIN: no script recipe in the workspace files", None)
            script = json.loads(recipe.group(1)).replace("<COMMAND>", taoq_cmd(watch))
            job = {"name": "Hotkey watch", "schedule": {"kind": "cron", "expr": "0 */6 * * *", "tz": zone},
                   "sessionTarget": "isolated", "payload": {"kind": "script", "script": script}}
            return call_tool(tools, "automations", {"action": "add", "job": job}, "cron")
        return ("text", f"{text_of(results[0].get('content')).strip()}\n\nScheduled: the hotkey watch every 6 hours. "
                        "Ask me to change or stop it.", None)
    if ask.lower().startswith(("run the daily stake report now", "run the hotkey watch now")):
        jid = JOB_ID.search(ask)
        if step == 0 and jid:
            return call_tool(tools, "automations", {"action": "run", "jobId": jid.group(0), "runMode": "force",
                                                    "timeoutMs": 120000}, "cron")
        return ("text", "SCRIPTED-BRAIN RUN-NOW: " + " | ".join(text_of(m.get("content"))[:300].replace("\n", " ") for m in results), None)
    site = SITE_ASK.search(said)
    if site:
        # AGENTS.md: a chain question goes to taoq even when the user names a
        # dashboard site (taostats.io blocks automated browsers).
        if "taostats.io" not in system or "taoq" not in system:
            return ("text", "SCRIPTED-BRAIN: AGENTS.md says nothing about sites like taostats.io", None)
        if step == 0:
            return call_tool(tools, "exec", {"command": taoq_cmd(f"taoq subnet {site.group(2)} --text")})
        return ("text", f"{text_of(results[0].get('content')).strip()}\n\nRead straight from the chain with taoq; "
                        f"{site.group(1)} blocks automated browsers.", None)
    how = HOW.search(said)
    if how:
        # A "how does it work" question: the agent loads the reference skill
        # listed in its prompt and reads one section, as SKILL.md says.
        entry = SKILL_ENTRY.search(system)
        if not entry:
            return ("text", "SCRIPTED-BRAIN: no bittensor skill in the prompt's skills list", None)
        location = entry.group(1).strip()
        if step == 0:
            return call_tool(tools, "exec", {"command": f"cat {location}"})
        if step == 1:
            skill = text_of(results[0].get("content"))
            if not PRIMER_SED.search(skill):
                return ("text", "SCRIPTED-BRAIN: SKILL.md does not say how to read a primer section", None)
            base = location.rsplit("/", 1)[0]
            return call_tool(tools, "exec", {"command": f"sed -n '/^## Staking/,/^## /p' {base}/PRIMER.md"})
        section = text_of(results[1].get("content")).strip()
        return ("text", f"{section[:900]}\n\n(From the Bittensor reference.)", None)
    if "Run exactly: taoq " in ask:
        # A scheduled run of a job made above: run the command once, report its output.
        log(f"cron-run step={step}")
        if step == 0:
            cmd = ask[ask.index("Run exactly: taoq ") + len("Run exactly: "):].split(". ")[0]
            return call_tool(tools, "exec", {"command": taoq_cmd(cmd)})
        return ("text", text_of(results[0].get("content")).strip() or "SCRIPTED-BRAIN: empty tool output", None)
    return None


def plan(req):
    msgs = req.get("messages", [])
    tools = {t.get("function", {}).get("name") for t in req.get("tools", []) or []}
    system = " ".join(text_of(m.get("content")) for m in msgs if m.get("role") in ("system", "developer"))
    # OpenClaw adds its own "runtime context" user message after the person's.
    said = [i for i, m in enumerate(msgs) if m.get("role") == "user"
            and not text_of(m.get("content")).startswith("OpenClaw runtime context")]
    last_user = said[-1] if said else -1
    ask = text_of(msgs[last_user].get("content")) if last_user >= 0 else ""
    results = [m for m in msgs[last_user + 1:] if m.get("role") == "tool"]
    step = len(results)
    log(f"step={step} tools_offered={len(tools)} ask={ask[:70]!r}")
    first = first_conversation(ask, system, tools, results)
    if first is not None:
        return first
    if "computer" in ask.lower() or results:
        if step == 0 and "exec" in tools:
            return ("tool", "exec", {"command": STAMP_CMD})
        browse = {"action": "open", "targetUrl": "https://example.com"}
        if step <= 1 and not any("example.com" in text_of(m.get("content")) for m in results):
            if "browser" in tools:
                return ("tool", "browser", browse)
            # OpenClaw may hide most tools behind Tool Search; dispatch by name.
            if "dispatch_action" in tools:
                return ("tool", "dispatch_action", {"id": "browser", "args": browse})
        summary = " | ".join(text_of(m.get("content"))[:300].replace("\n", " ") for m in results)
        return ("text", f"SCRIPTED-BRAIN DONE. Tool results: {summary}", None)
    if "code word" in ask.lower():
        told = CODE_WORD.search(ask)
        if told:
            return ("text", f"Noted. I will remember the code word {told.group(1)}.", None)
        for i in reversed(said[:-1]):
            earlier = CODE_WORD.search(text_of(msgs[i].get("content")))
            if earlier:
                return ("text", earlier.group(1), None)
        return ("text", "I was not told a code word in this conversation.", None)
    return ("text", "Hello from the scripted test model.", None)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _key_ok(self):
        if not EXPECT_KEY or self.headers.get("Authorization", "") == f"Bearer {EXPECT_KEY}":
            return True
        log("refused: not the expected API key")
        self._json(401, {"error": {"message": "Incorrect API key provided.", "type": "invalid_request_error",
                                   "param": None, "code": "invalid_api_key"}})
        return False

    def do_GET(self):
        if not self._key_ok():
            return
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": [{"id": "tools", "object": "model"}]})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(n) or b"{}"
        if not self._key_ok():
            return
        req = json.loads(raw)
        extra = {k: req[k] for k in sorted(req) if k not in STANDARD_FIELDS}
        log("extra_fields=" + (json.dumps(extra, separators=(",", ":"))[:400] if extra else "none"))
        system = " ".join(text_of(m.get("content")) for m in req.get("messages", [])
                          if m.get("role") in ("system", "developer"))
        everything = raw.decode("utf-8", "replace")
        log(f"request bytes={len(raw)} system_chars={len(system)} skills_list_bytes={len(skills_list(system).encode())} "
            f"reference_text={'yes' if any(m in everything for m in REFERENCE_MARKS) else 'no'}")
        if DUMP:
            log("dump", raw.decode("utf-8", "replace"))
        kind, a, b = plan(req)
        cid, now = f"chatcmpl-{int(time.time()*1000)}", int(time.time())
        if kind == "tool":
            call = {"index": 0, "id": f"call_{now}_{a}", "type": "function",
                    "function": {"name": a, "arguments": json.dumps(b)}}
            msg, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
            log("->", a, json.dumps(b)[:300])
        else:
            msg, finish = {"role": "assistant", "content": a}, "stop"
            log("-> text")
        out = max(1, len(json.dumps(msg)) // 4)
        usage = {"prompt_tokens": max(1, len(raw) // 4), "completion_tokens": out,
                 "total_tokens": max(1, len(raw) // 4) + out}
        if not req.get("stream"):
            return self._json(200, {"id": cid, "object": "chat.completion", "created": now, "model": "tools",
                                    "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                                    "usage": usage})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def ev(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")

        base = {"id": cid, "object": "chat.completion.chunk", "created": now, "model": "tools"}
        ev({**base, "choices": [{"index": 0, "delta": {k: v for k, v in msg.items()}, "finish_reason": None}]})
        ev({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        if (req.get("stream_options") or {}).get("include_usage") is True:
            ev({**base, "choices": [], "usage": usage})
            log("usage chunk sent (the request asked for it)")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    log("scripted brain on :11500 (test only)", "- requires the expected API key" if EXPECT_KEY else "")
    ThreadingHTTPServer(("0.0.0.0", 11500), H).serve_forever()
