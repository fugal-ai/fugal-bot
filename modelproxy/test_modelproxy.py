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


def wait_logs(n):
    """The proxy counts a call after the last byte has gone out; wait for its
    log line so a test never looks (or moves the clock) too early."""
    deadline = time.monotonic() + 5
    while len(LOGS) < n and time.monotonic() < deadline:
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
        logged = len(LOGS)
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
                         {"requests": 1, "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
                          "cached_prompt_tokens": 0, "usd": 0.0, "estimated_requests": 0})

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
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["usage.json"])

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
                         ["gateway-usage-2026-11-22.json", "gateway-usage-2026-11-23.json", "tenants.json"])
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
