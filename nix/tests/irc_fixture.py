# SPDX-License-Identifier: GPL-3.0-or-later
"""Tiny isolated IRC fixture; not a production IRC daemon.

Its private control socket lets VM tests send actual IRC wire messages, observe
WeeChat PRIVMSG output and deliberately cut the connection for a native reconnect.
"""

import argparse
import json
import os
import socket
import socketserver
import threading


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.clients = set()
        self.connections = 0
        self.messages = []
        self.joined_channels = set()

    def status(self):
        with self.lock:
            return {
                "connections": self.connections,
                "joined": sum(bool(client.joined) for client in self.clients),
                "messages": list(self.messages),
                "channels": sorted(self.joined_channels),
            }


STATE = State()


class IRC(socketserver.StreamRequestHandler):
    def setup(self):
        super().setup()
        self.nick = "unknown"
        self.user = False
        self.registered = False
        self.joined = set()
        with STATE.lock:
            STATE.clients.add(self)
            STATE.connections += 1

    def send(self, line):
        self.wfile.write((line + "\r\n").encode())
        self.wfile.flush()

    def handle(self):
        try:
            while raw := self.rfile.readline(8192):
                line = raw.decode().rstrip("\r\n")
                command, _, args = line.partition(" ")
                command = command.upper()
                if command == "CAP":
                    if " LS" in line:
                        self.send(":fixture CAP * LS :")
                    elif " REQ" in line:
                        self.send(":fixture CAP * NAK :")
                elif command == "NICK":
                    self.nick = args.lstrip(":")
                elif command == "USER":
                    self.user = True
                elif command == "PING":
                    self.send("PONG " + args)
                elif command == "JOIN":
                    for channel in args.split(" ")[0].lstrip(":").split(","):
                        with STATE.lock:
                            self.joined.add(channel)
                            STATE.joined_channels.add(channel)
                        self.send(f":{self.nick}!user@localhost JOIN :{channel}")
                        self.send(
                            f":fixture 353 {self.nick} = {channel} :{self.nick} tester"
                        )
                        self.send(f":fixture 366 {self.nick} {channel} :End of NAMES")
                elif command == "PRIVMSG":
                    target, _, message = args.partition(" :")
                    with STATE.lock:
                        STATE.messages.append({"target": target, "text": message})
                elif command == "QUIT":
                    return
                if self.nick != "unknown" and self.user and not self.registered:
                    self.registered = True
                    self.send(f":fixture 001 {self.nick} :Welcome")
                    self.send(f":fixture 002 {self.nick} :Your host is fixture")
                    self.send(f":fixture 003 {self.nick} :Created for this test")
                    self.send(f":fixture 004 {self.nick} fixture 1.0 io io")
                    self.send(
                        f":fixture 005 {self.nick} CHANTYPES=# CASEMAPPING=rfc1459 :supported"
                    )
                    self.send(f":fixture 422 {self.nick} :No MOTD")
        except (OSError, UnicodeError):
            return

    def finish(self):
        with STATE.lock:
            STATE.clients.discard(self)
        try:
            super().finish()
        except OSError:
            pass


class Control(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline(16384))
        with STATE.lock:
            if request["op"] == "send":
                target = request.get("target", "#test")
                text = request["text"]
                if any(char in text + target for char in "\r\n\0"):
                    raise ValueError("invalid fixture frame")
                for client in list(STATE.clients):
                    client.send(f":tester!test@localhost PRIVMSG {target} :{text}")
            elif request["op"] == "disconnect":
                for client in list(STATE.clients):
                    client.connection.shutdown(socket.SHUT_RDWR)
                    client.connection.close()
            elif request["op"] != "status":
                raise ValueError("invalid fixture operation")
        self.wfile.write(json.dumps(STATE.status()).encode() + b"\n")


class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=16667)
    parser.add_argument("--control", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    with TCPServer(("127.0.0.1", args.port), IRC) as irc:
        with UnixServer(args.control, Control) as control:
            thread = threading.Thread(target=irc.serve_forever, daemon=True)
            thread.start()
            control.serve_forever()


if __name__ == "__main__":
    main()
