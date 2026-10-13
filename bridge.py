# SPDX-License-Identifier: GPL-3.0-or-later
"""MCP newline-delimited stdio transport to a local WeeChat plugin socket."""

import os
import selectors
import socket
import sys


def main():
    tcp = len(sys.argv) == 3 and sys.argv[1] == "--loopback-port"
    if not tcp and len(sys.argv) != 2:
        raise SystemExit(
            "Usage: bridge.py /path/to/bridge.sock OR --loopback-port PORT"
        )
    with socket.socket(
        socket.AF_INET if tcp else socket.AF_UNIX, socket.SOCK_STREAM
    ) as sock:
        sock.connect(("127.0.0.1", int(sys.argv[2])) if tcp else sys.argv[1])
        selector = selectors.DefaultSelector()
        selector.register(sys.stdin.buffer, selectors.EVENT_READ, "stdin")
        selector.register(sock, selectors.EVENT_READ, "socket")
        while True:
            for key, _ in selector.select():
                if key.data == "stdin":
                    data = os.read(sys.stdin.fileno(), 65536)
                    if not data:
                        return
                    sock.sendall(data)
                else:
                    data = sock.recv(65536)
                    if not data:
                        return
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
