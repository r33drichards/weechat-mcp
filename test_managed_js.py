# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from managed_js import ManagedScripts, enabled, internal_result
from test_mcp import Fake
from weechat_mcp import BridgeProtocol, WeeChatAPI


class FakeHTTP:
    def __init__(self, w, complete):
        self.complete = complete
        self.requests = []
        self.canceled = []

    def request(self, *args):
        self.requests.append(args)

    def cancel(self, ident):
        self.canceled.append(ident)

    def close(self):
        pass


class Host(Fake):
    WEECHAT_HOOK_SIGNAL_STRING = "string"

    def __init__(self):
        super().__init__()
        self.native = []
        self.hooks = []
        self.reject = False

    def info_get(self, name, arguments):
        return ""

    def info_get_hashtable(self, name, values):
        self.native.append((name, values))
        if self.reject:
            return {"error": "policy_unavailable"}
        return {
            "ok": "1",
            "plugins": "[]",
            "digest": values.get("digest", ""),
            "generation": "2",
        }

    def hook_info_hashtable(self, *args):
        self.hooks.append(args)
        return str(len(self.hooks))

    def unhook(self, hook):
        pass

    def buffer_get_string(self, ptr, name):
        if name == "localvar_server":
            return "railway"
        return super().buffer_get_string(ptr, name)

    def hook_signal_send(self, signal, kind, value):
        self.calls.append((signal, kind, value))
        return self.WEECHAT_RC_OK


class ManagedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "sources"
        self.root.mkdir(mode=0o700)
        self.policy = base / "policy.rego"
        self.policy.write_text("package example")
        self.data = base / "data.json"
        self.data.write_text("{}")
        self.env = {
            "WEECHAT_MCP_FULL_CONTROL": "0",
            "WEECHAT_MANAGED_ROOT": str(self.root),
            "WEECHAT_MANAGED_POLICY": str(self.policy),
            "WEECHAT_MANAGED_DATA": str(self.data),
        }
        self.w = Host()
        self.api = WeeChatAPI(self.w, full_control=False)
        self.http_patch = patch("managed_js.NativeHTTP", FakeHTTP)
        self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        self.manager = ManagedScripts(self.api, self.env)
        self.api.managed = self.manager
        self.addCleanup(self.manager.close)
        self.protocol = BridgeProtocol(self.api)

    def call(self, operation, **args):
        return self.api.call("weechat_managed_script_" + operation, args)

    def test_named_write_read_validate_activate_and_status(self):
        source = 'weechat.onMessage(m => weechat.send(m.buffer, "hello"));'
        write = self.call("write", name="bot", source=source, expected_digest="")
        self.assertFalse(write["activated"])
        self.assertEqual(
            self.call("read", name="bot"),
            {"name": "bot", "source": source, "digest": write["digest"]},
        )
        self.assertFalse(
            self.call("validate", name="bot", digest=write["digest"])["activated"]
        )
        self.assertTrue(
            self.call("activate", name="bot", digest=write["digest"])["activated"]
        )
        self.assertEqual(
            self.w.native[-1],
            (
                "javascript_managed_activate",
                {"name": "bot", "source": source, "digest": write["digest"]},
            ),
        )
        self.assertEqual(self.call("status"), {"files": ["bot"], "active": []})
        self.assertEqual((self.root / "bot.js").stat().st_mode & 0o777, 0o600)

    def test_status_preserves_native_timer_counters_without_effect_calls(self):
        active = [
            {
                "name": "probe",
                "generation": 7,
                "active": True,
                "digest": "a" * 64,
                "timers": {
                    "live": 0,
                    "callbacks_started": 1,
                    "callbacks_completed": 1,
                    "callbacks_failed": 0,
                },
            }
        ]
        with patch.object(
            self.w,
            "info_get_hashtable",
            return_value={"ok": "1", "plugins": json.dumps(active)},
        ) as native:
            self.assertEqual(self.call("status"), {"files": [], "active": active})
            native.assert_called_once_with("javascript_managed_status", {})
        self.assertEqual(self.w.calls, [])

    def test_source_changes_require_current_digest(self):
        first = self.call("write", name="bot", source="one", expected_digest="")
        with self.assertRaises(ValueError):
            self.call("write", name="bot", source="lost update", expected_digest="")
        second = self.call(
            "write", name="bot", source="two", expected_digest=first["digest"]
        )
        for action in ("validate", "activate"):
            with self.assertRaises(ValueError):
                self.call(action, name="bot", digest=first["digest"])
        self.assertEqual(self.call("read", name="bot")["digest"], second["digest"])

    def test_names_cannot_read_or_overwrite_policy_or_other_paths(self):
        for name in (
            "../policy",
            "../policy.rego",
            "/etc/passwd",
            "bot.js",
            ".hidden",
            "a/b",
            "a\\b",
            "x" * 49,
            "",
            "é",
        ):
            with self.subTest(name=name):
                for action in ("read", "write"):
                    args = {"name": name}
                    if action == "write":
                        args.update(source="x", expected_digest="")
                    with self.assertRaises(ValueError):
                        self.call(action, **args)
        self.assertEqual(self.policy.read_text(), "package example")

    def test_symlinks_hardlinks_and_special_files_rejected(self):
        (self.root / "symlink.js").symlink_to(self.policy)
        os.link(self.policy, self.root / "hardlink.js")
        os.mkfifo(self.root / "pipe.js")
        for name in ("symlink", "hardlink", "pipe"):
            response = self.protocol.handle(
                {
                    "id": 1,
                    "operation": "call",
                    "name": "weechat_managed_script_read",
                    "arguments": {"name": name},
                }
            )
            self.assertIn("error", response)
        self.assertEqual(self.policy.read_text(), "package example")

    def test_limits_encodings_permissions_and_unknown_tools(self):
        for source in ("x" * 65537, "\x00", "\ud800"):
            with self.assertRaises(ValueError):
                self.call("write", name="bot", source=source, expected_digest="")
        (self.root / "bad.js").write_bytes(b"\xff")
        with self.assertRaises(ValueError):
            self.call("read", name="bad")
        (self.root / "bad.js").write_text("source")
        (self.root / "bad.js").chmod(0o666)
        with self.assertRaises(ValueError):
            self.call("read", name="bad")
        for tool in ("policy", "promote", "delete", "load", "exec"):
            with self.assertRaises(ValueError):
                self.call(tool)
        with self.assertRaises(ValueError):
            self.call("read", name="bot", path="/etc/passwd")

    def test_disabled_and_full_control_startup_and_missing_native(self):
        self.assertFalse(enabled({}))
        self.assertTrue(enabled({"WEECHAT_MCP_MANAGED_JS": "1"}))
        with self.assertRaises(ValueError):
            enabled({"WEECHAT_MCP_MANAGED_JS": "true"})
        with self.assertRaises(ValueError):
            ManagedScripts(self.api, dict(self.env, WEECHAT_MCP_FULL_CONTROL="1"))
        self.w.reject = True
        with self.assertRaises(ValueError):
            ManagedScripts(self.api, self.env)

    def test_policy_path_root_symlinks_and_directory_permissions(self):
        with self.assertRaises(ValueError):
            ManagedScripts(
                self.api,
                dict(self.env, WEECHAT_MANAGED_POLICY=str(self.root / "policy.rego")),
            )
        link = Path(self.temp.name) / "alias"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            ManagedScripts(self.api, dict(self.env, WEECHAT_MANAGED_ROOT=str(link)))
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            ManagedScripts(self.api, self.env)
        self.root.chmod(0o700)

    def test_only_scope_filtered_channel_events_cross_native_bridge(self):
        before = len(self.w.native)
        self.manager.message("irc.railway.#private", "irc_privmsg", "a", "secret")
        self.manager.message("irc.railway.#wchat", "irc_privmsg,self_msg", "a", "self")
        self.manager.message("irc.railway.#wchat", "notify_private", "a", "secret")
        self.assertEqual(len(self.w.native), before)
        self.manager.message(
            "irc.railway.#wchat", "irc_privmsg,nick_alice", "@alice", "[exec];${env}"
        )
        self.assertEqual(
            self.w.native[-1],
            (
                "javascript_managed_message",
                {
                    "buffer": "irc.railway.#wchat",
                    "nick": "alice",
                    "text": "[exec];${env}",
                },
            ),
        )

    def test_non_irc_plugin_buffer_cannot_be_a_privileged_deputy(self):
        with patch.object(
            self.api, "buffers", return_value={"python.fake.channel": "p"}
        ):
            before = len(self.w.native)
            self.manager.message("python.fake.channel", "irc_privmsg", "a", "trigger")
            self.assertEqual(len(self.w.native), before)
            with self.assertRaises(ValueError):
                self.manager.send({"buffer": "python.fake.channel", "text": "trigger"})
        with patch.object(
            self.w,
            "buffer_get_string",
            side_effect=lambda pointer, key: (
                "channel" if key == "localvar_type" else "python.fake.channel"
            ),
        ):
            with self.assertRaises(ValueError):
                self.manager.send({"buffer": "irc.railway.#wchat", "text": "trigger"})

    def test_send_forces_literal_irc_user_message_and_rechecks_scope(self):
        for text in ("/exec not a command", "/JOIN #outside", "//literal;still text"):
            self.manager.send({"buffer": "irc.railway.#wchat", "text": text})
            self.assertEqual(
                self.w.calls[-1],
                ("irc_input_send", "string", f"railway;#wchat;user_message;;{text}"),
            )
        self.w.allowed = ""
        with self.assertRaises(ValueError):
            self.manager.send({"buffer": "irc.railway.#wchat", "text": "no"})

    def test_send_rejects_ambiguous_or_mismatching_native_framing(self):
        original = self.w.buffer_get_string
        for key, value in (
            ("localvar_server", "r"),
            ("localvar_server", "railway;outside"),
            ("localvar_server", "other"),
            ("localvar_channel", "#wchat;user_message"),
            ("localvar_channel", "#other"),
            ("localvar_channel", "#bad\nchannel"),
        ):
            with patch.object(
                self.w,
                "buffer_get_string",
                side_effect=lambda ptr, field: (
                    value if field == key else original(ptr, field)
                ),
            ):
                with self.assertRaises(ValueError):
                    self.manager.send({"buffer": "irc.railway.#wchat", "text": "no"})

    def test_http_internal_validation_completion_and_cancel(self):
        prepared = self.manager.http_validate(
            {"method": "POST", "url": "https://example.com/", "body": "payload"}
        )
        self.assertEqual(prepared["host"], "example.com")
        self.assertEqual(prepared["port"], "443")
        self.manager.http_start(
            {
                "id": "1",
                "method": "POST",
                "url": "https://example.com/",
                "body": "payload",
            }
        )
        self.assertEqual(
            cast(FakeHTTP, self.manager.http).requests,
            [(1, "POST", "https://example.com/", "payload")],
        )
        self.manager._http_complete(
            {"id": 1, "error": "timeout", "outcome": "uncertain"}
        )
        self.assertEqual(
            json.loads(self.w.native[-1][1]["response_json"])["outcome"], "uncertain"
        )
        self.manager.http_cancel({"id": "1"})
        self.assertEqual(cast(FakeHTTP, self.manager.http).canceled, [1])
        self.assertEqual(
            internal_result(
                self.manager.http_validate,
                {"method": "GET", "url": "http://127.0.0.1/", "body": ""},
            ),
            {"ok": "0", "error": "managed_request_rejected"},
        )

    def test_discovery_only_when_enabled_no_policy_admin(self):
        names = {tool["name"] for tool in self.api.tools()}
        self.assertIn("weechat_managed_script_activate", names)
        self.assertNotIn("weechat_managed_policy", names)
        self.api.managed = None
        self.assertFalse(
            any(tool["name"].startswith("weechat_managed") for tool in self.api.tools())
        )

    def test_new_file_count_cap_keeps_existing_updates_available(self):
        first = self.call("write", name="bot", source="one", expected_digest="")
        for number in range(127):
            (self.root / f"file{number}.js").write_text("")
        with self.assertRaises(ValueError):
            self.call("write", name="overflow", source="x", expected_digest="")
        update = self.call(
            "write", name="bot", source="two", expected_digest=first["digest"]
        )
        self.assertEqual(self.call("read", name="bot")["digest"], update["digest"])

    def test_closed_source_root_cannot_fall_back_to_cwd(self):
        self.manager.close()
        for action in ("status", "read"):
            with self.assertRaises(ValueError):
                self.call(action, **({"name": "bot"} if action == "read" else {}))


if __name__ == "__main__":
    unittest.main()
