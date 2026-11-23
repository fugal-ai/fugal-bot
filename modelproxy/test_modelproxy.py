"""Unit tests for the model proxy (stdlib only). No network beyond 127.0.0.1:
a fake OpenAI-compatible upstream runs in this process.

    python3 -I modelproxy/test_modelproxy.py
"""

import base64
import hashlib
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True   # no __pycache__ beside the program
_spec = importlib.util.spec_from_file_location(
    "modelproxy", os.path.join(os.path.dirname(os.path.abspath(__file__)), "modelproxy.py"))
mp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mp)

REAL_KEY = "sk-REAL-upstream-key-0123456789"
PLACEHOLDER = "held-by-modelproxy"
BODY_CANARY = "the-user-said-something-private-7731"
REPLY_CANARY = "the-model-answered-something-private-9942"
USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}

LOGS = []
mp.log = LOGS.append


NOT_FINAL = ("asking again", "malformed answer", "passed on", "the answer stalled", "not waiting")


def wait_logs(n):
    """The proxy counts a call after the last byte has gone out; wait for its
    log line so a test never looks (or moves the clock) too early. Lines about
    a broken answer come before that line and are not counted."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if sum(1 for line in list(LOGS) if not any(m in line for m in NOT_FINAL)) >= n:
            return
        time.sleep(0.002)


def serve(server):
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()


def sse(*objs, done=True):
    out = b"".join(b"data: " + json.dumps(o).encode() + b"\n\n" for o in objs)
    return out + (b"data: [DONE]\n\n" if done else b"")


def chunk(delta, finish=None, **extra):
    return {"id": "c1", "object": "chat.completion.chunk", "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}


USAGE_CHUNK = {"id": "c1", "object": "chat.completion.chunk", "model": "m", "choices": [], "usage": USAGE}


class Upstream(BaseHTTPRequestHandler):
    """Records what it receives; `reply` (set per test) decides the answer."""
    protocol_version = "HTTP/1.1"
    seen = []
    reply = None

    def log_message(self, *a):
        pass

    def _handle(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(n) if n else b""
        req = {"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
               "body": body}
        type(self).seen.append(req)
        type(self).reply(self, req)

    do_GET = do_POST = do_DELETE = _handle

    def send_bytes(self, code, body, ctype="application/json", extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def start_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True


def completion(text=REPLY_CANARY, usage=USAGE):
    obj = {"id": "c1", "object": "chat.completion", "model": "m",
           "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}
    if usage is not None:
        obj["usage"] = usage
    return json.dumps(obj).encode()


class Base(unittest.TestCase):
    env = {}

    def setUp(self):
        LOGS.clear()
        Upstream.seen = []
        Upstream.reply = lambda h, req: h.send_bytes(200, completion())
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.upstream.daemon_threads = True
        serve(self.upstream)
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [1795305600.0]  # 2026-11-22 00:00:00 UTC
        self.proxy = None
        self.start(self.env)

    def start(self, env):
        if self.proxy:
            self.proxy.shutdown()
            self.proxy.server_close()
        full = {"MODEL_BASE_URL": f"http://127.0.0.1:{self.upstream.server_address[1]}/v1",
                "MODEL_API_KEY": REAL_KEY, "MODELPROXY_LISTEN_HOST": "127.0.0.1", "MODELPROXY_LISTEN_PORT": "0",
                "MODELPROXY_DATA_DIR": self.tmp.name, **env}
        self.cfg = mp.Config(full)
        self.cfg.listen_port = 0
        self.slept = []
        self.cfg.sleep = self.slept.append   # retry waits are recorded, not slept
        self.meter = mp.Meter(self.cfg, clock=lambda: self.now[0])
        self.proxy = mp.make_server(self.cfg, self.meter)
        self.port = self.proxy.server_address[1]
        serve(self.proxy)

    secrets = (REAL_KEY, BODY_CANARY, REPLY_CANARY, PLACEHOLDER)

    def tearDown(self):
        for s in (self.proxy, self.upstream):
            s.shutdown()
            s.server_close()
        self.tmp.cleanup()
        joined = "\n".join(LOGS)
        for secret in self.secrets:
            self.assertNotIn(secret, joined, "a secret or a body reached the log")

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Authorization": f"Bearer {PLACEHOLDER}", **(headers or {})}
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        logged = sum(1 for line in list(LOGS) if not any(m in line for m in NOT_FINAL))
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        if method == "POST" and 200 <= resp.status < 300:
            wait_logs(logged + 1)
        return resp, data

    def chat(self, **extra):
        return self.call("POST", "/v1/chat/completions",
                         {"model": "m", "messages": [{"role": "user", "content": BODY_CANARY}], **extra})

    def usage(self):
        return json.loads(self.call("GET", "/usage")[1])


class PassThrough(Base):
    def test_key_is_replaced_and_body_untouched(self):
        raw = json.dumps({"model": "m", "messages": [{"role": "user", "content": BODY_CANARY}],
                          "tools": [{"type": "function", "function": {"name": "exec"}}]}, indent=3).encode()
        resp, data = self.call("POST", "/v1/chat/completions", raw,
                               {"Content-Type": "application/json", "X-Api-Key": PLACEHOLDER, "Cookie": "a=b"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(data, completion())
        seen = Upstream.seen[0]
        self.assertEqual(seen["path"], "/v1/chat/completions")
        self.assertEqual(seen["headers"]["authorization"], f"Bearer {REAL_KEY}")
        self.assertEqual(seen["body"], raw)  # non-streamed requests go through byte for byte
        self.assertNotIn(PLACEHOLDER, json.dumps(seen["headers"]))
        self.assertNotIn("x-api-key", seen["headers"])
        self.assertNotIn("cookie", seen["headers"])
        self.assertEqual(seen["headers"]["accept-encoding"], "identity")

    def test_no_key_configured_sends_no_authorization(self):
        self.start({"MODEL_API_KEY": ""})
        self.chat()
        self.assertNotIn("authorization", Upstream.seen[0]["headers"])

    def test_key_never_appears_in_any_reply(self):
        for method, path in (("GET", "/usage"), ("GET", "/healthz"), ("GET", "/v1/nope"), ("GET", "/")):
            resp, data = self.call(method, path)
            self.assertNotIn(REAL_KEY.encode(), data)
            self.assertNotIn(REAL_KEY, str(resp.getheaders()))

    def test_models_is_forwarded_and_not_metered(self):
        Upstream.reply = lambda h, req: h.send_bytes(200, b'{"object":"list","data":[{"id":"org/model-a"}]}')
        resp, data = self.call("GET", "/v1/models")
        self.assertEqual((resp.status, json.loads(data)["data"][0]["id"]), (200, "org/model-a"))
        self.call("GET", "/v1/models/org/model-a?x=1")
        self.assertEqual([s["path"] for s in Upstream.seen], ["/v1/models", "/v1/models/org/model-a?x=1"])
        self.assertEqual(Upstream.seen[0]["headers"]["authorization"], f"Bearer {REAL_KEY}")
        self.assertEqual(self.usage()["total"]["requests"], 0)

    def test_only_inference_endpoints_are_forwarded(self):
        for method, path in (("POST", "/v1/files"), ("GET", "/v1/api-keys"), ("POST", "/v1/../admin"),
                             ("GET", "/v1/models/../../users/me"), ("GET", "/v1/models/%2e%2e/x"),
                             ("POST", "/chat/completions"), ("GET", "/v1/chat/completions"),
                             ("POST", "/v1/models"), ("POST", "/v2/chat/completions"), ("GET", "/v1//models")):
            resp, data = self.call(method, path, {} if method == "POST" else None)
            self.assertEqual(resp.status, 404, path)
            self.assertEqual(json.loads(data)["error"]["type"], "invalid_request_error")
        resp, _ = self.call("DELETE", "/v1/models/m")
        self.assertEqual(resp.status, 501)
        self.assertEqual(Upstream.seen, [])

    def test_upstream_error_passes_through_and_costs_nothing(self):
        err = b'{"error":{"message":"bad key","type":"invalid_request_error","code":"invalid_api_key"}}'
        Upstream.reply = lambda h, req: h.send_bytes(401, err, extra=[("X-Request-Id", "r1")])
        resp, data = self.chat()
        self.assertEqual((resp.status, data, resp.getheader("X-Request-Id")), (401, err, "r1"))
        self.assertEqual(self.usage()["total"]["requests"], 0)

    def test_upstream_down_gives_an_openai_style_502(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        resp, data = self.chat()
        self.assertEqual(resp.status, 502)
        self.assertEqual(json.loads(data)["error"]["type"], "upstream_error")
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)  # for tearDown
        serve(self.upstream)

    def test_redirects_are_not_followed(self):
        Upstream.reply = lambda h, req: h.send_bytes(307, b"", extra=[("Location", "http://127.0.0.1:1/steal")])
        resp, _ = self.chat()
        self.assertEqual(resp.status, 307)
        self.assertEqual(len(Upstream.seen), 1)

    def test_oversized_body_is_refused(self):
        self.start({"MODELPROXY_MAX_BODY_MB": "0.001"})
        resp, data = self.chat(pad="x" * 5000)
        self.assertEqual(resp.status, 413)
        self.assertEqual(Upstream.seen, [])

    def test_chunked_request_body(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps({"model": "m", "messages": []}).encode()
        conn.request("POST", "/v1/chat/completions", body=iter([payload[:9], payload[9:]]),
                     headers={"Content-Type": "application/json"}, encode_chunked=True)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(Upstream.seen[0]["body"], payload)

    def test_keep_alive_serves_several_requests_on_one_connection(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        for _ in range(3):
            conn.request("POST", "/v1/chat/completions", body=b'{"model":"m","messages":[]}')
            resp = conn.getresponse()
            self.assertEqual((resp.status, resp.read()), (200, completion()))
        conn.close()
        wait_logs(3)
        self.assertEqual(self.usage()["total"]["requests"], 3)


class Streaming(Base):
    def stream_reply(self, with_usage_when_asked=True, events=None):
        events = events or [chunk({"role": "assistant", "content": "Hel"}), chunk({"content": "lo"}), chunk({}, "stop")]

        def reply(h, req):
            asked = (json.loads(req["body"]).get("stream_options") or {}).get("include_usage") is True
            h.start_stream()
            h.wfile.write(sse(*events, done=False))
            if asked and with_usage_when_asked:
                h.wfile.write(sse(USAGE_CHUNK, done=False))
            h.wfile.write(b"data: [DONE]\n\n")
        Upstream.reply = reply
        return events

    def test_usage_is_requested_for_the_client_and_hidden_from_it(self):
        events = self.stream_reply()
        resp, data = self.chat(stream=True)
        self.assertEqual(resp.status, 200)
        self.assertIn("text/event-stream", resp.getheader("Content-Type"))
        sent = json.loads(Upstream.seen[0]["body"])
        self.assertEqual(sent["stream_options"], {"include_usage": True})
        self.assertEqual(sent["messages"][0]["content"], BODY_CANARY)
        self.assertEqual(data, sse(*events))  # exactly the stream a client without the option expects
        self.assertEqual(self.usage()["total"],
                         {**mp._zero(), "requests": 1, "prompt_tokens": 11, "completion_tokens": 7,
                          "total_tokens": 18})

    def test_include_usage_false_is_also_overridden_and_hidden(self):
        events = self.stream_reply()
        _, data = self.chat(stream=True, stream_options={"include_usage": False, "other": 1})
        self.assertEqual(json.loads(Upstream.seen[0]["body"])["stream_options"], {"include_usage": True, "other": 1})
        self.assertEqual(data, sse(*events))
        self.assertEqual(self.usage()["total"]["total_tokens"], 18)

    def test_client_that_asked_for_usage_gets_the_stream_unchanged(self):
        events = self.stream_reply()
        raw = json.dumps({"model": "m", "stream": True, "stream_options": {"include_usage": True},
                          "messages": [{"role": "user", "content": BODY_CANARY}]}, indent=2).encode()
        _, data = self.call("POST", "/v1/chat/completions", raw)
        self.assertEqual(Upstream.seen[0]["body"], raw)
        self.assertEqual(data, sse(*events, USAGE_CHUNK))
        self.assertEqual(self.usage()["total"]["total_tokens"], 18)

    def test_injection_can_be_switched_off(self):
        self.start({"MODELPROXY_STREAM_USAGE": "false"})
        events = self.stream_reply()
        _, data = self.chat(stream=True)
        self.assertNotIn("stream_options", json.loads(Upstream.seen[0]["body"]))
        self.assertEqual(data, sse(*events))
        total = self.usage()["total"]
        self.assertEqual((total["requests"], total["estimated_requests"], total["completion_tokens"]), (1, 1, 3))

    def test_tool_call_stream_passes_through(self):
        events = self.stream_reply(events=[
            chunk({"role": "assistant", "content": None, "tool_calls": [
                {"index": 0, "id": "call_1", "type": "function", "function": {"name": "exec", "arguments": ""}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{\"command\": \"echo \\\"usage\\\"\"}"}}]}),
            chunk({}, "tool_calls")])
        _, data = self.chat(stream=True, tools=[{"type": "function", "function": {"name": "exec"}}])
        self.assertEqual(data, sse(*events))
        self.assertEqual(self.usage()["total"]["total_tokens"], 18)

    def test_usage_on_the_last_content_chunk_is_counted_and_kept(self):
        events = self.stream_reply(with_usage_when_asked=False,
                                   events=[chunk({"content": "hi"}), chunk({}, "stop", usage=USAGE)])
        _, data = self.chat(stream=True)
        self.assertEqual(data, sse(*events))
        self.assertEqual(self.usage()["total"]["total_tokens"], 18)

    def test_crlf_events(self):
        def reply(h, req):
            h.start_stream()
            body = sse(chunk({"content": "hi"}), USAGE_CHUNK).replace(b"\n", b"\r\n")
            for i in range(0, len(body), 7):  # arbitrary splits, also inside the separators
                h.wfile.write(body[i:i + 7])
        Upstream.reply = reply
        _, data = self.chat(stream=True)
        self.assertEqual(data, sse(chunk({"content": "hi"})).replace(b"\n", b"\r\n"))
        self.assertEqual(self.usage()["total"]["total_tokens"], 18)

    def _first_event_arrives_before_the_upstream_finishes(self, headers=None, **extra):
        go, state = threading.Event(), {}

        def reply(h, req):
            h.start_stream()
            h.wfile.write(sse(chunk({"content": "first"}), done=False))
            h.wfile.flush()
            state["released_by_client"] = go.wait(5)
            h.wfile.write(sse(chunk({"content": "second"}), USAGE_CHUNK))
        Upstream.reply = reply
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/v1/chat/completions", headers=headers or {},
                     body=json.dumps({"model": "m", "stream": True, "messages": [], **extra}).encode())
        resp = conn.getresponse()
        line = resp.readline()
        self.assertIn(b'"first"', line)
        self.assertFalse(go.is_set())
        go.set()  # only now may the upstream send the rest
        rest = resp.read()
        conn.close()
        self.assertIn(b'"second"', rest)
        self.assertTrue(state["released_by_client"], "the proxy held the first event back")

    def test_not_buffered_when_the_proxy_adds_the_usage_option(self):
        self._first_event_arrives_before_the_upstream_finishes()

    def test_not_buffered_when_the_client_asked_for_usage(self):
        self._first_event_arrives_before_the_upstream_finishes(stream_options={"include_usage": True})

    def test_http_1_0_client_gets_an_unchunked_stream(self):
        import socket
        events = self.stream_reply()
        body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(b"POST /v1/chat/completions HTTP/1.0\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        raw = b""
        while True:
            part = s.recv(65536)
            if not part:
                break
            raw += part
        s.close()
        head, _, payload = raw.partition(b"\r\n\r\n")
        self.assertNotIn(b"chunked", head.lower())
        self.assertEqual(payload, sse(*events))


class OtherApis(Base):
    def test_responses_api_json_and_stream(self):
        done = {"type": "response.completed", "response": {"id": "r1", "usage": {
            "input_tokens": 30, "output_tokens": 12, "total_tokens": 42}}}

        def reply(h, req):
            if json.loads(req["body"]).get("stream"):
                h.start_stream()
                h.wfile.write(b"event: response.output_text.delta\n" + sse({"type": "response.output_text.delta",
                                                                           "delta": "hi"}, done=False))
                h.wfile.write(b"event: response.completed\n" + sse(done, done=False))
            else:
                h.send_bytes(200, json.dumps({"id": "r1", "object": "response", "output": [],
                                              "usage": {"input_tokens": 5, "output_tokens": 2}}).encode())
        Upstream.reply = reply
        resp, _ = self.call("POST", "/v1/responses", {"model": "m", "input": "hi"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.usage()["total"]["total_tokens"], 7)
        resp, data = self.call("POST", "/v1/responses", {"model": "m", "input": "hi", "stream": True})
        self.assertIn(b"response.completed", data)
        self.assertNotIn("stream_options", json.loads(Upstream.seen[1]["body"]))  # not a Responses API field
        total = self.usage()["total"]
        self.assertEqual((total["prompt_tokens"], total["completion_tokens"], total["estimated_requests"]), (35, 14, 0))

    def test_embeddings(self):
        Upstream.reply = lambda h, req: h.send_bytes(200, json.dumps({
            "object": "list", "data": [{"embedding": [0.1]}], "usage": {"prompt_tokens": 9, "total_tokens": 9}}).encode())
        resp, _ = self.call("POST", "/v1/embeddings", {"model": "e", "input": "hi"})
        self.assertEqual(resp.status, 200)
        total = self.usage()["total"]
        self.assertEqual((total["prompt_tokens"], total["completion_tokens"]), (9, 0))

    def test_reply_without_usage_is_estimated_and_flagged(self):
        Upstream.reply = lambda h, req: h.send_bytes(200, completion(usage=None))
        self.chat()
        total = self.usage()["total"]
        self.assertEqual((total["requests"], total["estimated_requests"]), (1, 1))
        self.assertGreater(total["prompt_tokens"], 0)
        self.assertGreater(total["completion_tokens"], 0)


class Metering(Base):
    def test_counters_survive_a_restart_and_roll_over_by_day(self):
        self.chat()
        self.chat()
        self.now[0] += 86400
        self.chat()
        self.start({})  # a new process reading the same file
        u = self.usage()
        self.assertEqual(u["today"], "2026-11-23")
        self.assertEqual((u["day"]["requests"], u["day"]["total_tokens"]), (1, 18))
        self.assertEqual((u["total"]["requests"], u["total"]["total_tokens"]), (3, 54))
        self.assertEqual(u["days"]["2026-11-22"]["total_tokens"], 36)
        with open(os.path.join(self.tmp.name, "usage.json"), encoding="utf-8") as f:
            self.assertNotIn(REAL_KEY, f.read())
        self.assertEqual(sorted(os.listdir(self.tmp.name)),
                         ["requests-2026-11-22.jsonl", "requests-2026-11-23.jsonl", "usage.json"])

    def test_unreadable_usage_file_is_kept_aside(self):
        with open(os.path.join(self.tmp.name, "usage.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.start({})
        self.assertEqual(self.usage()["total"]["requests"], 0)
        self.assertTrue(any(n.startswith("usage.json.unreadable-") for n in os.listdir(self.tmp.name)))

    def assertRefused(self, resp, data, word):
        self.assertEqual(resp.status, 400)   # the default MODELPROXY_EXHAUSTED_REPLY; the others: ExhaustedReply
        err = json.loads(data)["error"]
        self.assertEqual((err["type"], err["code"]), ("invalid_request_error", "allowance_exhausted"))
        self.assertEqual(err["message"], word)
        self.assertNotRegex(err["message"], r"[^A-Za-z ]")   # no punctuation: the web chat would escape it

    def test_daily_token_cap(self):
        self.start({"BUDGET_TOKENS_PER_DAY": "30"})
        self.assertEqual(self.chat()[0].status, 200)       # 18 used
        self.assertEqual(self.chat()[0].status, 200)       # 36: the request in flight may overshoot
        resp, data = self.chat(stream=True)
        self.assertRefused(resp, data, mp.USED_UP_TODAY)
        self.assertEqual(len(Upstream.seen), 2)            # the refused request never left
        u = self.usage()
        self.assertEqual((u["exceeded"], u["remaining"]["tokens_per_day"], u["rejected_since_start"]["budget"]),
                         ("tokens_per_day", 0, 1))
        self.now[0] += 86400                               # next UTC day
        self.assertEqual(self.chat()[0].status, 200)
        self.assertEqual(self.call("GET", "/v1/models")[0].status, 200)

    def test_total_token_cap_survives_restart_and_days(self):
        self.start({"BUDGET_TOKENS_TOTAL": "10"})
        self.assertEqual(self.chat()[0].status, 200)
        self.now[0] += 5 * 86400
        self.start({"BUDGET_TOKENS_TOTAL": "10"})
        resp, data = self.chat()
        self.assertRefused(resp, data, mp.USED_UP_TOTAL)
        self.start({"BUDGET_TOKENS_TOTAL": "1000"})        # the owner raises the limit
        self.assertEqual(self.chat()[0].status, 200)
        self.assertEqual(self.usage()["remaining"]["tokens_total"], 1000 - 36)

    def test_usd_caps_from_the_price_table(self):
        # 11 prompt + 7 completion tokens at 100 / 200 USD per million = 0.0025 USD per call
        self.start({"BUDGET_USD_PER_DAY": "0.004", "BUDGET_PRICES": '{"m": [100, 200], "*": [1, 1]}'})
        self.assertEqual(self.chat()[0].status, 200)
        self.assertAlmostEqual(self.usage()["total"]["usd"], 0.0025)
        self.assertEqual(self.chat()[0].status, 200)
        resp, data = self.chat()
        self.assertRefused(resp, data, mp.USED_UP_TODAY)
        self.start({"BUDGET_USD_TOTAL": "0.006", "BUDGET_PRICES": '{"other": [100, 200]}'})
        self.assertEqual(self.chat()[0].status, 200)       # unlisted model: billed at the dearest row
        self.assertAlmostEqual(self.usage()["total"]["usd"], 0.0075)
        self.assertRefused(*self.chat(), mp.USED_UP_TOTAL)

    def test_requests_per_minute(self):
        self.start({"BUDGET_REQUESTS_PER_MINUTE": "2"})
        self.assertEqual(self.chat()[0].status, 200)
        self.now[0] += 10
        self.assertEqual(self.chat()[0].status, 200)
        resp, data = self.chat()
        self.assertEqual(resp.status, 429)
        self.assertEqual(json.loads(data)["error"]["code"], "rate_limit_exceeded")
        self.assertEqual(resp.getheader("Retry-After"), "50")
        self.now[0] += 51
        self.assertEqual(self.chat()[0].status, 200)

    def test_bad_configuration_is_refused(self):
        base = {"MODEL_BASE_URL": "https://example.invalid/v1"}
        for env in ({}, {"MODEL_BASE_URL": "ftp://x"}, {"MODEL_BASE_URL": "https://user:pw@example.invalid/v1"},
                    {**base, "BUDGET_TOKENS_PER_DAY": "lots"}, {**base, "BUDGET_TOKENS_TOTAL": "-1"},
                    {**base, "BUDGET_USD_TOTAL": "5"}, {**base, "BUDGET_PRICES": "[1,2]"},
                    {**base, "BUDGET_PRICES": '{"m": [1]}'}, {**base, "MODEL_API_KEY": "two words"}):
            with self.assertRaises(mp.ConfigError, msg=str(env)):
                mp.Config(env)
        self.assertEqual(mp.Config({**base, "BUDGET_TOKENS_PER_DAY": "0", "BUDGET_USD_TOTAL": ""}).limits(),
                         {"tokens_per_day": None, "tokens_total": None, "usd_per_day": None, "usd_total": None,
                          "requests_per_minute": None})



def with_cache(cached, style="openai", prompt=1000, completion=100):
    """A `usage` object in one of the shapes upstreams use for cached prompt tokens."""
    u = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
    if style == "openai":
        u["prompt_tokens_details"] = {"cached_tokens": cached}
    elif style == "responses":
        u = {"input_tokens": prompt, "output_tokens": completion,
             "input_tokens_details": {"cached_tokens": cached}}
    elif style == "deepseek":
        u["prompt_cache_hit_tokens"] = cached
        u["prompt_cache_miss_tokens"] = prompt - cached
    elif style == "anthropic":
        u["cache_read_input_tokens"] = cached
    return u


class CachedTokens(Base):
    """Cached prompt tokens: read from `usage` in the shapes upstreams use, counted on their
    own, and billed at the third price of the model's row (input price when there is none)."""
    # per million: 1000 prompt (800 cached) + 100 completion at 100 in / 200 out / 10 cached
    #   = (200 * 100 + 800 * 10 + 100 * 200) / 1e6 = 0.048 USD; without the cache price 0.12 USD
    PRICES = '{"m": [100, 200, 10], "two": [100, 200], "*": [1, 1]}'

    def reply_with(self, usage):
        Upstream.reply = lambda h, req: h.send_bytes(200, completion(usage=usage))

    def test_usage_tokens_reads_every_style(self):
        for style in ("openai", "responses", "deepseek", "anthropic"):
            self.assertEqual(mp.usage_tokens({"usage": with_cache(800, style)}), (1000, 100, 800), style)
        self.assertEqual(mp.usage_tokens({"usage": {**with_cache(0, None), "cached_tokens": 5}}), (1000, 100, 5))
        self.assertEqual(mp.usage_tokens({"response": {"usage": with_cache(300, "responses")}}), (1000, 100, 300))
        self.assertEqual(mp.usage_tokens({"usage": USAGE}), (11, 7, 0))                  # nothing reported
        self.assertEqual(mp.usage_tokens({"usage": {"total_tokens": 9}}), (9, 0, 0))
        for bad in (-4, "800", 2.5, True, None, {}):                                     # unusable: 0
            u = with_cache(0, None)
            u["prompt_tokens_details"] = {"cached_tokens": bad}
            self.assertEqual(mp.usage_tokens({"usage": u}), (1000, 100, 0), bad)
        u = with_cache(0, None)
        u["prompt_tokens_details"] = None
        self.assertEqual(mp.usage_tokens({"usage": u}), (1000, 100, 0))
        self.assertEqual(mp.usage_tokens({"usage": with_cache(5000, "openai")}), (1000, 100, 1000))  # capped

    def test_cost_function(self):
        c = mp.cost_usd
        self.assertAlmostEqual(c((100, 200, 10), 1000, 100, 800), 0.048)
        self.assertAlmostEqual(c((100, 200, 100), 1000, 100, 800), 0.12)
        self.assertAlmostEqual(c((100, 200, 10), 1000, 100, 1000), 0.03)
        self.assertAlmostEqual(c((100, 200, 10), 1000, 100, 9999), 0.03)    # more cached than prompt: all of it
        self.assertAlmostEqual(c((100, 200, 10), 1000, 100, -5), 0.12)      # never a negative cached count
        self.assertEqual(c((100, 200, 10), 0, 0, 50), 0.0)
        self.assertGreaterEqual(c((100, 200, 10), -5, -5, 3), 0.0)
        self.assertEqual(mp.price_of({"m": (1.0, 2.0)}, "m"), (1.0, 2.0, 1.0))      # no third value: input price
        self.assertEqual(mp.price_of({"m": (1.0, 2.0, 0.1)}, "m"), (1.0, 2.0, 0.1))
        self.assertEqual(mp.price_of({"a": (1.0, 2.0), "b": (5.0, 5.0, 0.5)}, "x"), (5.0, 5.0, 0.5))
        self.assertEqual(mp.price_of({}, "m"), (0.0, 0.0, 0.0))

    def test_price_rows_with_two_or_three_values(self):
        self.assertEqual(mp.price_table({"a": [1, 2], "b": [1, 2, 0.5]}), {"a": (1.0, 2.0), "b": (1.0, 2.0, 0.5)})
        for bad in ({"a": [1]}, {"a": [1, 2, 3, 4]}, {"a": [1, 2, -1]}, {"a": [1, 2, "x"]}, {"a": [1, 2, None]},
                    {"a": [1, 2, True]}, {"a": [1, 2, float("inf")]}):
            self.assertIsNone(mp.price_table(bad), bad)
        with self.assertRaises(mp.ConfigError):
            mp.Config({"MODEL_BASE_URL": "http://x/v1", "BUDGET_PRICES": '{"m": [1, 2, 3, 4]}',
                       "MODELPROXY_DATA_DIR": self.tmp.name})

    def test_room_json_reply_is_billed_at_the_cached_price(self):
        self.start({"BUDGET_USD_PER_DAY": "10", "BUDGET_PRICES": self.PRICES})
        self.reply_with(with_cache(800))
        self.assertEqual(self.chat()[0].status, 200)
        u = self.usage()
        for row in (u["total"], u["day"], u["days"]["2026-11-22"]):
            self.assertEqual((row["prompt_tokens"], row["cached_prompt_tokens"], row["completion_tokens"]),
                             (1000, 800, 100))
            self.assertAlmostEqual(row["usd"], 0.048)
        self.assertTrue(any("cached=800" in line for line in LOGS))

    def test_a_two_value_row_bills_cached_tokens_at_the_input_price(self):
        self.start({"BUDGET_USD_PER_DAY": "10", "BUDGET_PRICES": self.PRICES})
        self.reply_with(with_cache(800))
        self.chat(model="two")           # the fake upstream ignores the model; the price comes from the request
        u = self.usage()
        self.assertEqual(u["total"]["cached_prompt_tokens"], 800)        # still counted
        self.assertAlmostEqual(u["total"]["usd"], 0.12)

    def test_room_stream_counts_cached_tokens(self):
        self.start({"BUDGET_USD_PER_DAY": "10", "BUDGET_PRICES": self.PRICES})

        def reply(h, req):
            h.start_stream()
            h.wfile.write(sse(chunk({"content": "hi"}), chunk({}, "stop"), done=False))
            h.wfile.write(sse({"id": "c1", "object": "chat.completion.chunk", "choices": [],
                               "usage": with_cache(800)}))
        Upstream.reply = reply
        _, data = self.chat(stream=True)
        self.assertEqual(data, sse(chunk({"content": "hi"}), chunk({}, "stop")))   # usage chunk still hidden
        t = self.usage()["total"]
        self.assertEqual((t["cached_prompt_tokens"], t["prompt_tokens"]), (800, 1000))
        self.assertAlmostEqual(t["usd"], 0.048)

    def test_cached_tokens_stretch_the_usd_allowance(self):
        self.start({"BUDGET_USD_PER_DAY": "0.1", "BUDGET_PRICES": self.PRICES})
        self.reply_with(with_cache(800))
        for _ in range(3):                       # 0.048 each: the third is still let in (0.096 < 0.1)
            self.assertEqual(self.chat()[0].status, 200)
        resp, data = self.chat()
        self.assertEqual((resp.status, json.loads(data)["error"]["code"]), (400, "allowance_exhausted"))

    def test_no_cache_info_bills_in_full_and_counts_zero(self):
        self.start({"BUDGET_USD_PER_DAY": "10", "BUDGET_PRICES": self.PRICES})
        self.reply_with(with_cache(0, None))
        self.chat()
        t = self.usage()["total"]
        self.assertEqual(t["cached_prompt_tokens"], 0)
        self.assertAlmostEqual(t["usd"], 0.12)

    def test_cached_above_prompt_is_treated_as_the_whole_prompt(self):
        self.start({"BUDGET_USD_PER_DAY": "10", "BUDGET_PRICES": self.PRICES})
        self.reply_with(with_cache(5000))
        self.chat()
        t = self.usage()["total"]
        self.assertEqual(t["cached_prompt_tokens"], 1000)
        self.assertAlmostEqual(t["usd"], 0.03)

    def test_an_old_usage_file_without_the_counter_still_loads(self):
        old = {k: v for k, v in mp._zero().items() if k != "cached_prompt_tokens"}
        old.update(requests=3, prompt_tokens=30, total_tokens=30, usd=0.5)
        with open(os.path.join(self.tmp.name, "usage.json"), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "total": old, "days": {"2026-11-22": old}}, f)
        self.start({})
        u = self.usage()
        self.assertEqual((u["total"]["requests"], u["total"]["cached_prompt_tokens"], u["total"]["usd"]), (3, 0, 0.5))
        self.assertEqual(u["day"]["cached_prompt_tokens"], 0)
        self.reply_with(with_cache(800))
        self.chat()
        self.assertEqual(self.usage()["total"]["cached_prompt_tokens"], 800)


