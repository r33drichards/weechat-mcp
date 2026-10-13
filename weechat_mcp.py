# SPDX-License-Identifier: GPL-3.0-or-later
"""WeeChat MCP plugin. All WeeChat calls run on its event loop, never threads."""

import collections
import json
import os
import socket
import uuid
from urllib.parse import quote, unquote

MAX_FRAME = 65536
SAFE_COMMANDS = {"msg", "say", "me", "names", "who", "topic"}


class BridgeProtocol:
    """Private local IPC, not MCP. WeeChat operations stay on its event loop."""

    def __init__(self, api):
        self.api = api

    def handle(self, req):
        ident = req.get("id")
        try:
            operation = req["operation"]
            if operation == "tools":
                result = self.api.tools()
            elif operation == "buffers":
                result = list(self.api.buffers())
            elif operation == "call":
                result = self.api.call(req["name"], req.get("arguments", {}))
            elif operation == "history":
                result = self.api.history(self.api.resolve(req["uri"]))
            else:
                raise ValueError("Unknown bridge operation")
            return {"id": ident, "result": result}
        except (ValueError, KeyError, TypeError) as e:
            return {"id": ident, "error": str(e)}


class WeeChatAPI:
    def __init__(self, w):
        self.w = w
        self.lines = collections.defaultdict(lambda: collections.deque(maxlen=200))
        self.evicted = collections.defaultdict(int)
        self.sequence = 0
        self.epoch = str(uuid.uuid4())

    def buffers(self):
        # Explicit scope: other channels, private messages, and core buffers are not exposed.
        allowed = set(self.w.config_get_plugin("buffers").split(","))
        result = {}
        il = self.w.infolist_get("buffer", "", "")
        try:
            while self.w.infolist_next(il):
                name = self.w.infolist_string(il, "full_name")
                if name in allowed:
                    result[name] = self.w.infolist_pointer(il, "pointer")
        finally:
            self.w.infolist_free(il)
        return result

    def commands(self, all_commands=False):
        commands = {}
        il = self.w.infolist_get("hook", "", "command")
        try:
            while self.w.infolist_next(il):
                name = self.w.infolist_string(il, "command")
                if all_commands or name in SAFE_COMMANDS:
                    commands[name] = self.w.infolist_string(il, "description")
        finally:
            self.w.infolist_free(il)
        return commands

    def tools(self):
        tools = [
            {
                "name": "weechat_buffers",
                "description": "List explicitly exposed buffers",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            }
        ]
        tools.append(
            {
                "name": "weechat_command_catalog",
                "description": "Discover registered slash commands and which are currently exposed; execution is allowlisted",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            }
        )
        for name, description in sorted(self.commands().items()):
            tools.append(
                {
                    "name": "weechat_" + name,
                    "description": description
                    + " (scoped to the selected exposed buffer)",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "buffer": {"type": "string"},
                            "arguments": {"type": "string"},
                        },
                        "required": ["buffer"],
                        "additionalProperties": False,
                    },
                }
            )
        return tools

    def call(self, name, args):
        if not isinstance(name, str):
            raise ValueError("Tool name must be a string")
        if not isinstance(args, dict):
            raise ValueError("Arguments must be an object")
        if name == "weechat_buffers":
            return list(self.buffers())
        if name == "weechat_command_catalog":
            return [
                {"command": k, "description": v, "exposed": k in SAFE_COMMANDS}
                for k, v in sorted(self.commands(True).items())
            ]
        command = name.removeprefix("weechat_")
        if command not in self.commands():
            raise ValueError("Command is not exposed")
        b = args.get("buffer")
        buffers = self.buffers()
        if b not in buffers:
            raise ValueError("Buffer is not exposed")
        value = args.get("arguments", "")
        if (
            not isinstance(value, str)
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or len(value.encode()) > 350
        ):
            raise ValueError("Expected a single line of at most 350 UTF-8 bytes")
        # Never permit command options or target overrides to escape the chosen buffer.
        if command in ("names", "who"):
            if value:
                raise ValueError(
                    "This command takes no arguments; target comes from buffer"
                )
            value = self.w.buffer_get_string(buffers[b], "localvar_channel")
        elif command == "msg":
            value = "* " + value
        elif command == "topic":
            channel = self.w.buffer_get_string(buffers[b], "localvar_channel")
            value = channel + (" " + value if value else "")
        elif command in ("say", "me") and value.startswith("-"):
            raise ValueError("Command options are not allowed")
        rc = self.w.command(buffers[b], "/" + command + (" " + value if value else ""))
        if rc != self.w.WEECHAT_RC_OK:
            raise ValueError("WeeChat rejected command")
        return {
            "accepted": True,
            "buffer": b,
            "note": "Local command accepted; not a delivery acknowledgement",
        }

    @staticmethod
    def uri(b):
        return "weechat://buffer/" + quote(b, safe="")

    def resolve(self, uri):
        if not isinstance(uri, str) or not uri.startswith("weechat://buffer/"):
            raise ValueError("Unknown resource")
        b = unquote(uri[len("weechat://buffer/") :])
        if b not in self.buffers():
            raise ValueError("Buffer is not exposed")
        return b

    def history(self, b):
        return {
            "epoch": self.epoch,
            "evicted_through": self.evicted[b],
            "lines": list(self.lines[b]),
            "retained": 200,
            "since": "plugin load; older lines not imported",
        }


