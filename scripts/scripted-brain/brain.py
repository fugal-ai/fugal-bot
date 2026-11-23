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
  anything else        -> one fixed sentence.

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


def plan(req):
    msgs = req.get("messages", [])
    tools = {t.get("function", {}).get("name") for t in req.get("tools", []) or []}
    # OpenClaw adds its own "runtime context" user message after the person's.
    said = [i for i, m in enumerate(msgs) if m.get("role") == "user"
            and not text_of(m.get("content")).startswith("OpenClaw runtime context")]
    last_user = said[-1] if said else -1
    ask = text_of(msgs[last_user].get("content")) if last_user >= 0 else ""
    results = [m for m in msgs[last_user + 1:] if m.get("role") == "tool"]
    step = len(results)
    log(f"step={step} tools_offered={len(tools)}")
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
        kind, a, b = plan(req)
        cid, now = f"chatcmpl-{int(time.time()*1000)}", int(time.time())
        if kind == "tool":
            call = {"index": 0, "id": f"call_{now}_{a}", "type": "function",
                    "function": {"name": a, "arguments": json.dumps(b)}}
            msg, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
            log("->", a, json.dumps(b)[:120])
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