class ExhaustedReply(Base):
    """MODELPROXY_EXHAUSTED_REPLY: the form of the answer once an allowance is used up."""

    def spend(self, mode):
        if os.path.exists(os.path.join(self.tmp.name, "usage.json")):
            os.remove(os.path.join(self.tmp.name, "usage.json"))
        Upstream.seen = []
        self.start({"BUDGET_TOKENS_TOTAL": "10", "MODELPROXY_EXHAUSTED_REPLY": mode})
        self.assertEqual(self.chat()[0].status, 200)   # 18 tokens: over the cap from now on

    def test_default_is_a_400_that_names_the_reason(self):
        self.assertEqual(mp.DEFAULT_EXHAUSTED_REPLY, "400")
        self.start({"BUDGET_TOKENS_TOTAL": "10"})
        self.chat()
        resp, data = self.chat()
        err = json.loads(data)["error"]
        self.assertEqual((resp.status, err["type"], err["code"]), (400, "invalid_request_error", "allowance_exhausted"))
        self.assertEqual(err["message"], mp.USED_UP_TOTAL)

    def test_429_and_402(self):
        for mode, status in (("429", 429), ("402", 402)):
            self.spend(mode)
            resp, data = self.chat()
            err = json.loads(data)["error"]
            self.assertEqual((resp.status, err["type"], err["code"]), (status, "insufficient_quota", "insufficient_quota"))
            self.assertIsNone(resp.getheader("Retry-After"))

    def test_200_is_an_assistant_message_and_costs_nothing(self):
        self.spend("200")
        before = self.usage()["total"]
        resp, data = self.chat()
        reply = json.loads(data)
        self.assertEqual(resp.status, 200)
        self.assertEqual(reply["object"], "chat.completion")
        self.assertEqual(reply["choices"][0]["finish_reason"], "stop")
        self.assertEqual(reply["choices"][0]["message"]["content"], mp.USED_UP_TOTAL)
        self.assertEqual(reply["usage"]["total_tokens"], 0)
        self.assertEqual(self.usage()["total"], before)
        self.assertEqual(len(Upstream.seen), 1)            # only the call before the cap went upstream

    def test_200_as_a_stream(self):
        self.spend("200")
        resp, data = self.chat(stream=True)
        self.assertIn("text/event-stream", resp.getheader("Content-Type"))
        events = [json.loads(line[6:]) for line in data.decode().split("\n\n") if line.startswith("data: {")]
        self.assertTrue(data.endswith(b"data: [DONE]\n\n"))
        self.assertEqual(events[0]["choices"][0]["delta"]["content"], mp.USED_UP_TOTAL)
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "stop")   # no usage chunk: not asked for
        _, data = self.chat(stream=True, stream_options={"include_usage": True})
        self.assertIn(b'"choices": [], "usage"', data)

    def test_200_falls_back_to_the_400_on_other_endpoints(self):
        self.spend("200")
        resp, data = self.call("POST", "/v1/embeddings", {"model": "e", "input": "hi"})
        self.assertEqual((resp.status, json.loads(data)["error"]["code"]), (400, "allowance_exhausted"))

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(mp.ConfigError):
            mp.Config({"MODEL_BASE_URL": "https://example.invalid/v1", "MODELPROXY_EXHAUSTED_REPLY": "teapot"})


