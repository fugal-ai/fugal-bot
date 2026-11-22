"""TEST ONLY: a deterministic OpenAI-compatible "brain" that emits tool calls.

The local test models (Ollama gemma3:4b, deepseek-r1:8b) cannot make tool
calls, and no paid model may be used today. This stub lets scripts/smoke.sh
drive the agent's REAL tool loop (OpenClaw's own exec / write / browser tools,
running on the bot's own computer) without any paid call. It is not part of
the bot; it runs only under scripts/smoke.compose.yaml.

Script, counted by tool results since the last user message:
  0 -> exec:    run one shell command that creates ~/workspace/smoke-file.txt
  1 -> browser: open https://example.com in the bot's (visible) browser
  2 -> final text summarising both tool results.
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STAMP_CMD = (
    "echo \"created by the bot's shell at $(date -u +%FT%TZ) on $(hostname)\" "
    "> /home/node/workspace/smoke-file.txt && uname -sr && id -un && "
    "ls -l /home/node/workspace/smoke-file.txt && cat /home/node/workspace/smoke-file.txt"
)


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
    last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1)
    results = [m for m in msgs[last_user + 1:] if m.get("role") == "tool"]
    step = len(results)
    log(f"step={step} tools_offered={len(tools)} has_exec={'exec' in tools} has_browser={'browser' in tools}")
    if step == 0 and "exec" in tools:
        return ("tool", "exec", {"command": STAMP_CMD})
    if step <= 1 and "browser" in tools and not any("example.com" in text_of(m.get("content")) for m in results):
        return ("tool", "browser", {"action": "open", "targetUrl": "https://example.com"})
    summary = " | ".join(text_of(m.get("content"))[:300].replace("\n", " ") for m in results)
    return ("text", f"SCRIPTED-BRAIN DONE. Tool results: {summary}", None)


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

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": [{"id": "tools", "object": "model"}]})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        kind, a, b = plan(req)
        cid, now = f"chatcmpl-{int(time.time()*1000)}", int(time.time())
        if kind == "tool":
            call = {"index": 0, "id": f"call_{now}_{a}", "type": "function",
                    "function": {"name": a, "arguments": json.dumps(b)}}
            msg, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
            log("->", a, json.dumps(b)[:120])
        else:
            msg, finish = {"role": "assistant", "content": a}, "stop"
            log("-> final text")
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
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
        ev({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    log("scripted brain on :11500 (test only)")
    ThreadingHTTPServer(("0.0.0.0", 11500), H).serve_forever()
