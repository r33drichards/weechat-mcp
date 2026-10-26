# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted source-management and native HTTP adapter for managed WeeChat JS.

This module is not the JS evaluator or policy engine. Both are inside the patched
native javascript plugin. It exposes only named source files and typed internal
bridges; all WeeChat calls remain on its event loop.
"""

import hashlib
import itertools
import json
import os
import re
import secrets
import stat

from managed_http import HttpError, NativeHTTP, validate_request

MAX_SOURCE = 65536
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
TOOLS = {
    "weechat_managed_script_status": (
        "List managed source files and active generations",
        {},
    ),
    "weechat_managed_script_read": (
        "Read one managed JavaScript source file",
        {"name": "string"},
    ),
    "weechat_managed_script_write": (
        "Write managed source only; does not activate it. Empty expected_digest creates a new file.",
        {"name": "string", "source": "string", "expected_digest": "string"},
    ),
    "weechat_managed_script_validate": (
        "Validate a candidate in a restricted isolate without effects; keep current generation",
        {"name": "string", "digest": "string"},
    ),
    "weechat_managed_script_activate": (
        "Validate and atomically hot-swap one managed generation; keep last-good on failure",
        {"name": "string", "digest": "string"},
    ),
}


def enabled(env=os.environ):
    value = env.get("WEECHAT_MCP_MANAGED_JS", "0")
    if value not in ("0", "1"):
        raise ValueError("WEECHAT_MCP_MANAGED_JS must be 0 or 1")
    return value == "1"


def _name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError(
            "managed name must start with a letter/digit and contain 1-48 ASCII letters, digits, _ or -"
        )
    return name + ".js"


def _source(source):
    if not isinstance(source, str) or "\x00" in source:
        raise ValueError("source must be NUL-free UTF-8 text")
    try:
        raw = source.encode("utf-8")
    except UnicodeError as error:
        raise ValueError("source must be UTF-8") from error
    if len(raw) > MAX_SOURCE:
        raise ValueError("source exceeds 64 KiB")
    return raw, hashlib.sha256(raw).hexdigest()


def _absolute(value):
    if not value or not os.path.isabs(value) or ".." in value.split(os.sep):
        raise ValueError("managed paths must be absolute without parent traversal")
    if os.path.realpath(value) != os.path.normpath(value):
        raise ValueError("managed paths must not contain symlinks")
    return os.path.normpath(value)


class ManagedScripts:
    def __init__(self, api, env=os.environ):
        if env.get("WEECHAT_MCP_FULL_CONTROL") != "0" or api.full_control:
            raise ValueError("managed script tools require restricted MCP mode")
        self.api = api
        self.w = api.w
        self.root = _absolute(env.get("WEECHAT_MANAGED_ROOT", ""))
        for key in ("WEECHAT_MANAGED_POLICY", "WEECHAT_MANAGED_DATA"):
            path = _absolute(env.get(key, ""))
            if os.path.commonpath((path, self.root)) == self.root:
                raise ValueError("policy and trust data must be outside managed root")
        home = self.w.info_get("weechat_data_dir", "") or env.get("WEECHAT_HOME")
        if home:
            home = _absolute(home)
            if os.path.commonpath((home, self.root)) in (home, self.root):
                raise ValueError("managed sources must not overlap WeeChat home")
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.http = None
        self.hooks = []
        try:
            info = os.fstat(self.fd)
            if info.st_uid not in (0, os.getuid()) or info.st_mode & 0o077:
                raise ValueError(
                    "managed root must be private and owned by root/service"
                )
            self._native("status", {})  # Mandatory patched plugin/policy readiness.
            self.http = NativeHTTP(self.w, self._http_complete)
            for name, callback in (
                ("mcp_managed_send", "managed_send"),
                ("mcp_managed_http_validate", "managed_http_validate"),
                ("mcp_managed_http_start", "managed_http_start"),
                ("mcp_managed_http_cancel", "managed_http_cancel"),
            ):
                hook = self.w.hook_info_hashtable(
                    name, "trusted managed JS bridge", "", "", callback, ""
                )
                if not hook:
                    raise ValueError("could not register managed bridge")
                self.hooks.append(hook)
        except Exception:
            self.close()
            raise

    def _native(self, action, values):
        response = self.w.info_get_hashtable("javascript_managed_" + action, values)
        if (
            not isinstance(response, dict)
            or response.get("ok") != "1"
            or response.get("error")
        ):
            raise ValueError(
                "managed native operation failed: "
                + str(
                    response.get("error", "unavailable")
                    if isinstance(response, dict)
                    else "unavailable"
                )
            )
        return response

    def tools(self):
        return [
            {
                "name": name,
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "properties": {key: {"type": kind} for key, kind in fields.items()},
                    "required": list(fields),
                    "additionalProperties": False,
                },
            }
            for name, (description, fields) in TOOLS.items()
        ]

    def read(self, name):
        if self.fd is None:
            raise ValueError("managed source directory is closed")
        filename = _name(name)
        fd = os.open(
            filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o022
                or info.st_size > MAX_SOURCE
            ):
                raise ValueError(
                    "managed source must be a bounded private regular file"
                )
            with os.fdopen(fd, "rb", closefd=False) as source:
                raw = source.read(MAX_SOURCE + 1)
            try:
                content = raw.decode("utf-8")
            except UnicodeError as error:
                raise ValueError("managed source is not UTF-8") from error
            _, digest = _source(content)
            return {"name": name, "source": content, "digest": digest}
        finally:
            os.close(fd)

    def write(self, name, source, expected_digest):
        if self.fd is None:
            raise ValueError("managed source directory is closed")
        filename = _name(name)
        raw, digest = _source(source)
        if not isinstance(expected_digest, str) or (
            expected_digest and not DIGEST.fullmatch(expected_digest)
        ):
            raise ValueError(
                "expected_digest must be a SHA-256 digest or empty for creation"
            )
        try:
            current = self.read(name)["digest"]
        except FileNotFoundError:
            current = ""
        if current != expected_digest:
            raise ValueError("source changed; reread before writing")
        if not current:
            with os.scandir(self.fd) as entries:
                if sum(1 for _ in itertools.islice(entries, 128)) >= 128:
                    raise ValueError("managed source directory entry limit reached")
        temporary = ".pending-" + secrets.token_hex(12)
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.fd,
        )
        try:
            with os.fdopen(fd, "wb", closefd=False) as output:
                output.write(raw)
                output.flush()
                os.fsync(fd)
            # Recheck just before replacement. MCP writes are serialized on the
            # event loop; outside writers must also use atomic replacement and
            # coordinate edits. This is not a cross-process filesystem CAS.
            try:
                latest = self.read(name)["digest"]
            except FileNotFoundError:
                latest = ""
            if latest != expected_digest:
                raise ValueError("source changed; reread before writing")
            os.replace(temporary, filename, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass
        return {"name": name, "digest": digest, "activated": False}

    def call(self, tool, args):
        if self.fd is None:
            raise ValueError("managed source directory is closed")
        if (
            tool not in TOOLS
            or not isinstance(args, dict)
            or set(args) != set(TOOLS[tool][1])
        ):
            raise ValueError("unexpected managed script arguments")
        if tool.endswith("_status"):
            files = []
            # Bound metadata traversal; this tool is not an arbitrary file listing.
            with os.scandir(self.fd) as entries:
                for entry in itertools.islice(entries, 256):
                    filename = entry.name
                    if filename.endswith(".js") and NAME.fullmatch(filename[:-3]):
                        files.append(filename[:-3])
            native = self._native("status", {})
            return {
                "files": sorted(files),
                "active": json.loads(native.get("plugins", "[]")),
            }
        if tool.endswith("_read"):
            return self.read(args["name"])
        if tool.endswith("_write"):
            return self.write(args["name"], args["source"], args["expected_digest"])
        snapshot = self.read(args["name"])
        if not DIGEST.fullmatch(args["digest"]) or args["digest"] != snapshot["digest"]:
            raise ValueError("source changed; reread and validate the current digest")
        result = self._native(
            "activate" if tool.endswith("_activate") else "validate", snapshot
        )
        return {
            "name": args["name"],
            "digest": result.get("digest", snapshot["digest"]),
            "generation": result.get("generation", ""),
            "activated": tool.endswith("_activate"),
        }

    def _channel(self, buffer):
        if not isinstance(buffer, str) or not buffer.startswith("irc."):
            return None
        pointer = self.api.buffers().get(buffer)
        if (
            not pointer
            or self.w.buffer_get_string(pointer, "full_name") != buffer
            or self.w.buffer_get_string(pointer, "localvar_type") != "channel"
        ):
            return None
        # full_name is constructed by WeeChat from the owning native plugin;
        # a custom Python/JS buffer cannot self-assert the "irc." owner prefix.
        return pointer

    def send(self, values):
        buffer, text = values["buffer"], values["text"]
        pointer = self._channel(buffer)
        if not pointer:
            raise ValueError("buffer is not an exposed IRC channel")
        if (
            not isinstance(text, str)
            or len(text.encode()) > 350
            or any(ord(char) < 32 or ord(char) == 127 for char in text)
        ):
            raise ValueError("expected one line of at most 350 UTF-8 bytes")
        server = self.w.buffer_get_string(pointer, "localvar_server")
        channel = self.w.buffer_get_string(pointer, "localvar_channel")
        # This fixed native IRC signal has a semicolon-framed prefix. Derive it
        # from the live native buffer, never from source-supplied framing, and
        # ensure it names exactly the already-authorized buffer. Upstream's
        # parser requires at least two server-name characters; reject otherwise.
        if (
            not isinstance(server, str)
            or not isinstance(channel, str)
            or len(server) < 2
            or not channel
            or any(
                char == ";" or ord(char) < 33 or ord(char) == 127
                for char in server + channel
            )
            or buffer != f"irc.{server}.{channel}"
        ):
            raise ValueError("unsupported IRC destination metadata")
        # `commands: -` alone is NOT sufficient: IRC's send_unknown_commands
        # setting can still interpret /JOIN as raw protocol input. Hardcoding
        # user_message forces literal channel text and preserves leading //.
        # Text is the fifth/remainder field, so its semicolons are not framing.
        if (
            self.w.hook_signal_send(
                "irc_input_send",
                self.w.WEECHAT_HOOK_SIGNAL_STRING,
                f"{server};{channel};user_message;;{text}",
            )
            != self.w.WEECHAT_RC_OK
        ):
            raise ValueError("WeeChat rejected literal channel input")
        return {"ok": "1", "accepted": "1"}

    @staticmethod
    def http_validate(values):
        return {
            "ok": "1",
            **validate_request(values["method"], values["url"], values["body"]),
        }

    def http_start(self, values):
        assert self.http is not None
        self.http.request(
            int(values["id"]), values["method"], values["url"], values["body"]
        )
        return {"ok": "1"}

    def http_cancel(self, values):
        assert self.http is not None
        self.http.cancel(int(values["id"]))
        return {"ok": "1"}

    def _http_complete(self, response):
        # Native pending records own script identity and generation. Never infer
        # authority from a script-provided name in a completion callback.
        self.w.info_get_hashtable(
            "javascript_managed_http_complete",
            {
                "id": str(response["id"]),
                "response_json": json.dumps(response, separators=(",", ":")),
            },
        )

    def message(self, buffer, tags, nick, text):
        if not all(isinstance(value, str) for value in (buffer, tags, nick, text)):
            return
        tags = set(tags.split(","))
        if "irc_privmsg" not in tags or "self_msg" in tags or len(text.encode()) > 4096:
            return
        if not self._channel(buffer):
            return
        nick = next((tag[5:] for tag in tags if tag.startswith("nick_")), nick)
        self.w.info_get_hashtable(
            "javascript_managed_message", {"buffer": buffer, "nick": nick, "text": text}
        )

    def close(self):
        if self.http is not None:
            # Releasing the trusted adapter must not strand callback slots in
            # the native generation. Cancellation never implies remote rollback.
            for ident, item in list(getattr(self.http, "pending", {}).items()):
                outcome = (
                    "not_sent"
                    if item["stage"] == "dns"
                    else "failed"
                    if item["method"] == "GET"
                    else "uncertain"
                )
                self._http_complete(
                    {"id": ident, "error": "adapter_closed", "outcome": outcome}
                )
            self.http.close()
        for hook in self.hooks:
            self.w.unhook(hook)
        self.hooks = []
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def internal_result(function, values):
    """Stable internal failure result; never expose exception bodies/credentials."""
    try:
        return function(values)
    except (ValueError, KeyError, TypeError, OSError, HttpError):
        return {"ok": "0", "error": "managed_request_rejected"}