class UpstreamRetry(Base):
    """Upstream 429 / 503 are asked again before anything went to the client."""

    def busy_then_ok(self, times, status=429, headers=(), stream=False):
        def reply(h, req):
            if len(Upstream.seen) <= times:
                return h.send_bytes(status, b'{"error":{"message":"slow down","type":"rate_limit_error"}}', extra=headers)
            if stream:
                h.start_stream()
                return h.wfile.write(sse(chunk({"content": "hi"}), USAGE_CHUNK))
            h.send_bytes(200, completion())
        Upstream.reply = reply

    def test_room_mode_passes_a_429_through_unless_asked_to_retry(self):
        self.busy_then_ok(1, headers=[("Retry-After", "7")])
        resp, data = self.chat()
        self.assertEqual((resp.status, resp.getheader("Retry-After")), (429, "7"))
        self.assertIn(b"slow down", data)
        self.assertEqual((len(Upstream.seen), self.slept), (1, []))

    def test_retry_after_seconds_is_honoured(self):
        self.start({"MODELPROXY_UPSTREAM_RETRIES": "3"})
        self.busy_then_ok(2, headers=[("Retry-After", "2")])
        resp, data = self.chat()
        self.assertEqual((resp.status, data), (200, completion()))
        self.assertEqual((len(Upstream.seen), self.slept), (3, [2.0, 2.0]))
        self.assertEqual(self.usage()["total"]["requests"], 1)
        self.assertIn("after 2 retries", LOGS[-1])

    def test_503_retry_after_ms_and_http_date(self):
        self.start({"MODELPROXY_UPSTREAM_RETRIES": "3"})
        self.busy_then_ok(1, status=503, headers=[("retry-after-ms", "250")])
        self.assertEqual(self.chat()[0].status, 200)
        self.assertEqual(self.slept, [0.25])
        Upstream.seen, self.slept[:] = [], []
        import email.utils
        self.busy_then_ok(1, headers=[("Retry-After", email.utils.formatdate(time.time() + 5, usegmt=True))])
        self.assertEqual(self.chat()[0].status, 200)
        self.assertEqual(len(self.slept), 1)
        self.assertTrue(3 <= self.slept[0] <= 5, self.slept)

    def test_backoff_when_the_upstream_names_no_wait_and_the_bound(self):
        self.start({"MODELPROXY_UPSTREAM_RETRIES": "3"})
        self.busy_then_ok(99)
        resp, data = self.chat()
        self.assertEqual(resp.status, 429)                 # room mode: the last answer goes through as it is
        self.assertEqual((len(Upstream.seen), self.slept), (4, [1.0, 2.0, 4.0]))

    def test_a_wait_longer_than_the_limit_is_not_started(self):
        self.start({"MODELPROXY_UPSTREAM_RETRIES": "3", "MODELPROXY_RETRY_MAX_WAIT": "10"})
        self.busy_then_ok(99, headers=[("Retry-After", "6")])
        self.assertEqual(self.chat()[0].status, 429)
        self.assertEqual((len(Upstream.seen), self.slept), (2, [6.0]))   # 6 + 6 would pass 10 s
        Upstream.seen, self.slept[:] = [], []
        self.busy_then_ok(99, headers=[("Retry-After", "3600")])
        self.assertEqual(self.chat()[0].status, 429)
        self.assertEqual((len(Upstream.seen), self.slept), (1, []))

    def test_a_stream_that_started_is_never_retried(self):
        self.start({"MODELPROXY_UPSTREAM_RETRIES": "3"})

        def reply(h, req):
            h.start_stream()
            h.wfile.write(sse(chunk({"content": "partial"}), done=False))   # then the connection drops
        Upstream.reply = reply
        resp, data = self.chat(stream=True)
        self.assertEqual(resp.status, 200)
        self.assertIn(b"partial", data)
        self.assertEqual((len(Upstream.seen), self.slept), (1, []))

    def test_stream_after_a_retry_is_intact(self):
        self.start({"MODELPROXY_UPSTREAM_RETRIES": "2"})
        self.busy_then_ok(1, stream=True)
        resp, data = self.chat(stream=True)
        self.assertEqual((resp.status, data), (200, sse(chunk({"content": "hi"}))))
        self.assertEqual(self.usage()["total"]["total_tokens"], 18)


def new_token(bot_id):
    return f"fbk_{bot_id}_" + base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class GatewayBase(Base):
    """The proxy in gateway mode, with its admin listener, in front of the fake upstream."""
    ALICE, BOB = "bq4x7k2m9pa", "bz3n8w5t6rc"
    # "m": 11 prompt + 7 completion tokens at 100 / 200 USD per million = 0.0025 USD per call
    PRICES = {"m": [100, 200], "big": [1000, 2000]}

    def setUp(self):
        self.admin = self.gw = None
        self.tokens = {self.ALICE: new_token(self.ALICE), self.BOB: new_token(self.BOB)}
        self.secrets = (*Base.secrets, *self.tokens.values())
        super().setUp()
        self.put(self.table())

    def table(self, **over):
        return {"tenants": [{"id": self.ALICE, "token_sha256": digest(self.tokens[self.ALICE]), "usd_per_day": 0.004,
                             "models": ["m", "big"], "enabled": True},
                            {"id": self.BOB, "token_sha256": digest(self.tokens[self.BOB]), "usd_per_day": 1,
                             "models": ["m"], "enabled": True}],
                "prices": self.PRICES, "platform_usd_per_day": 100, **over}

    def start(self, env):
        self.stop()
        full = {"MODEL_BASE_URL": f"http://127.0.0.1:{self.upstream.server_address[1]}/v1",
                "MODEL_API_KEY": REAL_KEY, "MODELPROXY_MODE": "gateway", "MODELPROXY_LISTEN_HOST": "127.0.0.1",
                "MODELPROXY_LISTEN_PORT": "0", "MODELPROXY_ADMIN_LISTEN_PORT": "0",
                "MODELPROXY_DATA_DIR": self.tmp.name, **env}
        self.cfg = mp.Config(full)
        self.cfg.listen_port = 0
        self.slept = []
        self.cfg.sleep = self.slept.append
        self.gw = mp.Gateway(self.cfg, clock=lambda: self.now[0])
        self.proxy = mp.make_server(self.cfg, gateway=self.gw)
        self.admin = mp.make_admin_server(self.cfg, self.gw)
        self.port, self.admin_port = self.proxy.server_address[1], self.admin.server_address[1]
        serve(self.proxy)
        serve(self.admin)

    def stop(self):
        for s in (self.proxy, self.admin):
            if s:
                s.shutdown()
                s.server_close()
        self.proxy = self.admin = None

    def tearDown(self):
        self.admin.shutdown()
        self.admin.server_close()
        for name in os.listdir(self.tmp.name):       # no token and no upstream key in any stored file
            with open(os.path.join(self.tmp.name, name), encoding="utf-8") as f:
                stored = f.read()
            for secret in self.secrets:
                self.assertNotIn(secret, stored, f"a secret was written to {name}")
        super().tearDown()

    def admin_call(self, method, path, body=None, headers=None, port=None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.admin_port, timeout=10)
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def put(self, table):
        resp, data = self.admin_call("PUT", "/admin/tenants", table)
        self.assertEqual(resp.status, 200, data)
        return json.loads(data)

    def report(self, query=""):
        return json.loads(self.admin_call("GET", "/admin/usage" + query)[1])

    def ask(self, who=None, model="m", **extra):
        who = who or self.ALICE
        return self.call("POST", "/v1/chat/completions",
                         {"model": model, "messages": [{"role": "user", "content": BODY_CANARY}], **extra},
                         {"Authorization": "Bearer " + self.tokens[who]})

    def code(self, resp_data):
        return resp_data[0].status, json.loads(resp_data[1])["error"]["code"]