clients = {}
listener = None
api = None


def send(sock, obj):
    clients[sock]["out"] += json.dumps(obj).encode() + b"\n"
    if len(clients[sock]["out"]) > 1024 * 1024:
        drop(sock)


def drop(sock):
    clients.pop(sock, None)
    sock.close()


def tick(data, remaining):
    assert listener is not None and api is not None
    try:
        while len(clients) < 16:
            sock, _ = listener.accept()
            sock.setblocking(False)
            clients[sock] = {"in": b"", "out": b"", "protocol": BridgeProtocol(api)}
    except BlockingIOError:
        pass
    for sock, state in list(clients.items()):
        try:
            try:
                chunk = sock.recv(MAX_FRAME)
                if not chunk:
                    drop(sock)
                    continue
                state["in"] += chunk
            except BlockingIOError:
                pass
            if len(state["in"]) > MAX_FRAME:
                drop(sock)
                continue
            for _ in range(20):
                if b"\n" not in state["in"]:
                    break
                line, state["in"] = state["in"].split(b"\n", 1)
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError()
                    response = state["protocol"].handle(request)
                except (ValueError, UnicodeError):
                    response = {"id": None, "error": "Invalid bridge request"}
                if response:
                    send(sock, response)
                if sock not in clients:
                    break
            if sock not in clients:
                continue
            if state["out"]:
                try:
                    state["out"] = state["out"][sock.send(state["out"]) :]
                except BlockingIOError:
                    pass
        except OSError:
            drop(sock)
    return api.w.WEECHAT_RC_OK


def printed(data, buffer, date, tags, displayed, highlight, prefix, message):
    assert api is not None
    name = api.w.buffer_get_string(buffer, "full_name")
    if name in api.buffers():
        api.sequence += 1
        if len(api.lines[name]) == api.lines[name].maxlen:
            api.evicted[name] = api.lines[name][0]["sequence"]
        api.lines[name].append(
            {
                "sequence": api.sequence,
                "date": date,
                "prefix": api.w.string_remove_color(prefix, ""),
                "nick": next(
                    (t[5:] for t in tags.split(",") if t.startswith("nick_")), ""
                ),
                "message": api.w.string_remove_color(message, ""),
            }
        )
        uri = api.uri(name)
        for sock, state in list(clients.items()):
            send(sock, {"event": "updated", "uri": uri})
    return api.w.WEECHAT_RC_OK


def shutdown():
    assert api is not None
    for sock in list(clients):
        drop(sock)
    if listener:
        listener.close()
    if owns_socket and socket_path and os.path.exists(socket_path):
        os.unlink(socket_path)
    return api.w.WEECHAT_RC_OK


socket_path = None
owns_socket = False
try:
    import weechat  # ty: ignore[unresolved-import]  # Provided by WeeChat.
except ImportError:
    weechat = None
if weechat and weechat.register(
    "mcp",
    "Robert Wendt",
    "0.1.0",
    "GPL3",
    "Scoped MCP tools and buffer subscriptions",
    "shutdown",
    "",
):
    if not weechat.config_is_set_plugin("buffers"):
        weechat.config_set_plugin("buffers", "irc.railway.#wchat")
    api = WeeChatAPI(weechat)
    directory = os.path.join(weechat.info_get("weechat_data_dir", ""), "mcp")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    tcp_port = os.environ.get("WEECHAT_MCP_LOOPBACK_PORT")
    if tcp_port is not None:
        # Explicit isolated development mode: never listen on a public interface.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", int(tcp_port)))
        endpoint = "127.0.0.1:" + str(listener.getsockname()[1])
        with open(os.path.join(directory, "loopback-port"), "w") as f:
            f.write(str(listener.getsockname()[1]))
    else:
        socket_path = os.path.join(directory, "bridge.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Fail closed if another instance owns the path; never silently replace it.
        listener.bind(socket_path)
        owns_socket = True
        os.chmod(socket_path, 0o600)
        endpoint = socket_path
    listener.listen(16)
    listener.setblocking(False)
    weechat.hook_timer(50, 0, 0, "tick", "")
    weechat.hook_print("", "", "", 1, "printed", "")
    weechat.prnt("", "MCP local socket ready: " + endpoint)
