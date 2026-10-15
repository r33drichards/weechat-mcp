# SPDX-License-Identifier: GPL-3.0-or-later
"""Default full access and explicit restricted-mode boundaries."""

import os
import unittest
from unittest.mock import patch

import weechat_mcp as plugin
from test_command_manifest import Runtime, inputs, record


class PolicyTests(unittest.TestCase):
    def test_default_explicit_opt_out_and_invalid_values(self):
        for value, enabled in [(None, True), ("1", True), ("0", False)]:
            env = {} if value is None else {"WEECHAT_MCP_FULL_CONTROL": value}
            with self.subTest(value=value), patch.dict(os.environ, env, clear=True):
                api = plugin.WeeChatAPI(Runtime())
                self.assertEqual(api.full_control, enabled)
                self.assertEqual("core.weechat" in api.buffers(), enabled)
                self.assertEqual(
                    any(t["name"].startswith("weechat_cmd_") for t in api.tools()),
                    enabled,
                )
        for value in ["", "true", "false", "01", " 0", "2"]:
            with patch.dict(os.environ, {"WEECHAT_MCP_FULL_CONTROL": value}):
                with self.assertRaisesRegex(ValueError, "must be 0 or 1"):
                    plugin.WeeChatAPI(Runtime())

    def test_full_mode_reads_dynamic_buffers_and_events_without_new_grants(self):
        runtime = Runtime()
        runtime.allowed = "irc.railway.#wchat"
        runtime.add_line("joined channel output", pointer="secret")
        with patch.dict(os.environ, {}, clear=True):
            api = plugin.WeeChatAPI(runtime)
        dynamic = "irc.railway.#private"  # A channel outside the configured list.
        self.assertIn(dynamic, api.call("weechat_buffers", {}))
        self.assertIn(dynamic, api.call("weechat_command_contexts", {}))
        self.assertEqual(api.resolve(api.uri(dynamic)), dynamic)
        self.assertEqual(
            api.buffer_data(dynamic)["lines"][0]["message"], "joined channel output"
        )
        self.assertEqual(
            api.event_history(api.uri(dynamic))["lines"][0]["message"],
            "joined channel output",
        )
        # Read eligibility creates neither native commands nor event subscriptions.
        self.assertEqual(runtime.calls, [])
        with patch.object(runtime, "buffer_get_string", return_value="private"):
            with self.assertRaisesRegex(ValueError, "IRC channel"):
                api.event_history(api.uri(dynamic))
        with self.assertRaisesRegex(ValueError, "IRC channel"):
            api.event_history(api.uri("core.weechat"))

    def test_explicit_zero_rejects_generated_commands_contexts_and_reads(self):
        runtime = Runtime()
        runtime.records.append(record("exec", "exec", "<command>"))
        full = plugin.WeeChatAPI(runtime, full_control=True)
        item = next(
            c for c in full.command_manifest()["commands"] if c["name"] == "exec"
        )
        with patch.dict(os.environ, {"WEECHAT_MCP_FULL_CONTROL": "0"}):
            restricted = plugin.WeeChatAPI(runtime)
        for name, args in [
            (item["tool_name"], inputs(item, "raw", {"arguments": "id"})),
            ("weechat_command_contexts", {}),
            ("weechat_command_manifest", {}),
            ("weechat_exec", {"buffer": "irc.railway.#wchat", "arguments": "id"}),
            ("weechat_buffers", {"buffer": "irc.railway.#private"}),
        ]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                restricted.call(name, args)
        with self.assertRaises(ValueError):
            restricted.event_history(restricted.uri("irc.railway.#private"))
        self.assertEqual(runtime.calls, [])


if __name__ == "__main__":
    unittest.main()
