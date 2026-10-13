# SPDX-License-Identifier: GPL-3.0-or-later
"""Small version-pinned MCP stdio client for the local wakeup experiment."""

import json
import os
import select
import subprocess
import sys
from pathlib import Path


class Client:
    def __init__(self, port):
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("bridge.py")),
                "--loopback-port",
                str(port),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.stdin = self.process.stdin
        self.stdout = self.process.stdout
        self.buffer = b""
        self.sequence = 0
        self.notifications = []
        response = self.call(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "weechat-wakeup-experiment", "version": "0.1"},
            },
        )
        self.version = response["protocolVersion"]
        if self.version not in ("2025-11-25", "2025-06-18"):
            self.close()
            raise ValueError(
                "This listener supports legacy resource subscriptions only"
            )
        self.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.stdin.flush()

    def read(self, timeout=5):
        while b"\n" not in self.buffer:
            if not select.select([self.stdout], [], [], timeout)[0]:
                return None
            chunk = os.read(self.stdout.fileno(), 65536)
            if not chunk:
                raise ConnectionError("MCP bridge closed")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def call(self, method, params=None):
        self.sequence += 1
        ident = self.sequence
        self.stdin.write(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "method": method,
                        "params": params or {},
                    }
                )
                + "\n"
            ).encode()
        )
        self.stdin.flush()
        while True:
            response = self.read()
            if response is None:
                raise TimeoutError("MCP response timed out")
            if response.get("id") == ident:
                if "error" in response:
                    raise RuntimeError(response["error"])
                return response["result"]
            self.notifications.append(response)

    def listen(self, uri):
        self.call("resources/subscribe", {"uri": uri})
        # Subscribe before catch-up read: updates during the read remain queued.
        return self.snapshot(uri)

    def snapshot(self, uri):
        result = self.call("resources/read", {"uri": uri})
        return json.loads(result["contents"][0]["text"])

    def updates(self, uri):
        while True:
            message = self.read(timeout=0)
            if message is None:
                break
            self.notifications.append(message)
        matching = any(
            n.get("method") == "notifications/resources/updated"
            and n.get("params", {}).get("uri") == uri
            for n in self.notifications
        )
        self.notifications.clear()
        return self.snapshot(uri) if matching else None

    def send(self, buffer, message):
        result = self.call(
            "tools/call",
            {
                "name": "weechat_msg",
                "arguments": {"buffer": buffer, "arguments": message},
            },
        )
        if result.get("isError"):
            raise RuntimeError(result)
        return result

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        for f in (self.process.stdin, self.process.stdout, self.process.stderr):
            if f is not None:
                f.close()
