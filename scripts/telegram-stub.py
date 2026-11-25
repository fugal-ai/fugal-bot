"""TEST ONLY: a stand-in Telegram Bot API for scripts/hosted-test.sh.

The bot's Telegram channel talks to it instead of api.telegram.org when
render-config sets channels.telegram.apiRoot (TELEGRAM_API_ROOT, which only the
hosted-test overlay sets, to http://127.0.0.1:<port>). It answers the Bot API
the way Telegram does for a private chat, enough for OpenClaw's long polling
and replies:

  /bot<token>/getMe          the bot
  /bot<token>/getUpdates     the queued updates from `offset` on (held up to 2 s)
  /bot<token>/send*, edit*   a message object; every outgoing text is recorded
  /bot<token>/getChat        a private chat
  /bot<token>/<other>        true

and, for the test (never part of the Bot API):

  POST /test/message?from=<user id>   queue a private message with that body as text
  GET  /test/sent                     every recorded outgoing call: [{method, chat_id, text}]
  GET  /test/calls                    how often each Bot API method was called

Python stdlib only. usage: telegram-stub.py <host> <port>
"""

import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = sys.argv[1], int(sys.argv[2])
BOT = {"id": 7000000001, "is_bot": True, "first_name": "Fugal Test Bot", "username": "fugal_test_bot",
       "can_join_groups": False, "can_read_all_group_messages": False, "supports_inline_queries": False}
lock = threading.Condition()
updates: list = []
sent: list = []
calls: dict = {}
next_update = [1000]
next_message = [1]

GET_ANSWERS = {
    "getWebhookInfo": {"url": "", "has_custom_certificate": False, "pending_update_count": 0},
    "getMyCommands": [],
    "getMyName": {"name": BOT["first_name"]},
    "getMyDescription": {"description": ""},
    "getMyShortDescription": {"short_description": ""},
    "getMyDefaultAdministratorRights": {},
}


def chat(chat_id):
    return {"id": chat_id, "type": "private", "first_name": "Owner"}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fields(self, raw: bytes) -> dict:
        ctype = self.headers.get("Content-Type", "")
        try:
            if "application/json" in ctype:
                return json.loads(raw or b"{}")
            if "x-www-form-urlencoded" in ctype:
                return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
            if "multipart/form-data" in ctype:
                out = {}
                for part in raw.split(b"--" + ctype.split("boundary=")[-1].strip('"').encode()):
                    head, _, val = part.partition(b"\r\n\r\n")
                    if b'name="' in head:
                        name = head.split(b'name="')[1].split(b'"')[0].decode()
                        out[name] = val.rstrip(b"\r\n").decode("utf-8", "replace")
                return out
        except (ValueError, UnicodeDecodeError):
            pass
        return {}

    def do_GET(self):
        self.handle_any(b"")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.handle_any(self.rfile.read(n) if n else b"")

    def handle_any(self, raw):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/test/message":
            uid = int(q["from"][0])
            with lock:
                next_update[0] += 1
                next_message[0] += 1
                updates.append({"update_id": next_update[0], "message": {
                    "message_id": next_message[0], "date": int(time.time()), "chat": chat(uid),
                    "from": {"id": uid, "is_bot": False, "first_name": "Owner", "language_code": "en"},
                    "text": raw.decode("utf-8")}})
                lock.notify_all()
            return self._json({"ok": True, "update_id": next_update[0]})
        if url.path == "/test/sent":
            with lock:
                return self._json(sent)
        if url.path == "/test/calls":
            with lock:
                return self._json(calls)
        parts = url.path.split("/")
        if len(parts) != 3 or not parts[1].startswith("bot"):
            return self._json({"ok": False, "error_code": 404, "description": "Not Found"}, 404)
        method = parts[2]
        fields = {**{k: v[0] for k, v in q.items()}, **self._fields(raw)}
        with lock:
            calls[method] = calls.get(method, 0) + 1
        if method == "getMe":
            return self._json({"ok": True, "result": BOT})
        if method == "getUpdates":
            offset = int(fields.get("offset") or 0)
            wait = min(float(fields.get("timeout") or 0), 2.0)
            deadline = time.time() + wait
            with lock:
                while True:
                    ready = [u for u in updates if u["update_id"] >= offset]
                    left = deadline - time.time()
                    if ready or left <= 0:
                        break
                    lock.wait(left)
            return self._json({"ok": True, "result": ready})
        if method in GET_ANSWERS:
            return self._json({"ok": True, "result": GET_ANSWERS[method]})
        chat_id = fields.get("chat_id")
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            pass
        text = fields.get("text") if isinstance(fields.get("text"), str) else fields.get("caption")
        if method.startswith(("send", "edit", "copy", "forward")):
            with lock:
                sent.append({"method": method, "chat_id": chat_id, "text": text})
                next_message[0] += 1
                mid = int(fields.get("message_id") or next_message[0])
            if method in ("sendChatAction", "sendMessageDraft"):
                return self._json({"ok": True, "result": True})
            return self._json({"ok": True, "result": {"message_id": mid, "date": int(time.time()),
                                                      "chat": chat(chat_id if isinstance(chat_id, int) else 0),
                                                      "from": BOT, "text": text or ""}})
        if method == "getChat":
            return self._json({"ok": True, "result": chat(chat_id)})
        return self._json({"ok": True, "result": True})


ThreadingHTTPServer.daemon_threads = True
ThreadingHTTPServer((HOST, PORT), H).serve_forever()
