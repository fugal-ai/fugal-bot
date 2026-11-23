"""TEST ONLY: a throwaway object store for scripts/smoke.sh (backup round trip).

It stands in for a presigned-URL object store on loopback: PUT /<name> stores
the body in a directory, GET /<name> returns it. Like such a store it wants a
Content-Length on a PUT (411 without one) and answers 404 for a missing
object; a name that starts with "denied" answers 403, the reply an expired
presigned URL gets. Python stdlib only.

usage: object-store.py <host> <port> <directory>
"""

import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT, ROOT = sys.argv[1], int(sys.argv[2]), sys.argv[3]
NAME = re.compile(r"^/([A-Za-z0-9._-]{1,120})(?:\?.*)?$")


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _name(self):
        m = NAME.match(self.path)
        if not m or m.group(1).startswith("."):
            self._reply(400)
            return None
        if m.group(1).startswith("denied"):
            self._reply(403, b"<Error><Code>AccessDenied</Code></Error>")
            return None
        return m.group(1)

    def do_PUT(self):
        name = self._name()
        if not name:
            return
        if self.headers.get("Content-Length") is None:
            self.close_connection = True
            return self._reply(411)
        left = int(self.headers["Content-Length"])
        tmp = os.path.join(ROOT, f".{name}.part")
        with open(tmp, "wb") as f:
            while left > 0:
                chunk = self.rfile.read(min(left, 1 << 16))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            os.unlink(tmp)
            self.close_connection = True
            return self._reply(400)
        os.replace(tmp, os.path.join(ROOT, name))
        self._reply(200)

    def do_GET(self):
        name = self._name()
        if not name:
            return
        try:
            with open(os.path.join(ROOT, name), "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return self._reply(404)
        self._reply(200, data)


if __name__ == "__main__":
    os.makedirs(ROOT, exist_ok=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