class GatewayAuth(GatewayBase):
    def test_a_valid_token_is_swapped_for_the_upstream_key(self):
        resp, data = self.ask()
        self.assertEqual((resp.status, data), (200, completion()))
        seen = Upstream.seen[0]
        self.assertEqual(seen["headers"]["authorization"], f"Bearer {REAL_KEY}")
        self.assertNotIn(self.tokens[self.ALICE], json.dumps(seen["headers"]) + seen["body"].decode())
        self.assertIn(f"tenant={self.ALICE}", LOGS[-1])

    def test_everything_else_is_refused_before_the_upstream(self):
        good = self.tokens[self.ALICE]
        stranger = new_token("b0000000000")
        wrong_secret = new_token(self.ALICE)
        for header in (None, "", "Bearer", "Bearer " + PLACEHOLDER, "Bearer " + REAL_KEY, "Bearer " + stranger,
                       "Bearer " + wrong_secret, "Bearer " + good[:-1], "Bearer " + good + "A", "Basic " + good,
                       "Bearer " + good.replace(self.ALICE, self.BOB), "Bearer " + digest(good)):
            headers = {} if header is None else {"Authorization": header}
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("POST", "/v1/chat/completions", body=b'{"model":"m","messages":[]}', headers=headers)
            resp = conn.getresponse()
            err = json.loads(resp.read())["error"]
            conn.close()
            self.assertEqual((resp.status, err["code"]), (401, "invalid_api_key"), header)
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("GET", "/v1/models", headers=headers)
            self.assertEqual(conn.getresponse().status, 401, header)
            conn.close()
        self.assertEqual(Upstream.seen, [])
        self.assertEqual(self.report()["rejected_since_start"]["auth"], 24)
        self.secrets = (*self.secrets, stranger, wrong_secret)   # refused tokens are not logged either

    def test_a_tenant_that_is_switched_off(self):
        table = self.table()
        table["tenants"][0]["enabled"] = False
        self.put(table)
        self.assertEqual(self.code(self.ask()), (403, "tenant_disabled"))
        self.assertEqual(self.ask(self.BOB)[0].status, 200)
        self.assertEqual(len(Upstream.seen), 1)

    def test_only_the_hash_of_a_token_is_stored(self):
        self.ask()
        with open(os.path.join(self.tmp.name, "tenants.json"), encoding="utf-8") as f:
            stored = f.read()
        self.assertIn(digest(self.tokens[self.ALICE]), stored)
        self.assertNotIn(self.tokens[self.ALICE], stored)   # (tearDown checks every file and the log again)
        self.assertNotIn(self.tokens[self.ALICE], self.admin_call("GET", "/admin/tenants")[1].decode())

    def test_headers_stay_on_their_side(self):
        Upstream.reply = lambda h, req: h.send_bytes(200, completion(), extra=[
            ("Set-Cookie", "acct=1"), ("OpenAI-Organization", "org-shared"), ("X-RateLimit-Remaining-Tokens", "5"),
            ("X-Request-Id", "r1")])
        resp, _ = self.call("POST", "/v1/chat/completions", {"model": "m", "messages": []},
                            {"Authorization": "Bearer " + self.tokens[self.ALICE], "X-Forwarded-For": "203.0.113.9",
                             "Cookie": "a=b", "CF-Connecting-IP": "203.0.113.9", "User-Agent": "agent/1"})
        sent = Upstream.seen[0]["headers"]
        self.assertEqual(sorted(sent), ["accept-encoding", "authorization", "content-length", "content-type", "host",
                                        "user-agent"])
        got = {k.lower() for k, _ in resp.getheaders()}
        self.assertEqual(got & {"set-cookie", "openai-organization", "x-ratelimit-remaining-tokens"}, set())
        self.assertEqual(resp.getheader("X-Request-Id"), "r1")


class GatewayAdmin(GatewayBase):
    def test_hot_reload_without_a_restart(self):
        carol = "bc2c2c2c2c2"
        self.tokens[carol] = new_token(carol)
        self.secrets = (*self.secrets, self.tokens[carol])
        self.assertEqual(self.code(self.ask(carol)), (401, "invalid_api_key"))
        table = self.table()
        table["tenants"].append({"id": carol, "token_sha256": digest(self.tokens[carol]), "usd_per_day": 1,
                                 "models": ["m"]})                       # "enabled" defaults to true
        self.assertEqual(self.put(table), {"ok": True, "tenants": 3, "enabled": 3})
        self.assertEqual(self.ask(carol)[0].status, 200)
        table["tenants"] = table["tenants"][1:]                           # ALICE is removed
        self.put(table)
        self.assertEqual(self.code(self.ask()), (401, "invalid_api_key"))
        self.assertEqual(self.ask(carol)[0].status, 200)
        rotated = new_token(carol)                                        # a new token for the same bot
        table["tenants"][-1]["token_sha256"] = digest(rotated).upper()    # hex in either case
        self.put(table)
        self.assertEqual(self.code(self.ask(carol)), (401, "invalid_api_key"))
        self.tokens[carol] = rotated
        self.secrets = (*self.secrets, rotated)
        self.assertEqual(self.ask(carol)[0].status, 200)
        stored = {t["id"]: t for t in json.loads(self.admin_call("GET", "/admin/tenants")[1])["tenants"]}
        self.assertEqual((sorted(stored), stored[carol]["token_sha256"]), (sorted([carol, self.BOB]), digest(rotated)))

    def test_a_faulty_table_changes_nothing(self):
        good = self.table()
        t0 = dict(good["tenants"][0])
        faulty = [
            b"{not json", [], {}, {**good, "tenants": "x"}, {**good, "extra": 1},
            {k: v for k, v in good.items() if k != "platform_usd_per_day"},
            {**good, "platform_usd_per_day": -1}, {**good, "platform_usd_per_day": "9"},
            {**good, "prices": {"m": [1]}}, {**good, "prices": {"m": [1, -2]}}, {**good, "prices": []},
            {**good, "prices": {"big": [1, 2]}},                              # ALICE and BOB may use "m": no price
            {**good, "tenants": [t0, t0]},                                     # the same id twice
            {**good, "tenants": [{**t0, "id": "has_underscore"}]}, {**good, "tenants": [{**t0, "id": ""}]},
            {**good, "tenants": [{**t0, "token_sha256": "abc"}]},
            {**good, "tenants": [{**t0, "token_sha256": self.tokens[self.ALICE]}]},   # a token instead of its hash
            {**good, "tenants": [{**t0, "usd_per_day": None}]}, {**good, "tenants": [{**t0, "usd_per_day": True}]},
            {**good, "tenants": [{**t0, "models": "m"}]}, {**good, "tenants": [{**t0, "models": ["*"]}]},
            {**good, "tenants": [{**t0, "enabled": "yes"}]}, {**good, "tenants": [{**t0, "token": "x"}]},
            {**good, "tenants": [{k: v for k, v in t0.items() if k != "models"}]},
        ]
        for body in faulty:
            resp, data = self.admin_call("PUT", "/admin/tenants", body)
            self.assertEqual(resp.status, 400, body)
            self.assertFalse(json.loads(data)["ok"])
            self.assertNotIn(self.tokens[self.ALICE], data.decode())
        self.assertEqual(json.loads(self.admin_call("GET", "/admin/tenants")[1]), {
            **good, "prices": {k: [float(a), float(b)] for k, (a, b) in good["prices"].items()},
            "tenants": [{**t, "usd_per_day": float(t["usd_per_day"])} for t in good["tenants"]],
            "platform_usd_per_day": 100.0})
        self.assertEqual(self.ask()[0].status, 200)

    def test_a_table_that_cannot_be_stored_is_not_used(self):
        table = self.table()
        table["tenants"][0]["enabled"] = False
        os.mkdir(os.path.join(self.tmp.name, "tenants.json.tmp"))          # the write will fail
        resp, _ = self.admin_call("PUT", "/admin/tenants", table)
        os.rmdir(os.path.join(self.tmp.name, "tenants.json.tmp"))
        self.assertEqual(resp.status, 500)
        self.assertEqual(self.ask()[0].status, 200)                         # ALICE is still enabled

    def test_admin_api_is_not_on_the_public_listener(self):
        before = self.admin_call("GET", "/admin/tenants")[1]
        empty = {"tenants": [], "prices": {}, "platform_usd_per_day": None}
        token = {"Authorization": "Bearer " + self.tokens[self.ALICE]}
        for method, path, body in (("GET", "/admin/usage", None), ("GET", "/admin/tenants", None),
                                   ("PUT", "/admin/tenants", empty), ("POST", "/admin/tenants", empty),
                                   ("GET", "/v1/admin/tenants", None), ("PUT", "/v1/admin/tenants", empty),
                                   ("GET", "/usage", None), ("GET", "//admin/usage", None),
                                   ("GET", "/v1/../admin/usage", None), ("DELETE", "/admin/tenants", None)):
            for headers in ({}, token):
                resp, data = self.admin_call(method, path, body, headers, port=self.port)
                self.assertIn(resp.status, (404, 501), (method, path))
                self.assertNotIn(b"token_sha256", data)
                self.assertNotIn(b"platform", data)
        self.assertEqual(self.admin_call("GET", "/admin/tenants")[1], before)
        self.assertEqual(self.ask()[0].status, 200)
        # and the other way round: no inference on the admin listener
        resp, _ = self.admin_call("POST", "/v1/chat/completions", {"model": "m", "messages": []}, token)
        self.assertEqual(resp.status, 501)
        self.assertEqual(self.admin_call("GET", "/v1/models", None, token)[0].status, 404)
        self.assertEqual(len(Upstream.seen), 1)

    def test_optional_admin_token(self):
        admin_token = "adm-" + base64.urlsafe_b64encode(os.urandom(24)).decode()
        self.secrets = (*self.secrets, admin_token)
        self.start({"MODELPROXY_ADMIN_TOKEN": admin_token})
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Bearer " + self.tokens[self.ALICE]}):
            self.assertEqual(self.admin_call("GET", "/admin/usage", None, headers)[0].status, 401)
            self.assertEqual(self.admin_call("GET", "/admin/tenants", None, headers)[0].status, 401)
            self.assertEqual(self.admin_call("PUT", "/admin/tenants", self.table(), headers)[0].status, 401)
        ok = {"Authorization": "Bearer " + admin_token}
        self.assertEqual(self.admin_call("PUT", "/admin/tenants", self.table(platform_usd_per_day=None), ok)[0].status, 200)
        self.assertIsNone(json.loads(self.admin_call("GET", "/admin/usage", None, ok)[1])["platform"]["usd_per_day"])
        self.assertEqual(self.admin_call("GET", "/healthz")[0].status, 200)

    def test_bad_gateway_configuration_is_refused(self):
        base = {"MODEL_BASE_URL": "https://example.invalid/v1", "MODELPROXY_MODE": "gateway"}
        for env in ({**base, "MODELPROXY_MODE": "both"}, {**base, "BUDGET_TOKENS_PER_DAY": "5"},
                    {**base, "BUDGET_PRICES": '{"m": [1, 2]}'}, {**base, "MODELPROXY_ADMIN_LISTEN_PORT": "9001"},
                    {**base, "MODELPROXY_UPSTREAM_RETRIES": "-1"}, {**base, "MODELPROXY_UPSTREAM_RETRIES": "x"}):
            with self.assertRaises(mp.ConfigError, msg=str(env)):
                mp.Config(env)
        cfg = mp.Config(base)
        self.assertEqual((cfg.retries, cfg.admin_host, cfg.admin_port, cfg.tenant_concurrency), (3, "127.0.0.1", 9002, 4))
        self.assertEqual(mp.Config({**base, "MODELPROXY_UPSTREAM_RETRIES": "0"}).retries, 0)
        self.assertEqual(mp.Config({"MODEL_BASE_URL": "https://example.invalid/v1"}).retries, 0)   # room mode


class GatewayModels(GatewayBase):
    def test_a_model_outside_the_list_is_refused_clearly(self):
        resp, data = self.ask(self.BOB, model="big")
        err = json.loads(data)["error"]
        self.assertEqual((resp.status, err["type"], err["code"]), (400, "invalid_request_error", "model_not_allowed"))
        self.assertEqual(err["message"], "The model 'big' is not available to this bot. Available: m.")
        self.assertEqual(self.ask(self.ALICE, model="big")[0].status, 200)     # on ALICE's list
        self.assertEqual(self.code(self.ask(model="M")), (400, "model_not_allowed"))   # ids are exact
        self.assertEqual(self.code(self.ask(model=["m"])), (400, "model_required"))
        resp, data = self.call("POST", "/v1/chat/completions", b"not json at all",
                               {"Authorization": "Bearer " + self.tokens[self.ALICE]})
        self.assertEqual(self.code((resp, data)), (400, "model_required"))
        self.assertEqual(len(Upstream.seen), 1)
        self.assertEqual(self.report()["rejected_since_start"]["model"], 4)

    def test_models_endpoint_is_the_tenants_own_list(self):
        for who, ids in ((self.ALICE, ["m", "big"]), (self.BOB, ["m"])):
            resp, data = self.call("GET", "/v1/models", headers={"Authorization": "Bearer " + self.tokens[who]})
            self.assertEqual((resp.status, [m["id"] for m in json.loads(data)["data"]]), (200, ids))
        auth = {"Authorization": "Bearer " + self.tokens[self.BOB]}
        self.assertEqual(json.loads(self.call("GET", "/v1/models/m", headers=auth)[1])["id"], "m")
        self.assertEqual(self.call("GET", "/v1/models/big", headers=auth)[0].status, 404)
        self.assertEqual(Upstream.seen, [])                # answered here: the upstream's list is not shown


class ExhaustedTexts(unittest.TestCase):
    """What the user reads (after OpenClaw's "LLM request rejected: ") once an allowance is used up."""

    TEXTS = {"today": mp.USED_UP_TODAY, "total": mp.USED_UP_TOTAL, "platform": mp.USED_UP_PLATFORM}

    def test_no_punctuation_and_no_digits(self):
        for name, t in self.TEXTS.items():
            self.assertRegex(t, r"\A[A-Za-z ]+\Z", name)   # the web chat backslash-escapes punctuation

    def test_each_says_what_to_do_now(self):
        for name, t in self.TEXTS.items():
            self.assertIn("free allowance", t, name)
            self.assertIn("used up", t, name)
            self.assertIn("add your own model key in Settings on the Fugal Bot page", t, name)
            self.assertNotRegex(t.lower(), r"quota|exceed|credit|rate limit|retry|billing", name)

    def test_daily_texts_say_when_it_comes_back(self):
        for name in ("today", "platform"):
            self.assertIn("comes back at midnight UTC", self.TEXTS[name], name)
        self.assertNotIn("comes back", self.TEXTS["total"])    # a total cap does not reset
        self.assertIn("shared by all bots", self.TEXTS["platform"])
        self.assertNotIn("shared by all bots", self.TEXTS["today"])


