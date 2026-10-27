# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import unittest
from unittest.mock import patch

import weechat_mcp as plugin
from native_buffer import MAX_RESPONSE_BYTES, NativeBuffers
from test_mcp import Fake

NAME = "irc.railway.#wchat"


class NativeBufferTests(unittest.TestCase):
    def setUp(self):
        self.w = Fake()
        # Existing native lines predate the adapter, without any printed hook.
        for i in range(650):
            self.w.add_line(str(i))
        self.api = plugin.WeeChatAPI(self.w, full_control=False)

    def read(self, **kwargs):
        return self.api.call("weechat_buffers", {"buffer": NAME, **kwargs})

    def test_preload_lines_pagination_order_and_no_content_cache(self):
        pages = []
        page = self.read()
        self.assertEqual(page["available"], 650)
        self.assertEqual(page["returned"], 100)
        while True:
            ids = [line["id"] for line in page["lines"]]
            self.assertEqual(ids, sorted(ids))
            pages.append(ids)
            if not page["next_cursor"]:
                break
            page = self.read(cursor=page["next_cursor"])
        self.assertEqual(
            [i for part in reversed(pages) for i in part], list(range(650))
        )
        self.assertFalse(page["has_more"])
        self.assertFalse(hasattr(self.api, "lines"))
        self.assertFalse(hasattr(self.api, "sequence"))

    def test_data_identity_survives_reload_but_process_marker_does_not_cross_pid(self):
        before = self.read()
        after = NativeBuffers(self.w).snapshot(NAME, "p")
        self.assertNotEqual(before["epoch"], after["epoch"])
        self.assertEqual(before["data_epoch"], after["data_epoch"])
        with patch.dict(
            os.environ,
            {
                "_WEECHAT_MCP_PROCESS_INSTANCE": "different-pid:11111111-1111-4111-8111-111111111111"
            },
        ):
            other_process = NativeBuffers(self.w).snapshot(NAME, "p")
            self.assertNotEqual(before["data_epoch"], other_process["data_epoch"])

    def test_new_arrival_does_not_shift_old_page(self):
        page = self.read(limit=10)
        self.w.add_line("arrived after first page")
        older = self.read(limit=10, cursor=page["next_cursor"])
        self.assertEqual([line["id"] for line in older["lines"]], list(range(630, 640)))

    def test_order_is_native_insertion_not_timestamp(self):
        self.w.add_line("backdated", date=1, displayed=0, highlight=1)
        page = self.read(limit=1)
        line = page["lines"][0]
        self.assertEqual(line["message"], "backdated")
        self.assertEqual(line["date"], 1)
        self.assertFalse(line["displayed"])
        self.assertTrue(line["highlight"])
        self.assertEqual(line["tags"], ["nick_alice", "irc_privmsg"])

    def test_merged_buffer_never_read(self):
        self.w.add_line("secret", "secret")
        self.w.nodes["p"]["lines"] = "ls"
        self.w.nodes["p"]["mixed_lines"] = "ls"
        page = self.read(limit=500)
        self.assertNotIn("secret", [row["message"] for row in page["lines"]])
        self.assertFalse(
            any(key in ("lines", "mixed_lines") for _, _, key in self.w.gets)
        )

    def test_cursor_scope_tamper_reload_and_close(self):
        cursor = self.read(limit=1)["next_cursor"]
        with self.assertRaises(ValueError):
            self.api.native.snapshot("another", "p", cursor=cursor)
        for bad in (cursor[:-4] + "AAAA", "not-base64", "x" * 4097):
            with self.assertRaises(ValueError):
                self.read(cursor=bad)
        reloaded = NativeBuffers(self.w)
        with self.assertRaises(ValueError):
            reloaded.snapshot(NAME, "p", cursor=cursor)
        with patch.object(plugin, "api", self.api):
            plugin.buffer_closing("", "buffer_closing", "p")
        with self.assertRaises(ValueError):
            self.read(cursor=cursor)

    def test_native_eviction_and_clear_expire_cursor(self):
        page = self.read(limit=500)
        self.w.evict(151)
        with self.assertRaisesRegex(ValueError, "no longer retained"):
            self.read(cursor=page["next_cursor"])
        self.w.evict(499)
        page = self.read()
        self.assertEqual(page["lines"], [])
        self.assertEqual(page["last_assigned_id"], 649)
        self.w.add_line("after clear")
        self.assertEqual(self.read()["lines"][0]["id"], 650)

    def test_renamed_old_weechat_buffer_close_invalidates_reused_pointer(self):
        original_type = self.w.hdata_get_var_type_string
        with patch.object(
            self.w,
            "hdata_get_var_type_string",
            side_effect=lambda h, f: (
                "" if (h, f) == ("buffer", "id") else original_type(h, f)
            ),
        ):
            cursor = self.read(limit=1)["next_cursor"]
            # Before WeeChat 4.3, native unique buffer IDs are unavailable.
            # The old name must be invalidated even when the buffer was renamed.
            with (
                patch.object(plugin, "api", self.api),
                patch.object(self.w, "buffer_get_string", return_value="renamed"),
            ):
                plugin.buffer_closing("", "buffer_closing", "p")
            with self.assertRaises(ValueError):
                self.read(cursor=cursor)

    def test_free_content_rejected(self):
        self.w.nodes["p"]["type"] = 1
        with self.assertRaisesRegex(ValueError, "formatted buffer"):
            self.read()

    def test_complete_payload_bounded_and_no_silent_line_truncation(self):
        self.w.add_line("x" * (300 * 1024))
        self.w.add_line("y" * (300 * 1024))
        page = self.read()
        self.assertEqual(page["returned"], 1)
        self.assertTrue(page["has_more"])
        self.assertLess(len(json.dumps(page).encode()), MAX_RESPONSE_BYTES)
        self.assertEqual(len(page["lines"][0]["message"]), 300 * 1024)
        self.w.add_line("x" * MAX_RESPONSE_BYTES)
        with self.assertRaisesRegex(ValueError, "budget"):
            self.read()

    def test_metadata_and_event_enrichment_are_budgeted(self):
        original = self.w.buffer_get_string
        with patch.object(
            self.w,
            "buffer_get_string",
            side_effect=lambda p, k: (
                "x" * MAX_RESPONSE_BYTES if k == "title" else original(p, k)
            ),
        ):
            with self.assertRaisesRegex(ValueError, "budget"):
                self.read()
        self.w.add_line("", tags_array=["nick_" + "x" * 510_000])
        with self.assertRaisesRegex(ConnectionError, "budget"):
            self.api.event_history(self.api.uri(NAME))

    def test_event_window_uses_actual_predecessor_and_native_metadata(self):
        result = self.api.event_history(self.api.uri(NAME))
        self.assertEqual(result["evicted_through"], 150)
        self.assertEqual(result["last_assigned_id"], 649)
        self.assertEqual(result["native_retained_from"], 0)
        self.assertEqual(result["lines"][0]["sequence"], 151)
        self.assertTrue(result["lines"][0]["is_message"])
        self.assertFalse(result["lines"][0]["is_self"])
        self.w.add_line("mine", tags_array=["nick_dot", "irc_action", "notify_self"])
        result = self.api.event_history(self.api.uri(NAME))
        self.assertTrue(result["lines"][-1]["is_self"])
        self.w.evict(651)
        result = self.api.event_history(self.api.uri(NAME))
        self.assertEqual(result["lines"], [])
        self.assertEqual(result["evicted_through"], 651)

    def test_sparse_ids_are_not_invented_evictions(self):
        self.w.evict(650)
        self.w.nodes["p"]["next_line_id"] = 1_000_000_000_000
        self.w.add_line("first sparse")
        result = self.api.event_history(self.api.uri(NAME))
        self.assertEqual(result["evicted_through"], 0)
        self.assertEqual(result["native_retained_from"], 1_000_000_000_000)

    def test_private_event_history_pages_keep_epoch_order_and_scope(self):
        uri = self.api.uri(NAME)
        first = self.api.event_history(uri)
        self.w.add_line("newer than first page")
        older = self.api.event_history(uri, first["next_cursor"])
        self.assertEqual(first["epoch"], older["epoch"])
        self.assertEqual([x["sequence"] for x in older["lines"]], list(range(1, 151)))
        self.assertIsNone(older["next_cursor"])
        self.assertTrue(all(x["is_message"] for x in older["lines"]))
        response = plugin.BridgeProtocol(self.api).handle(
            {
                "id": 1,
                "operation": "event_history",
                "uri": uri,
                "cursor": first["next_cursor"],
            }
        )
        self.assertEqual(response["result"]["lines"], older["lines"])
        self.api.native = NativeBuffers(self.w)
        with self.assertRaises(ConnectionError):
            self.api.event_history(uri, first["next_cursor"])
        with self.assertRaises(ValueError):
            self.api.event_history(self.api.uri("core.weechat"))

    def test_print_hook_is_notification_only(self):
        before = self.read()
        with patch.object(plugin, "api", self.api), patch.object(plugin, "clients", {}):
            plugin.printed("", "p", 1, "nick_alice", 1, 0, "alice", "not captured")
        self.assertEqual(before, self.read())


if __name__ == "__main__":
    unittest.main()
