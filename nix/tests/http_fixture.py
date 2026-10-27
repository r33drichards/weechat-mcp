# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTP origin on a synthetic global address inside the isolated test VM only."""

import argparse
import contextlib
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOCK = threading.Lock()
HITS = []
STATE = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    trap = False

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.serve()

    def do_POST(self):
        self.serve()

    def do_PUT(self):
        self.serve()

    def do_DELETE(self):
        self.serve()

    def serve(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with LOCK:
            HITS.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "host": self.headers.get("Host"),
                    "body": body.decode(),
                    "trap": self.trap,
                }
            )
            assert STATE is not None
            temporary = STATE.with_suffix(".new")
            temporary.write_text(json.dumps(HITS))
            temporary.replace(STATE)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:18081/trap")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/large":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            # No Content-Length: only a running byte limit can stop this body.
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                for _ in range(512):
                    self.wfile.write(b"1000\r\n" + b"x" * 4096 + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            return
        if self.path == "/slow":
            time.sleep(3)
            response = b"slow"
        else:
            response = self.command.encode() + b":" + body
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(response)


class TrapHandler(Handler):
    trap = True


def main():
    global STATE
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    STATE = args.state
    STATE.write_text("[]")
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(args.cert, args.key)
    tls = ThreadingHTTPServer(("93.184.216.34", 18443), Handler)
    tls.socket = tls_context.wrap_socket(tls.socket, server_side=True)
    threading.Thread(target=tls.serve_forever, daemon=True).start()
    with ThreadingHTTPServer(("93.184.216.34", 18080), Handler) as origin:
        with ThreadingHTTPServer(("127.0.0.1", 18081), TrapHandler) as trap:
            threading.Thread(target=trap.serve_forever, daemon=True).start()
            origin.serve_forever()


if __name__ == "__main__":
    main()