class GatewayAllowance(GatewayBase):
    def test_tenant_daily_allowance_in_usd(self):
        self.assertEqual(self.ask()[0].status, 200)        # 0.0025 USD
        self.assertEqual(self.ask()[0].status, 200)        # 0.0050: the call in flight may overshoot 0.004
        resp, data = self.ask(stream=True)
        err = json.loads(data)["error"]
        self.assertEqual((resp.status, err["code"]), (400, "allowance_exhausted"))
        self.assertEqual(err["message"], mp.USED_UP_TODAY)
        self.assertEqual(len(Upstream.seen), 2)            # the refused call never left
        self.assertEqual(self.ask(self.BOB)[0].status, 200)   # another tenant is not affected
        r = self.report()
        alice, bob = r["tenants"][self.ALICE], r["tenants"][self.BOB]
        self.assertEqual((alice["exceeded"], alice["remaining_usd"], alice["day"]["requests"]), (True, 0.0, 2))
        self.assertAlmostEqual(alice["day"]["usd"], 0.005)
        self.assertEqual((bob["exceeded"], bob["day"]["total_tokens"]), (False, 18))
        self.assertAlmostEqual(bob["remaining_usd"], 0.9975)
        self.assertAlmostEqual(r["platform"]["day"]["usd"], 0.0075)
        self.assertEqual(r["rejected_since_start"]["tenant_allowance"], 1)
        self.now[0] += 86400                               # next UTC day
        self.assertEqual(self.ask()[0].status, 200)
        r = self.report()
        self.assertEqual((r["today"], sorted(r["tenants"][self.ALICE]["days"])), ("2026-11-23", ["2026-11-22", "2026-11-23"]))

    def test_raising_an_allowance_takes_effect_at_once(self):
        self.ask()
        self.ask()
        self.assertEqual(self.code(self.ask()), (400, "allowance_exhausted"))
        table = self.table()
        table["tenants"][0]["usd_per_day"] = 0.01
        self.put(table)
        self.assertEqual(self.ask()[0].status, 200)
        table["tenants"][0]["usd_per_day"] = 0             # zero is "nothing", not "no limit"
        self.put(table)
        self.assertEqual(self.code(self.ask()), (400, "allowance_exhausted"))

    def test_dearer_model_spends_faster(self):
        self.assertEqual(self.ask(model="big")[0].status, 200)   # 11 * 1000 + 7 * 2000 per million = 0.025 USD
        self.assertAlmostEqual(self.report()["tenants"][self.ALICE]["day"]["usd"], 0.025)
        self.assertEqual(self.code(self.ask(model="m")), (400, "allowance_exhausted"))

    def test_platform_cap_across_all_tenants(self):
        self.put(self.table(platform_usd_per_day=0.004))
        self.assertEqual(self.ask(self.BOB)[0].status, 200)
        self.assertEqual(self.ask(self.ALICE)[0].status, 200)   # platform: 0.005, each tenant under its own allowance
        for who in (self.ALICE, self.BOB):
            resp, data = self.ask(who)
            self.assertEqual(self.code((resp, data)), (400, "allowance_exhausted"))
            self.assertEqual(json.loads(data)["error"]["message"], mp.USED_UP_PLATFORM)
        r = self.report()
        self.assertEqual((r["platform"]["exceeded"], r["platform"]["remaining_usd"]), (True, 0.0))
        self.assertEqual(r["rejected_since_start"]["platform_allowance"], 2)
        self.assertEqual(len(Upstream.seen), 2)
        self.put(self.table(platform_usd_per_day=0.5))     # the owner raises it
        self.assertEqual(self.ask(self.BOB)[0].status, 200)
        self.put(self.table(platform_usd_per_day=0))       # zero stops everything
        self.assertEqual(self.code(self.ask(self.BOB)), (400, "allowance_exhausted"))
        self.put(self.table(platform_usd_per_day=None))    # null: no platform cap
        self.assertEqual(self.ask(self.BOB)[0].status, 200)
        self.put(self.table(platform_usd_per_day=0.004))
        self.now[0] += 86400
        self.assertEqual(self.ask(self.BOB)[0].status, 200)   # a new day

    def test_exhausted_reply_mode_applies_to_the_gateway_too(self):
        self.start({"MODELPROXY_EXHAUSTED_REPLY": "429"})
        self.put(self.table())
        self.ask()
        self.ask()
        self.assertEqual(self.code(self.ask()), (429, "insufficient_quota"))

    def test_stream_usage_is_counted_per_tenant(self):
        def reply(h, req):
            self.assertTrue(json.loads(req["body"])["stream_options"]["include_usage"])
            h.start_stream()
            h.wfile.write(sse(chunk({"content": "hi"}), USAGE_CHUNK))
        Upstream.reply = reply
        resp, data = self.ask(self.BOB, stream=True)
        self.assertEqual(data, sse(chunk({"content": "hi"})))
        day = self.report()["tenants"][self.BOB]["day"]
        self.assertEqual((day["prompt_tokens"], day["completion_tokens"], day["estimated_requests"]), (11, 7, 0))

    def test_too_many_calls_at_once_from_one_tenant(self):
        self.start({"MODELPROXY_TENANT_CONCURRENCY": "1"})
        self.put(self.table())
        entered, go = threading.Event(), threading.Event()

        def reply(h, req):
            if not entered.is_set():           # only the first call is held open
                entered.set()
                go.wait(5)
            h.send_bytes(200, completion())
        Upstream.reply = reply
        first = threading.Thread(target=self.ask, args=(self.BOB,))
        first.start()
        self.assertTrue(entered.wait(5))
        resp, data = self.ask(self.BOB)
        self.assertEqual((resp.status, json.loads(data)["error"]["code"], resp.getheader("Retry-After")),
                         (429, "rate_limit_exceeded", "2"))
        self.assertEqual(self.ask(self.ALICE)[0].status, 200)   # per tenant, not for all
        go.set()
        first.join(5)
        deadline = time.monotonic() + 5                     # a slot is handed back just after the reply
        while self.report()["tenants"][self.BOB]["in_flight"] and time.monotonic() < deadline:
            time.sleep(0.002)
        self.assertEqual(self.ask(self.BOB)[0].status, 200)
        self.assertEqual(self.report()["tenants"][self.BOB]["in_flight"], 0)



class GatewayCachedTokens(GatewayBase):
    # "m" bills cached input at a tenth; "plain" has two values; per call of 1000 prompt (800 cached) + 100 completion:
    # m: (200 * 100 + 800 * 10 + 100 * 200) / 1e6 = 0.048 USD, plain: 0.12 USD
    PRICES = {"m": [100, 200, 10], "plain": [100, 200], "big": [1000, 2000]}

    def table(self, **over):
        t = super().table(**over)
        t["tenants"][0].update(usd_per_day=0.1, models=["m", "plain", "big"])
        t["tenants"][1]["models"] = ["m", "plain"]
        return t

    def reply_with(self, usage):
        Upstream.reply = lambda h, req: h.send_bytes(200, completion(usage=usage))

    def test_three_value_rows_are_accepted_and_stored_as_given(self):
        stored = json.loads(self.admin_call("GET", "/admin/tenants")[1])
        self.assertEqual(stored["prices"], {"m": [100, 200, 10], "plain": [100, 200], "big": [1000, 2000]})
        with open(os.path.join(self.tmp.name, "tenants.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["prices"]["plain"], [100, 200])       # two values stay two: older gateways can read it
        for bad in ([1], [1, 2, 3, 4], [1, 2, -1], [1, 2, "x"], [1, 2, None]):
            resp, data = self.admin_call("PUT", "/admin/tenants", self.table(prices={"m": bad, "plain": [1, 2], "big": [1, 2]}))
            self.assertEqual(resp.status, 400, bad)
            self.assertIn("prices", json.loads(data)["error"])
        self.assertEqual(json.loads(self.admin_call("GET", "/admin/tenants")[1])["prices"]["m"], [100, 200, 10])

    def test_cached_tokens_are_counted_per_tenant_and_platform_and_billed_at_the_cached_price(self):
        self.reply_with(with_cache(800))
        self.assertEqual(self.ask()[0].status, 200)
        self.assertEqual(self.ask(self.BOB)[0].status, 200)
        r = self.report()
        for row in (r["tenants"][self.ALICE]["day"], r["tenants"][self.BOB]["day"]):
            self.assertEqual((row["prompt_tokens"], row["cached_prompt_tokens"]), (1000, 800))
            self.assertAlmostEqual(row["usd"], 0.048)
        self.assertEqual(r["platform"]["day"]["cached_prompt_tokens"], 1600)
        self.assertEqual(r["platform"]["days"]["2026-11-22"]["cached_prompt_tokens"], 1600)
        self.assertEqual(r["tenants"][self.ALICE]["days"]["2026-11-22"]["cached_prompt_tokens"], 800)
        self.assertAlmostEqual(r["platform"]["day"]["usd"], 0.096)
        self.assertAlmostEqual(r["tenants"][self.ALICE]["remaining_usd"], 0.052)

    def test_a_two_value_row_bills_cached_tokens_in_full(self):
        self.reply_with(with_cache(800))
        self.ask(model="plain")
        day = self.report()["tenants"][self.ALICE]["day"]
        self.assertEqual(day["cached_prompt_tokens"], 800)
        self.assertAlmostEqual(day["usd"], 0.12)

    def test_cached_tokens_stretch_the_allowance(self):
        self.reply_with(with_cache(800))
        self.assertEqual(self.ask()[0].status, 200)           # 0.048
        self.assertEqual(self.ask()[0].status, 200)           # 0.096, under 0.1: one more is let in
        self.assertEqual(self.ask()[0].status, 200)           # 0.144
        self.assertEqual(self.code(self.ask()), (400, "allowance_exhausted"))
        self.reply_with(with_cache(0, None))                  # without caching the same allowance ends after 1 call
        self.now[0] += 86400
        self.assertEqual(self.ask()[0].status, 200)
        self.assertEqual(self.code(self.ask()), (400, "allowance_exhausted"))

    def test_cached_above_prompt_is_capped_and_never_negative(self):
        self.reply_with(with_cache(5000))
        self.ask()
        day = self.report()["tenants"][self.ALICE]["day"]
        self.assertEqual(day["cached_prompt_tokens"], 1000)
        self.assertAlmostEqual(day["usd"], 0.03)
        self.assertGreaterEqual(day["usd"], 0)

    def test_streamed_calls_count_cached_tokens_per_tenant(self):
        def reply(h, req):
            h.start_stream()
            h.wfile.write(sse(chunk({"content": "hi"}), {"id": "c1", "object": "chat.completion.chunk",
                                                          "choices": [], "usage": with_cache(800, "deepseek")}))
        Upstream.reply = reply
        _, data = self.ask(self.BOB, stream=True)
        self.assertEqual(data, sse(chunk({"content": "hi"})))
        day = self.report()["tenants"][self.BOB]["day"]
        self.assertEqual((day["prompt_tokens"], day["cached_prompt_tokens"]), (1000, 800))
        self.assertAlmostEqual(day["usd"], 0.048)

    def test_cached_counters_survive_a_restart_and_old_day_files_load(self):
        self.reply_with(with_cache(800))
        self.ask()
        self.start({})
        self.assertEqual(self.report()["tenants"][self.ALICE]["day"]["cached_prompt_tokens"], 800)
        path = os.path.join(self.tmp.name, "gateway-usage-2026-11-22.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for row in (data["platform"], *data["tenants"].values()):      # a file written before the counter existed
            del row["cached_prompt_tokens"]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.start({})
        r = self.report()
        self.assertEqual((r["platform"]["day"]["requests"], r["platform"]["day"]["cached_prompt_tokens"]), (1, 0))
        self.assertEqual(sorted(n for n in os.listdir(self.tmp.name) if ".unreadable" in n), [])


class GatewayUpstream(GatewayBase):
    busy_then_ok = UpstreamRetry.busy_then_ok

    def test_a_passing_upstream_limit_never_reaches_the_tenant(self):
        self.busy_then_ok(2, headers=[("Retry-After", "1")])
        resp, data = self.ask()
        self.assertEqual((resp.status, data), (200, completion()))
        self.assertEqual((len(Upstream.seen), self.slept), (3, [1.0, 1.0]))
        self.assertEqual(self.report()["tenants"][self.ALICE]["day"]["requests"], 1)   # counted once

    def test_a_lasting_upstream_limit_is_a_plain_503_not_a_quota_error(self):
        for status in (429, 503):
            Upstream.seen, self.slept[:] = [], []
            self.busy_then_ok(99, status=status)
            resp, data = self.ask(self.BOB)
            err = json.loads(data)["error"]
            self.assertEqual((resp.status, err["type"], err["code"]), (503, "server_error", "upstream_unavailable"))
            self.assertNotIn(b"slow down", data)
            self.assertNotIn(b"quota", data)
            self.assertIsNone(resp.getheader("Retry-After"))
            self.assertEqual((len(Upstream.seen), self.slept), (4, [1.0, 2.0, 4.0]))
        self.assertEqual(self.report()["platform"]["day"]["requests"], 0)

    def test_trouble_with_the_shared_account_is_not_shown_to_tenants(self):
        for status, body in ((401, b'{"error":{"message":"Incorrect API key sk-...","code":"invalid_api_key"}}'),
                             (402, b'{"error":{"message":"account balance is empty","code":"insufficient_quota"}}'),
                             (403, b'{"error":{"message":"organisation org-shared is suspended"}}')):
            Upstream.seen = []
            Upstream.reply = lambda h, req, s=status, b=body: h.send_bytes(s, b)
            resp, data = self.ask()
            self.assertEqual(self.code((resp, data)), (503, "upstream_unavailable"), status)
            for leak in (b"sk-", b"balance", b"org-shared", b"insufficient_quota", b"invalid_api_key"):
                self.assertNotIn(leak, data)
            self.assertEqual((len(Upstream.seen), self.slept), (1, []))   # not a case for retrying
            self.assertIn(f"upstream {status}", LOGS[-1])

    def test_streams_are_not_buffered(self):
        token = {"Authorization": "Bearer " + self.tokens[self.ALICE]}
        Streaming._first_event_arrives_before_the_upstream_finishes(self, headers=token)
        Streaming._first_event_arrives_before_the_upstream_finishes(
            self, headers=token, stream_options={"include_usage": True})

    def test_other_upstream_errors_pass_through(self):
        err = b'{"error":{"message":"context too long","type":"invalid_request_error","code":"context_length_exceeded"}}'
        Upstream.reply = lambda h, req: h.send_bytes(400, err)
        resp, data = self.ask()
        self.assertEqual((resp.status, data), (400, err))
        self.assertEqual(self.report()["platform"]["day"]["requests"], 0)


class GatewayPersistence(GatewayBase):
    def test_table_and_usage_survive_a_restart(self):
        self.ask()
        self.ask()                                         # ALICE is over her allowance
        self.ask(self.BOB)
        self.now[0] += 86400
        self.ask(self.BOB)
        self.now[0] -= 86400
        self.start({})                                     # a new process on the same directory; no PUT
        self.assertTrue(self.gw.loaded_table)
        self.assertEqual(self.code(self.ask()), (400, "allowance_exhausted"))   # still counted
        self.assertEqual(self.ask(self.BOB)[0].status, 200)
        r = self.report()
        self.assertEqual((r["tenants"][self.BOB]["day"]["requests"], r["platform"]["day"]["requests"]), (2, 4))
        self.assertEqual(sorted(os.listdir(self.tmp.name)),
                         ["gateway-usage-2026-11-22.json", "gateway-usage-2026-11-23.json",
                          "requests-2026-11-22.jsonl", "requests-2026-11-23.jsonl", "tenants.json"])
        self.assertEqual(os.stat(os.path.join(self.tmp.name, "tenants.json")).st_mode & 0o777, 0o600)

    def test_empty_until_the_first_table(self):
        os.remove(os.path.join(self.tmp.name, "tenants.json"))
        self.start({})
        self.assertFalse(self.gw.loaded_table)
        self.assertEqual(self.code(self.ask()), (401, "invalid_api_key"))
        self.assertEqual(self.report()["tenants"], {})
        self.put(self.table())
        self.assertEqual(self.ask()[0].status, 200)

    def test_unreadable_files_are_kept_aside(self):
        self.ask(self.BOB)
        for name in ("tenants.json", "gateway-usage-2026-11-22.json"):
            with open(os.path.join(self.tmp.name, name), "w", encoding="utf-8") as f:
                f.write('{"tenants": [{"id": 5}')
        self.start({})
        self.assertEqual(self.code(self.ask(self.BOB)), (401, "invalid_api_key"))
        names = os.listdir(self.tmp.name)
        self.assertTrue(any(n.startswith("tenants.json.unreadable-") for n in names))
        self.assertTrue(any(n.startswith("gateway-usage-2026-11-22.json.unreadable-") for n in names))
        self.put(self.table())
        self.assertEqual(self.ask(self.BOB)[0].status, 200)
        self.assertEqual(self.report()["platform"]["day"]["requests"], 1)

    def test_old_days_are_dropped_and_recent_days_reported(self):
        for _ in range(mp.KEEP_GATEWAY_DAYS + 3):
            self.ask(self.BOB)
            self.now[0] += 86400
        files = [n for n in os.listdir(self.tmp.name) if n.startswith("gateway-usage-")]
        self.assertEqual(len(files), mp.KEEP_GATEWAY_DAYS)
        self.assertNotIn("gateway-usage-2026-11-22.json", files)
        self.assertEqual(len(self.report()["platform"]["days"]), 7)
        self.assertEqual(len(self.report("?days=30")["tenants"][self.BOB]["days"]), 30)
        self.assertEqual(len(self.report("?days=9999")["platform"]["days"]), mp.KEEP_GATEWAY_DAYS)

    def test_a_removed_tenant_keeps_its_usage_in_the_report(self):
        self.ask(self.BOB)
        table = self.table()
        table["tenants"] = table["tenants"][:1]
        self.put(table)
        bob = self.report()["tenants"][self.BOB]
        self.assertEqual((bob["in_table"], bob["enabled"], bob["usd_per_day"], bob["day"]["requests"]),
                         (False, False, None, 1))


class RoomProxyBehindGateway(GatewayBase):
    """agent -> room-mode proxy -> gateway -> upstream, all in this process: the
    room proxy is configured with the gateway's URL and the bot's token only."""

    def setUp(self):
        super().setUp()
        self.room_dir = tempfile.TemporaryDirectory()
        cfg = mp.Config({"MODEL_BASE_URL": f"http://127.0.0.1:{self.port}/v1", "MODEL_API_KEY": self.tokens[self.ALICE],
                         "MODELPROXY_LISTEN_HOST": "127.0.0.1", "MODELPROXY_LISTEN_PORT": "0",
                         "MODELPROXY_DATA_DIR": self.room_dir.name})
        cfg.listen_port = 0
        self.room_meter = mp.Meter(cfg, clock=lambda: self.now[0])
        self.room = mp.make_server(cfg, self.room_meter)
        self.gateway_port, self.port = self.port, self.room.server_address[1]   # self.call() now talks to the room proxy
        serve(self.room)

    def tearDown(self):
        self.room.shutdown()
        self.room.server_close()
        with open(os.path.join(self.room_dir.name, "usage.json"), encoding="utf-8") as f:
            self.assertNotIn(self.tokens[self.ALICE], f.read())
        self.room_dir.cleanup()
        super().tearDown()

    def test_a_streamed_call_through_both(self):
        def reply(h, req):
            h.start_stream()
            h.wfile.write(sse(chunk({"content": "hi"}), USAGE_CHUNK))
        Upstream.reply = reply
        logged = len(LOGS)
        resp, data = self.chat(stream=True)                # the agent sends its placeholder key
        wait_logs(logged + 2)
        self.assertEqual((resp.status, data), (200, sse(chunk({"content": "hi"}))))
        seen = Upstream.seen[0]
        self.assertEqual(seen["headers"]["authorization"], f"Bearer {REAL_KEY}")
        self.assertNotIn(self.tokens[self.ALICE], json.dumps(seen["headers"]))
        self.assertEqual(self.usage()["total"]["total_tokens"], 18)                          # the room's counters
        self.assertEqual(self.report()["tenants"][self.ALICE]["day"]["total_tokens"], 18)    # and the gateway's

    def test_the_gateways_refusal_reaches_the_agent_unchanged(self):
        self.chat()
        self.chat()
        resp, data = self.chat()
        err = json.loads(data)["error"]
        self.assertEqual((resp.status, err["code"]), (400, "allowance_exhausted"))
        self.assertEqual(err["message"], mp.USED_UP_TODAY)
        self.assertEqual(self.code(self.chat(model="other")), (400, "model_not_allowed"))
        self.assertEqual(self.usage()["total"]["requests"], 2)   # refused calls are not counted in the room either
        table = self.table()
        table["tenants"][0]["usd_per_day"] = 1
        self.put(table)
        self.assertEqual(self.chat()[0].status, 200)            # raised centrally: the next call goes through


# ---- broken tool calls ---------------------------------------------------------------
# Recorded answers of DeepSeek-V4-Flash on Chutes (2026-11-23, a real bot's
# turns, model output only): four with the model's `</｜DSML｜>` markup
# inside tool-call arguments (the JSON still parses), one whose arguments turn
# into garbage that is not JSON; and two good ones.
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
BAD = ("bad-dsml-1.sse", "bad-dsml-2.sse", "bad-dsml-3.sse", "bad-dsml-4.sse", "bad-garbage-args.sse")


def fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


def fixture_usage(name):
    for line in reversed(fixture(name).split(b"\n")):
        if line.startswith(b"data: {") and b'"usage":{' in line:
            return mp.usage_tokens(json.loads(line[6:]))


def check_stream(raw):
    check = mp.ToolCallCheck()
    scan = mp.SSEScanner(strip=False, observer=check.payload)
    scan.feed(raw)
    scan.finish()
    return check


TOOLS = [{"type": "function", "function": {"name": "exec", "parameters": {"type": "object"}}}]
GOOD_CALL = [chunk({"role": "assistant", "content": None, "tool_calls": [
                 {"index": 0, "id": "call_1", "type": "function", "function": {"name": "exec", "arguments": ""}}]}),
             chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"command": "taoq subnet 70 --text"}'}}]}),
             chunk({}, "tool_calls")]


def tool_json(arguments, name="exec", finish="tool_calls", usage=USAGE):
    return json.dumps({"id": "c1", "object": "chat.completion", "model": "m", "usage": usage, "choices": [
        {"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}]}}]}).encode()


class ToolCallChecks(unittest.TestCase):
    """The check alone, on recorded and made-up answers."""

    def test_recorded_broken_answers_are_found(self):
        reasons = {name: check_stream(fixture(name)).problem(stream=True) for name in BAD}
        self.assertEqual(reasons, {"bad-dsml-1.sse": "model markup in a tool call",
                                   "bad-dsml-2.sse": "model markup in a tool call",
                                   "bad-dsml-3.sse": "model markup in a tool call",
                                   "bad-dsml-4.sse": "model markup in a tool call",
                                   "bad-garbage-args.sse": "tool-call arguments are not valid JSON"})

    def test_recorded_good_answers_pass(self):
        for name in ("good-tool-call.sse", "good-text.sse"):
            check = check_stream(fixture(name))
            self.assertIsNone(check.problem(stream=True), name)
        self.assertTrue(check_stream(fixture("good-tool-call.sse")).tool_seen)
        self.assertFalse(check_stream(fixture("good-text.sse")).tool_seen)

    def test_ordinary_symbols_are_not_markup(self):
        for command in ('ls -l | grep "<b>" > /tmp/x 2>&1 || echo "a|b"', "echo ▁▂▃ progress",
                        "echo 'A｜B'", "printf '<|im_start|>'", "cat <<'EOF'\n|x|\nEOF"):
            check = check_stream(sse(*GOOD_CALL[:1], chunk({"tool_calls": [{"index": 0, "function": {
                "arguments": json.dumps({"command": command})}}]}), chunk({}, "tool_calls")))
            self.assertIsNone(check.problem(stream=True), command)

    def test_each_kind_of_broken_call(self):
        def one(*events, done=True):
            return check_stream(sse(*events, done=done)).problem(stream=True)
        start = chunk({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "exec", "arguments": ""}}]})

        def args(text):
            return chunk({"tool_calls": [{"index": 0, "function": {"arguments": text}}]})
        self.assertEqual(one(start, args('{"command": "ls'), done=False), "tool-call arguments are not valid JSON")
        self.assertEqual(one(start, args('{"command": "ls"}'), done=False), "the stream ended inside a tool call")
        self.assertEqual(one(start, args('{"command": "ls"}')), "the stream ended without a finish reason")
        self.assertEqual(one(start, args('{"command": "ls"}'), chunk({}, "length")),
                         "a tool call cut off at the length limit")
        # cut at the cap in the middle of the arguments: the cap is the reason (not retried), not the bad JSON
        self.assertEqual(one(start, args('{"command": "cat > a.html <<EOF'), chunk({}, "length")),
                         mp.LENGTH_CUT)
        self.assertEqual(one(start, args('["ls"]'), chunk({}, "tool_calls")), "tool-call arguments are not a JSON object")
        self.assertEqual(one(chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}), chunk({}, "tool_calls")),
                         "a tool call without a name")
        # markup escaped inside the arguments' JSON, and markup split across two text chunks
        self.assertEqual(one(start, args('{"command": "ls\\uff5c>"}'), chunk({}, "tool_calls")),
                         "model markup in a tool call")
        self.assertEqual(one(chunk({"content": "ok <"}), chunk({"content": "｜tool▁calls"}), start,
                             args("{}"), chunk({}, "tool_calls")), "model markup in the text")
        self.assertIsNone(one(start, args(""), chunk({}, "tool_calls")))       # no arguments at all is fine
        self.assertEqual(mp.ToolCallCheck().problem(stream=True, interrupted=True), "the answer was cut off")

    def test_whole_json_answers(self):
        for body, want in ((tool_json('{"command": "ls"}'), None),
                           (tool_json('{"command": "ls</｜DSML｜>"}'), "model markup in a tool call"),
                           (tool_json('{"command": "ls" HHHH'), "tool-call arguments are not valid JSON"),
                           (tool_json('{"command": "l', finish="length"), mp.LENGTH_CUT),
                           (completion(), None)):
            check = mp.ToolCallCheck()
            check.message(json.loads(body))
            self.assertEqual(check.problem(stream=False), want)

    def test_which_requests_are_checked(self):
        self.assertTrue(mp.offers_tools({"tools": TOOLS}))
        self.assertTrue(mp.offers_tools({"functions": [{"name": "f"}]}))
        for req in ({}, {"tools": []}, {"tools": None}, None, [1], {"tool_choice": "auto"}):
            self.assertFalse(mp.offers_tools(req), req)


class MalformedRetry(Base):
    """A broken tool call is asked again while nothing has gone to the client."""
    PRICES = '{"m": [100, 200, 10]}'

    def setUp(self):
        super().setUp()
        self.start({"BUDGET_PRICES": self.PRICES})

    def answers(self, *replies):
        """The upstream answers the n-th call with replies[n] (bytes = an SSE
        body, a callable = itself), the last one again after that."""
        def reply(h, req):
            r = replies[min(len(Upstream.seen), len(replies)) - 1]
            if callable(r):
                return r(h, req)
            h.start_stream()
            h.wfile.write(r)
        Upstream.reply = reply

    def ask(self, stream=True, **extra):
        body = {"model": "m", "messages": [{"role": "user", "content": BODY_CANARY}], "tools": TOOLS, **extra}
        if stream:
            body.update(stream=True, stream_options={"include_usage": True})   # as OpenClaw asks
        return self.call("POST", "/v1/chat/completions", body)

    def price(self, usage):
        return mp.cost_usd((100, 200, 10), *usage)

    def test_a_broken_answer_is_replaced_by_the_next_one(self):
        self.answers(fixture("bad-dsml-2.sse"), fixture("good-tool-call.sse"))
        resp, data = self.ask()
        self.assertEqual((resp.status, data), (200, fixture("good-tool-call.sse")))   # byte for byte
        self.assertEqual(len(Upstream.seen), 2)
        self.assertEqual(Upstream.seen[0]["body"], Upstream.seen[1]["body"])          # the same request again
        good, bad = fixture_usage("good-tool-call.sse"), fixture_usage("bad-dsml-2.sse")
        total = self.usage()["total"]
        self.assertEqual((total["requests"], total["prompt_tokens"], total["completion_tokens"],
                          total["cached_prompt_tokens"]), (1, good[0], good[1], good[2]))      # billed once
        self.assertAlmostEqual(total["usd"], self.price(good), places=8)
        self.assertEqual((total["malformed_detected"], total["malformed_retried"], total["malformed_forwarded"]),
                         (1, 1, 0))
        self.assertEqual((total["discarded_requests"], total["discarded_prompt_tokens"],
                          total["discarded_completion_tokens"], total["discarded_cached_prompt_tokens"]),
                         (1, bad[0], bad[1], bad[2]))
        self.assertAlmostEqual(total["discarded_usd"], self.price(bad), places=8)
        self.assertEqual(self.usage()["day"]["malformed_retried"], 1)
        self.assertTrue(any("malformed answer (model markup in a tool call); asking again (1 of 2)" in line
                            for line in LOGS), LOGS)

    def test_every_recorded_broken_answer_is_retried(self):
        for name in BAD:
            Upstream.seen = []
            self.answers(fixture(name), fixture("good-tool-call.sse"))
            self.assertEqual(self.ask()[1], fixture("good-tool-call.sse"), name)
            self.assertEqual(len(Upstream.seen), 2, name)
        self.assertEqual(self.usage()["total"]["malformed_retried"], len(BAD))

    def test_when_every_attempt_is_broken_the_last_is_passed_on(self):
        self.answers(fixture("bad-dsml-1.sse"), fixture("bad-dsml-3.sse"), fixture("bad-garbage-args.sse"))
        resp, data = self.ask()
        self.assertEqual((resp.status, data), (200, fixture("bad-garbage-args.sse")))
        self.assertEqual(len(Upstream.seen), 3)                                   # 1 + MODELPROXY_MALFORMED_RETRIES
        total, last = self.usage()["total"], fixture_usage("bad-garbage-args.sse")
        self.assertEqual((total["requests"], total["prompt_tokens"]), (1, last[0]))
        self.assertEqual((total["malformed_detected"], total["malformed_retried"], total["malformed_forwarded"],
                          total["discarded_requests"]), (3, 2, 1, 2))
        self.assertEqual(total["discarded_prompt_tokens"],
                         fixture_usage("bad-dsml-1.sse")[0] + fixture_usage("bad-dsml-3.sse")[0])
        self.assertTrue(any("passed on" in line for line in LOGS))

    def test_the_number_of_retries_is_a_setting_and_0_switches_the_check_off(self):
        self.start({"BUDGET_PRICES": self.PRICES, "MODELPROXY_MALFORMED_RETRIES": "1"})
        self.answers(fixture("bad-dsml-1.sse"))
        self.assertEqual(self.ask()[1], fixture("bad-dsml-1.sse"))
        self.assertEqual(len(Upstream.seen), 2)
        self.start({"BUDGET_PRICES": self.PRICES, "MODELPROXY_MALFORMED_RETRIES": "0"})
        Upstream.seen = []
        self.assertEqual(self.ask()[1], fixture("bad-dsml-1.sse"))
        self.assertEqual(len(Upstream.seen), 1)
        with self.assertRaises(mp.ConfigError):
            mp.Config({"MODEL_BASE_URL": "http://x", "MODELPROXY_MALFORMED_RETRIES": "-1"})

    def test_a_stream_cut_off_inside_a_tool_call_is_asked_again(self):
        def cut(h, req):
            h.start_stream()
            h.wfile.write(sse(*GOOD_CALL[:2], done=False))   # then the connection closes: no finish, no [DONE]
        self.answers(cut, sse(*GOOD_CALL, USAGE_CHUNK))
        self.assertEqual(self.ask()[1], sse(*GOOD_CALL, USAGE_CHUNK))
        self.assertEqual(len(Upstream.seen), 2)
        total = self.usage()["total"]
        self.assertEqual((total["malformed_retried"], total["discarded_requests"]), (1, 1))

    def test_length_cut_inside_a_tool_call_is_passed_on_not_asked_again(self):
        cut = sse(*GOOD_CALL[:2], chunk({}, "length"), USAGE_CHUNK)
        self.answers(cut, sse(*GOOD_CALL, USAGE_CHUNK))
        self.assertEqual(self.ask()[1], cut)
        self.assertEqual(len(Upstream.seen), 1)
        total = self.usage()["total"]
        self.assertEqual((total["malformed_detected"], total["malformed_retried"], total["malformed_forwarded"]), (1, 0, 1))

    def test_proxy_added_usage_option_is_still_hidden_after_a_retry(self):
        self.answers(fixture("bad-dsml-4.sse"), fixture("good-tool-call.sse"))
        resp, data = self.call("POST", "/v1/chat/completions", {"model": "m", "stream": True, "tools": TOOLS,
                                                                  "messages": []})
        self.assertNotIn(b'"choices":[],"usage"', data)
        self.assertTrue(data.endswith(b"data: [DONE]\n\n"))
        self.assertEqual(self.usage()["total"]["prompt_tokens"], fixture_usage("good-tool-call.sse")[0])

    def test_a_good_text_answer_passes_unchanged_and_costs_no_retry(self):
        self.answers(fixture("good-text.sse"))
        self.assertEqual(self.ask()[1], fixture("good-text.sse"))
        total = self.usage()["total"]
        self.assertEqual((len(Upstream.seen), total["malformed_detected"], total["discarded_requests"]), (1, 0, 0))

    def test_long_text_streams_before_the_upstream_finishes_and_is_then_never_retried(self):
        go, state = threading.Event(), {}
        long_text = "word " * 130                                    # 650 characters: past the hold limit

        def reply(h, req):
            h.start_stream()
            h.wfile.write(sse(chunk({"role": "assistant", "content": long_text}), done=False))
            h.wfile.flush()
            state["released_by_client"] = go.wait(5)
            # then a broken tool call: too late to ask again, it is passed on and counted
            h.wfile.write(sse(GOOD_CALL[0], chunk({"tool_calls": [{"index": 0, "function": {
                "arguments": '{"command": "ls</｜DSML｜>"}'}}]}), chunk({}, "tool_calls"), USAGE_CHUNK))
        Upstream.reply = reply
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=json.dumps(
            {"model": "m", "stream": True, "tools": TOOLS, "messages": []}).encode())
        resp = conn.getresponse()
        self.assertIn(b"word word", resp.readline())
        self.assertFalse(go.is_set())
        go.set()
        rest = resp.read()
        conn.close()
        wait_logs(2)
        self.assertIn(b"DSML", rest)
        self.assertTrue(state["released_by_client"], "the proxy held long text back")
        self.assertEqual(len(Upstream.seen), 1)
        total = self.usage()["total"]
        self.assertEqual((total["malformed_detected"], total["malformed_retried"], total["malformed_forwarded"]),
                         (1, 0, 1))
        self.assertTrue(any("passed on (it had started to stream)" in line for line in LOGS))

    def test_short_text_is_held_until_the_answer_ends(self):
        go, state = threading.Event(), {}

        def reply(h, req):
            h.start_stream()
            h.wfile.write(sse(chunk({"content": "I'll check."}), done=False))
            h.wfile.flush()
            state["waited"] = go.wait(0.5)
            h.wfile.write(sse(*GOOD_CALL, USAGE_CHUNK))
        Upstream.reply = reply
        t0 = time.monotonic()
        _, data = self.ask()
        self.assertFalse(state["waited"])           # nothing reached the client while the upstream paused
        self.assertGreaterEqual(time.monotonic() - t0, 0.45)
        self.assertEqual(data, sse(chunk({"content": "I'll check."}), *GOOD_CALL, USAGE_CHUNK))

    def test_requests_without_tools_are_not_checked_or_held(self):
        self.answers(fixture("bad-dsml-1.sse"))
        _, data = self.call("POST", "/v1/chat/completions", {"model": "m", "stream": True, "messages": [],
                                                               "stream_options": {"include_usage": True}})
        self.assertEqual(data, fixture("bad-dsml-1.sse"))
        self.assertEqual((len(Upstream.seen), self.usage()["total"]["malformed_detected"]), (1, 0))

    def test_whole_json_answers_are_checked_too(self):
        bad = tool_json('{"command": "ls" HHHH', usage={"prompt_tokens": 1000, "completion_tokens": 50})
        good = tool_json('{"command": "ls"}')
        self.answers(lambda h, req: h.send_bytes(200, bad), lambda h, req: h.send_bytes(200, good))
        resp, data = self.ask(stream=False)
        self.assertEqual((resp.status, data, resp.getheader("Content-Length")), (200, good, str(len(good))))
        total = self.usage()["total"]
        self.assertEqual((total["prompt_tokens"], total["discarded_prompt_tokens"], total["malformed_retried"]),
                         (11, 1000, 1))

    def test_if_asking_again_fails_the_broken_answer_is_passed_on(self):
        self.answers(fixture("bad-dsml-2.sse"), lambda h, req: h.send_bytes(500, b'{"error":{"message":"x"}}'))
        resp, data = self.ask()
        self.assertEqual((resp.status, data), (200, fixture("bad-dsml-2.sse")))
        self.assertEqual(len(Upstream.seen), 2)
        total = self.usage()["total"]
        self.assertEqual((total["requests"], total["prompt_tokens"], total["discarded_requests"],
                          total["malformed_forwarded"]), (1, fixture_usage("bad-dsml-2.sse")[0], 0, 1))

    def test_a_busy_upstream_inside_a_retry_is_waited_for(self):
        self.start({"BUDGET_PRICES": self.PRICES, "MODELPROXY_UPSTREAM_RETRIES": "2"})
        self.answers(fixture("bad-dsml-2.sse"), lambda h, req: h.send_bytes(429, b"{}", extra=[("Retry-After", "1")]),
                     fixture("good-tool-call.sse"))
        self.assertEqual(self.ask()[1], fixture("good-tool-call.sse"))
        self.assertEqual((len(Upstream.seen), self.slept), (3, [1.0]))

    def slow(self, raw, pause):
        def reply(h, req):
            h.start_stream()
            h.wfile.write(raw[:200])
            h.wfile.flush()
            time.sleep(pause)
            h.wfile.write(raw[200:])
        return reply

    def test_a_held_stream_sends_keepalive_comments_and_still_asks_again(self):
        self.cfg.keepalive_seconds = 0.1
        self.answers(self.slow(fixture("bad-dsml-2.sse"), 0.6), fixture("good-tool-call.sse"))
        resp, data = self.ask()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Transfer-Encoding"), "chunked")
        comments = data.count(b": keepalive\n\n")
        self.assertGreaterEqual(comments, 2)
        self.assertEqual(data.replace(b": keepalive\n\n", b""), fixture("good-tool-call.sse"))
        self.assertTrue(data.startswith(b": keepalive"))      # nothing of the broken answer went out
        self.assertEqual(len(Upstream.seen), 2)

    def test_after_a_keepalive_a_failed_retry_still_gives_the_broken_answer(self):
        self.cfg.keepalive_seconds = 0.1
        self.answers(self.slow(fixture("bad-dsml-2.sse"), 0.4), lambda h, req: h.send_bytes(503, b"{}"))
        resp, data = self.ask()
        self.assertEqual(resp.status, 200)
        self.assertIn(b": keepalive", data)
        self.assertEqual(data.replace(b": keepalive\n\n", b""), fixture("bad-dsml-2.sse"))

    def test_no_keepalive_inside_a_stream_that_passes_through(self):
        self.cfg.keepalive_seconds = 0.1
        self.start({"BUDGET_PRICES": self.PRICES, "MODELPROXY_MALFORMED_RETRIES": "1"})
        self.cfg.keepalive_seconds = 0.1
        self.answers(fixture("bad-dsml-2.sse"), self.slow(fixture("good-tool-call.sse"), 0.5))
        _, data = self.ask()
        while data.startswith(b": keepalive\n\n"):              # (one may come while the first attempt is held)
            data = data[len(b": keepalive\n\n"):]
        self.assertEqual(data, fixture("good-tool-call.sse"))   # the last attempt streams: no comment inside it

    def test_counters_survive_a_restart_and_old_files_load(self):
        self.answers(fixture("bad-dsml-2.sse"), fixture("good-tool-call.sse"))
        self.ask()
        self.start({"BUDGET_PRICES": self.PRICES})
        self.assertEqual(self.usage()["total"]["malformed_retried"], 1)
        path = os.path.join(self.tmp.name, "usage.json")
        with open(path, "w", encoding="utf-8") as f:      # a file from before these counters
            json.dump({"version": 1, "total": {"requests": 2, "prompt_tokens": 5}, "days": {}}, f)
        self.start({"BUDGET_PRICES": self.PRICES})
        total = self.usage()["total"]
        self.assertEqual((total["requests"], total["malformed_detected"], total["discarded_usd"]), (2, 0, 0.0))

    def test_room_caps_count_only_the_answer_passed_on(self):
        good = fixture_usage("good-tool-call.sse")
        self.start({"BUDGET_PRICES": self.PRICES, "BUDGET_TOKENS_PER_DAY": str(good[0] + good[1] + 1)})
        self.answers(fixture("bad-dsml-2.sse"), fixture("good-tool-call.sse"))
        self.ask()
        self.assertIsNone(self.usage()["exceeded"])


class GatewayMalformed(GatewayBase):
    PRICES = {"m": [100, 200, 10], "big": [1000, 2000]}

    def answers(self, *replies):
        MalformedRetry.answers(self, *replies)

    def ask_tools(self, who=None):
        return self.ask(who, stream=True, tools=TOOLS, stream_options={"include_usage": True})

    def test_the_tenant_pays_for_the_answer_it_gets_and_the_platform_counts_the_rest(self):
        table = self.table()
        table["tenants"][0]["usd_per_day"] = 1
        self.put(table)
        self.answers(fixture("bad-dsml-3.sse"), fixture("good-tool-call.sse"))
        resp, data = self.ask_tools()
        self.assertEqual((resp.status, data), (200, fixture("good-tool-call.sse")))
        self.assertEqual(resp.getheader(mp.CHECKED_HEADER), "checked")
        good, bad = fixture_usage("good-tool-call.sse"), fixture_usage("bad-dsml-3.sse")
        usd_good, usd_bad = mp.cost_usd((100, 200, 10), *good), mp.cost_usd((100, 200, 10), *bad)
        rep = self.report()
        alice, platform = rep["tenants"][self.ALICE]["day"], rep["platform"]["day"]
        self.assertEqual((alice["requests"], alice["prompt_tokens"]), (1, good[0]))
        self.assertAlmostEqual(alice["usd"], usd_good, places=8)                 # never the thrown-away attempt
        self.assertAlmostEqual(rep["tenants"][self.ALICE]["remaining_usd"], 1 - usd_good, places=6)
        for row in (alice, platform):
            self.assertEqual((row["malformed_detected"], row["malformed_retried"], row["malformed_forwarded"],
                              row["discarded_prompt_tokens"]), (1, 1, 0, bad[0]))
            self.assertAlmostEqual(row["discarded_usd"], usd_bad, places=8)
        self.assertAlmostEqual(platform["usd"], usd_good, places=8)
        self.assertAlmostEqual(rep["platform"]["remaining_usd"], 100 - usd_good - usd_bad, places=6)
        self.assertEqual(rep["platform"]["days"][rep["today"]]["malformed_retried"], 1)

    def test_thrown_away_attempts_count_against_the_platform_cap(self):
        bad = fixture_usage("bad-dsml-3.sse")
        usd_bad = mp.cost_usd((100, 200, 10), *bad)
        good = mp.cost_usd((100, 200, 10), *fixture_usage("good-tool-call.sse"))
        self.put(self.table(platform_usd_per_day=good + usd_bad / 2,
                            tenants=[{**self.table()["tenants"][1], "usd_per_day": 100}]))
        self.answers(fixture("bad-dsml-3.sse"), fixture("good-tool-call.sse"))
        self.assertEqual(self.ask_tools(self.BOB)[0].status, 200)
        self.assertTrue(self.report()["platform"]["exceeded"])
        self.assertEqual(self.code(self.ask(self.BOB)), (400, "allowance_exhausted"))

    def test_counters_survive_a_restart(self):
        self.answers(fixture("bad-dsml-3.sse"), fixture("good-tool-call.sse"))
        self.ask_tools()
        self.start({})
        self.assertEqual(self.report()["tenants"][self.ALICE]["day"]["malformed_retried"], 1)


class RoomBehindGatewayMalformed(RoomProxyBehindGateway):
    PRICES = {"m": [100, 200, 10], "big": [1000, 2000]}

    def test_only_the_gateway_asks_again(self):
        table = self.table()
        table["tenants"][0]["usd_per_day"] = 1
        self.put(table)
        MalformedRetry.answers(self, fixture("bad-dsml-1.sse"))      # every attempt broken
        resp, data = self.call("POST", "/v1/chat/completions", {
            "model": "m", "stream": True, "stream_options": {"include_usage": True}, "tools": TOOLS, "messages": []})
        wait_logs(2)                                                 # the room's line and the gateway's
        self.assertEqual((resp.status, data), (200, fixture("bad-dsml-1.sse")))
        self.assertIsNone(resp.getheader(mp.CHECKED_HEADER))       # the mark stays between the two proxies
        self.assertEqual(len(Upstream.seen), 3)                      # not 3 x 3
        self.assertEqual(self.usage()["total"]["malformed_detected"], 0)    # the room proxy did not check again
        self.assertEqual(self.report()["tenants"][self.ALICE]["day"]["malformed_forwarded"], 1)


# ---- degenerate output, request records, time limits ------------------------------------
def call_with_args(arguments, stream=True):
    """A tool-call answer whose arguments are `arguments` (a dict, JSON-encoded)."""
    text = json.dumps(arguments, ensure_ascii=False)
    return sse(*GOOD_CALL[:1], chunk({"tool_calls": [{"index": 0, "function": {"arguments": text}}]}),
               chunk({}, "tool_calls"), USAGE_CHUNK)


class DegenerateChecks(unittest.TestCase):
    LEGIT = ["ls -la | grep foo && echo done", "A" * 120 + "/8BAQ==" + base64.b64encode(os.urandom(90)).decode(),
             "0x" + "0" * 40, "deadbeef" * 8, "https://example.com/a/b/c?x=1&y=2#frag" * 3,
             "a,b,c\n" + "1,2,3\n" * 40, "| a | b |\n|---|---|\n" + "| 1 | 2 |\n" * 30, "-" * 200, "=" * 200,
             "#" * 120, "\n" * 300, " " * 500, "使用Python编写程序然后运行它", "مرحبا بالعالم هذا نص عربي",
             "😀" * 30, "Pythonでプログラムを書く", "null," * 40, "NaN, " * 40, "ghp_" + "x" * 36, "求x的α次方",
             "Привет мир, hello world", "for i in range(10):\n    print(i)\n" * 3, "ababab", "na na na na hey hey"]
    BAD = {"H" * 80: "degenerate repetition in a tool call",
           "the " * 20: "degenerate repetition in a tool call",
           "My output is getting corrupted. " * 16: "degenerate repetition in a tool call",
           "ok abcабв中文字abc ok": "mixed-script garbage in a tool call",
           "xyzЖЖЖ漢字okay": "mixed-script garbage in a tool call"}

    def test_ordinary_text_passes(self):
        for s in self.LEGIT:
            self.assertIsNone(mp.degenerate(s), s[:60])
            self.assertIsNone(check_stream(call_with_args({"command": s})).problem(stream=True), s[:60])

    def test_degenerate_text_is_found_in_any_argument(self):
        for s, why in self.BAD.items():
            self.assertEqual(mp.degenerate(s), why, s[:40])
            self.assertEqual(check_stream(call_with_args({"path": "/tmp/x", "content": {"lines": [s]}})).problem(
                stream=True), why, s[:40])

    def test_recorded_garbage_is_still_found(self):
        self.assertEqual(check_stream(fixture("bad-garbage-args.sse")).problem(stream=True),
                         "tool-call arguments are not valid JSON")

    def test_long_arguments_are_checked_quickly(self):
        big = "".join(chr(97 + (i * 7919) % 26) + (" " if i % 7 == 0 else "") for i in range(200000))
        t0 = time.monotonic()
        self.assertIsNone(mp.degenerate(big))
        self.assertLess(time.monotonic() - t0, 1.0)


class DegenerateRetry(MalformedRetry):
    """Only the new case; the rest of MalformedRetry runs once, above."""

    def test_a_looping_tool_call_is_asked_again(self):
        self.answers(call_with_args({"command": "echo " + "the " * 30}), fixture("good-tool-call.sse"))
        self.assertEqual(self.ask()[1], fixture("good-tool-call.sse"))
        self.assertEqual(len(Upstream.seen), 2)
        self.assertTrue(any("malformed answer (degenerate repetition in a tool call); asking again" in line
                            for line in LOGS), LOGS)


for _name in [n for n in vars(MalformedRetry) if n.startswith("test_")]:
    setattr(DegenerateRetry, _name, None)


class RequestRecords(Base):
    env = {"BUDGET_PRICES": '{"m": [100, 200, 10]}'}

    def records(self, query=""):
        return json.loads(self.call("GET", "/usage/requests" + query)[1])["requests"]

    def test_one_line_per_request_without_content(self):
        self.chat()
        self.chat(stream=True)
        recs = self.records()
        self.assertEqual(len(recs), 2)
        r = recs[0]
        self.assertEqual((r["status"], r["route"], r["model"], r["prompt_tokens"], r["completion_tokens"]),
                         (200, "chat/completions", "m", 11, 7))
        self.assertAlmostEqual(r["usd"], mp.cost_usd((100, 200, 10), 11, 7), places=8)
        self.assertEqual(r["ts"], self.now[0])
        for key in ("duration_s", "ttfb_s", "estimated", "malformed", "malformed_retried", "discarded_usd",
                    "first_byte_timeouts", "stalled", "interrupted", "error", "stream", "cached_prompt_tokens"):
            self.assertIn(key, r)
        self.assertIsNotNone(r["ttfb_s"])
        with open(os.path.join(self.tmp.name, "requests-2026-11-22.jsonl"), encoding="utf-8") as f:
            stored = f.read()
        for secret in self.secrets:
            self.assertNotIn(secret, stored)
        self.assertEqual(os.stat(os.path.join(self.tmp.name, "requests-2026-11-22.jsonl")).st_mode & 0o777, 0o600)

    def test_since_and_limit(self):
        for i in range(5):
            self.now[0] += 10
            self.chat()
        self.assertEqual(len(self.records("?limit=2")), 2)
        self.assertEqual(self.records("?limit=2")[-1]["ts"], self.now[0])        # the newest
        self.assertEqual(len(self.records(f"?since={self.now[0] - 15}")), 2)
        self.assertEqual(len(self.records("?since=nonsense&limit=x")), 5)

    def test_old_days_are_dropped_and_a_full_day_stops_growing(self):
        self.start({**self.env, "MODELPROXY_REQUEST_LOG_DAYS": "2"})
        for _ in range(3):
            self.chat()
            self.now[0] += 86400
        self.chat()
        names = sorted(n for n in os.listdir(self.tmp.name) if n.startswith("requests-"))
        self.assertEqual(names, ["requests-2026-11-24.jsonl", "requests-2026-11-25.jsonl"])
        self.start({**self.env, "MODELPROXY_REQUEST_LOG_MB": "0.0005"})        # about 500 bytes
        for _ in range(5):
            self.chat()
        self.assertLess(os.path.getsize(os.path.join(self.tmp.name, "requests-2026-11-25.jsonl")), 600)
        self.assertEqual(sum("request log for 2026-11-25 is full" in line for line in LOGS), 1)

    def test_a_retried_broken_answer_is_on_the_line(self):
        MalformedRetry.answers(self, fixture("bad-dsml-2.sse"), fixture("good-tool-call.sse"))
        self.call("POST", "/v1/chat/completions", {"model": "m", "stream": True, "tools": TOOLS, "messages": [],
                                                    "stream_options": {"include_usage": True}})
        r = self.records()[-1]
        self.assertEqual((r["malformed"], r["malformed_retried"]), (1, 1))
        self.assertAlmostEqual(r["discarded_usd"], mp.cost_usd((100, 200, 10), *fixture_usage("bad-dsml-2.sse")),
                               places=8)


class GatewayRequestRecords(GatewayBase):
    def test_per_tenant_records_on_the_admin_listener_only(self):
        self.ask()
        self.ask(self.BOB)
        recs = json.loads(self.admin_call("GET", "/admin/requests")[1])["requests"]
        self.assertEqual([r["tenant"] for r in recs], [self.ALICE, self.BOB])
        bob = json.loads(self.admin_call("GET", f"/admin/requests?tenant={self.BOB}")[1])["requests"]
        self.assertEqual(len(bob), 1)
        self.assertAlmostEqual(bob[0]["usd"], mp.cost_usd((100, 200, 100), 11, 7), places=8)
        self.assertEqual(self.call("GET", "/admin/requests")[0].status, 404)       # not on the public listener
        self.start({"MODELPROXY_ADMIN_TOKEN": "t0ken-for-admin"})
        self.assertEqual(self.admin_call("GET", "/admin/requests")[0].status, 401)


class Timeouts(Base):
    env = {"MODELPROXY_FIRST_BYTE_TIMEOUT": "0.3", "MODELPROXY_STALL_TIMEOUT": "0.3"}

    def answers(self, *replies):
        MalformedRetry.answers(self, *replies)

    @staticmethod
    def late(delay, raw=None, head_first=False):
        """Answers after `delay` seconds (the head too, unless head_first)."""
        def reply(h, req):
            if head_first:
                h.start_stream()
                h.wfile.flush()
            time.sleep(delay)
            try:
                if not head_first:
                    h.start_stream()
                h.wfile.write(raw or sse(chunk({"content": "hi"}), USAGE_CHUNK))
            except OSError:
                pass
        return reply

    def test_defaults(self):
        cfg = mp.Config({"MODEL_BASE_URL": "http://x"})
        self.assertEqual((cfg.first_byte_timeout, cfg.stall_timeout, cfg.retry_max_wait), (30.0, 60.0, 10.0))
        behind = mp.Config({"MODEL_BASE_URL": "http://gw/v1", "MODEL_API_KEY": new_token("bq4x7k2m9pa")})
        self.assertEqual((behind.upstream_is_gateway, behind.first_byte_timeout, behind.stall_timeout), (True, 0, 0))
        explicit = mp.Config({"MODEL_BASE_URL": "http://gw/v1", "MODEL_API_KEY": new_token("bq4x7k2m9pa"),
                              "MODELPROXY_FIRST_BYTE_TIMEOUT": "20"})
        self.assertEqual(explicit.first_byte_timeout, 20)
        gw = mp.Config({"MODEL_BASE_URL": "http://x", "MODELPROXY_MODE": "gateway"})
        self.assertEqual((gw.first_byte_timeout, gw.stall_timeout), (30.0, 60.0))
        for bad in ("-1", "x", "inf"):
            with self.assertRaises(mp.ConfigError):
                mp.Config({"MODEL_BASE_URL": "http://x", "MODELPROXY_FIRST_BYTE_TIMEOUT": bad})

    def test_no_head_in_time_is_asked_again_once(self):
        self.answers(self.late(1.0), self.late(0))
        t0 = time.monotonic()
        resp, data = self.chat(stream=True)
        self.assertEqual((resp.status, data), (200, sse(chunk({"content": "hi"}))))
        self.assertLess(time.monotonic() - t0, 0.9)
        self.assertEqual(len(Upstream.seen), 2)
        u = self.usage()["total"]
        self.assertEqual((u["requests"], u["first_byte_timeouts"], u["first_byte_retries"], u["first_byte_failures"]),
                         (1, 1, 1, 0))
        self.assertTrue(any("no first byte in 0.3s; asking again (waiting up to 0.45s)" in line for line in LOGS), LOGS)
        self.assertTrue(any("ttfb=" in line and "first-byte-retries=1" in line for line in LOGS), LOGS)

    def test_a_head_without_any_data_counts_as_no_first_byte(self):
        self.answers(self.late(1.0, head_first=True), self.late(0))
        resp, data = self.chat(stream=True)
        self.assertEqual((resp.status, data), (200, sse(chunk({"content": "hi"}))))
        self.assertEqual(self.usage()["total"]["first_byte_retries"], 1)

    def test_twice_too_late_is_a_504_the_user_can_read(self):
        self.answers(self.late(1.0))
        resp, data = self.chat(stream=True)
        err = json.loads(data)["error"]
        self.assertEqual((resp.status, err["code"]), (504, "upstream_timeout"))
        self.assertIn("did not start to answer in time", err["message"])
        self.assertEqual(len(Upstream.seen), 2)
        u = self.usage()["total"]
        self.assertEqual((u["requests"], u["first_byte_timeouts"], u["first_byte_failures"]), (0, 2, 1))
        rec = json.loads(self.call("GET", "/usage/requests")[1])["requests"][-1]
        self.assertEqual((rec["status"], rec["error"], rec["first_byte_timeouts"]), (504, "first_byte_timeout", 2))

    def test_0_switches_it_off(self):
        self.start({"MODELPROXY_FIRST_BYTE_TIMEOUT": "0"})
        self.answers(self.late(0.5))
        self.assertEqual(self.chat(stream=True)[0].status, 200)
        self.assertEqual(len(Upstream.seen), 1)

    def test_a_stall_after_the_answer_started_ends_it(self):
        def reply(h, req):
            h.start_stream()
            h.wfile.write(sse(chunk({"content": "The"}), done=False))
            h.wfile.flush()
            time.sleep(1.0)
            try:
                h.wfile.write(sse(chunk({"content": " end"}), USAGE_CHUNK))
            except OSError:
                pass
        Upstream.reply = reply
        t0 = time.monotonic()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=json.dumps({"model": "m", "stream": True, "messages": []}))
        resp = conn.getresponse()
        with self.assertRaises(http.client.IncompleteRead):
            resp.read()
        conn.close()
        self.assertLess(time.monotonic() - t0, 0.9)          # ended at the stall limit, not after the upstream
        wait_logs(1)
        self.assertEqual(self.usage()["total"]["stalls"], 1)
        self.assertTrue(any("stalled (no byte for 0s)" in line or "the answer stalled" in line for line in LOGS), LOGS)
        self.assertEqual(len(Upstream.seen), 1)               # part of it had gone out: never asked again

    def test_a_held_tool_call_that_stalls_is_asked_again(self):
        def stall(h, req):
            h.start_stream()
            h.wfile.write(sse(*GOOD_CALL[:2], done=False))
            h.wfile.flush()
            time.sleep(1.0)
        self.answers(stall, fixture("good-tool-call.sse"))
        resp, data = self.call("POST", "/v1/chat/completions", {"model": "m", "stream": True, "tools": TOOLS,
                                                                  "messages": []})
        self.assertIn(b'"tool_calls"', data)
        self.assertNotIn(b'"choices":[],"usage"', data)          # the usage chunk the proxy asked for is hidden
        self.assertTrue(data.endswith(b"data: [DONE]\n\n"))
        self.assertEqual(len(Upstream.seen), 2)
        u = self.usage()["total"]
        self.assertEqual((u["stalls"], u["malformed_retried"]), (1, 1))

    def test_twice_too_late_inside_a_held_stream_ends_with_an_error_event(self):
        self.cfg.keepalive_seconds = 0.1
        self.answers(self.late(1.5, head_first=True))
        resp, data = self.call("POST", "/v1/chat/completions", {"model": "m", "stream": True, "tools": TOOLS,
                                                                  "messages": []})
        self.assertEqual(resp.status, 200)
        self.assertTrue(data.startswith(b": keepalive"))
        self.assertIn(b'"code": "upstream_timeout"', data)
        self.assertEqual(len(Upstream.seen), 2)

    def test_busy_upstream_waits_are_bounded_and_logged(self):
        self.start({"MODELPROXY_UPSTREAM_RETRIES": "3"})
        UpstreamRetry.busy_then_ok(self, 99, headers=[("Retry-After", "6")])
        self.assertEqual(self.chat()[0].status, 429)
        self.assertEqual(self.slept, [6.0])                      # 6 + 6 would pass the 10 s default
        self.assertTrue(any("upstream 429; waiting 6.0s before asking again (1 of 3, 6.0s of 10s)" in line
                            for line in LOGS), LOGS)
        self.assertTrue(any("not waiting 6.0s more (over the 10s bound)" in line for line in LOGS), LOGS)


class GatewayTimeouts(GatewayBase):
    def test_the_gateway_asks_again_and_the_room_proxy_behind_it_does_not_wait_twice(self):
        self.start({"MODELPROXY_FIRST_BYTE_TIMEOUT": "0.3"})
        Upstream.reply = Timeouts.late(1.0)
        resp, data = self.ask(stream=True)
        self.assertEqual(self.code((resp, data)), (504, "upstream_timeout"))
        day = self.report()["tenants"][self.ALICE]["day"]
        self.assertEqual((day["first_byte_timeouts"], day["first_byte_failures"], day["requests"]), (2, 1, 0))
        self.assertEqual(self.report()["platform"]["day"]["first_byte_retries"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
