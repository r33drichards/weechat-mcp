# SPDX-License-Identifier: GPL-3.0-or-later
import copy
import json
import unittest
from pathlib import Path

from jsonschema import validate

from command_manifest import build_manifest, compile_variant, serialize, tool_spec
from test_mcp import Fake
from weechat_mcp import WeeChatAPI


def record(name="test", plugin="core", args="list|show [<name>]"):
    return dict(
        name=name,
        plugin=plugin,
        args=args,
        description="test",
        args_description="name: arbitrary string",
        completion="%(foo)",
        keep_spaces_right=1,
    )


def inputs(item, syntax="v0", params=None):
    return dict(
        buffer="core.weechat",
        registry_revision=item["revision"],
        syntax=syntax,
        params=params if params is not None else {"token_1": "list"},
    )


class Runtime(Fake):
    def __init__(self):
        super().__init__()
        self.records = [
            record(),
            record("test", "python"),
            record("opaque", args="<thing>..."),
        ]

    def info_get(self, key, args):
        return "4.6.3"

    def infolist_integer(self, il, key):
        return int(self.infolist_string(il, key) or 0)

    def infolist_get(self, kind, ptr, args):
        if kind == "hook":
            return {
                "rows": [
                    dict(r, command=r["name"], plugin_name=r["plugin"])
                    for r in self.records
                ],
                "i": -1,
            }
        il = super().infolist_get(kind, ptr, args)
        il["rows"].append({"full_name": "core.weechat", "pointer": "core"})
        return il


class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.item = build_manifest([record()])["commands"][0]

    def test_schema_and_serialize(self):
        args = inputs(self.item, params={"token_1": "show", "name": "hello"})
        validate(args, tool_spec(self.item)["inputSchema"])
        self.assertEqual(serialize(self.item, args), "show hello")
        self.assertEqual(serialize(self.item, inputs(self.item)), "list")

    def test_no_guessed_types(self):
        schema, _ = compile_variant("<port> <count>")
        self.assertEqual(schema["properties"]["port"]["type"], "string")

    def test_fallbacks(self):
        for syntax in [
            "",
            "<name>...",
            "<name>suffix",
            "[-<option>[=<value>]]",
            "<host>[/<port>]",
            "[<first>] <second>",
        ]:
            self.assertIsNone(compile_variant(syntax))
        item = build_manifest([record(args="list || <name>...")])["commands"][0]
        self.assertEqual(item["coverage"], "mixed")
        raw = 'first "two words"; /quit'
        args = inputs(item, "raw", {"arguments": raw})
        validate(args, tool_spec(item)["inputSchema"])
        self.assertEqual(serialize(item, args), raw)  # Preserved, never shlex-requoted.

    def test_injection_controls_and_quoting(self):
        for value in ["a\nb", "\r/quit", "a\x00b", "\x7f"]:
            with self.assertRaises(ValueError):
                serialize(self.item, inputs(self.item, "raw", {"arguments": value}))
        for value in ["two words", '"quoted"', "slash\\escape", ""]:
            with self.assertRaises(ValueError):
                serialize(
                    self.item,
                    inputs(self.item, params={"token_1": "list", "name": value}),
                )

    def test_missing_extra_gap_and_stale(self):
        item = build_manifest([record(args="<one> [<two>] [<three>]")])["commands"][0]
        for params in [{}, {"one": "1", "extra": "x"}, {"one": "1", "three": "3"}]:
            with self.assertRaises(ValueError):
                serialize(item, inputs(item, params=params))
        args = inputs(self.item)
        args["registry_revision"] = "stale"
        with self.assertRaises(ValueError):
            serialize(self.item, args)

    def test_origin_and_drift(self):
        original = record()
        for key in ["args", "args_description", "completion", "keep_spaces_right"]:
            changed = copy.deepcopy(original)
            changed[key] = str(changed[key]) + "changed"
            self.assertNotEqual(
                build_manifest([changed])["commands"][0]["revision"],
                self.item["revision"],
            )
        self.assertNotEqual(
            build_manifest([original], "new")["commands"][0]["revision"],
            self.item["revision"],
        )

    def test_real_registry_fixture_full_coverage(self):
        fixture = json.loads(
            (
                Path(__file__).parent / "fixtures/runtime-commands-weechat-4.6.3.json"
            ).read_text()
        )
        manifest = build_manifest(
            fixture["commands"], fixture["origin"]["weechat_version"]
        )
        self.assertEqual(manifest["coverage"], fixture["expected_coverage"])
        self.assertEqual(
            len({c["tool_name"] for c in manifest["commands"]}),
            len(fixture["commands"]),
        )
        for item in manifest["commands"]:
            args = inputs(item, "raw", {"arguments": ""})
            validate(args, tool_spec(item)["inputSchema"])
            self.assertEqual(serialize(item, args), "")

    def test_source_evidence_and_drift(self):
        source = {
            "schema_version": 1,
            "source": {"revision": "git-revision"},
            "commands": [record()],
        }
        matched = build_manifest([record()], source=source)
        self.assertEqual(matched["source"]["revision"], "git-revision")
        self.assertEqual(matched["commands"][0]["source_evidence"]["status"], "matched")
        changed = record(args="<changed>")
        drift = build_manifest([changed], source=source)
        self.assertEqual(
            drift["commands"][0]["source_evidence"]["status"], "drift_or_unresolved"
        )
        self.assertEqual(drift["commands"][0]["args"], "<changed>")

    def test_all_runtime_commands_and_dynamic_unload(self):
        runtime = Runtime()
        api = WeeChatAPI(runtime, full_control=True)
        manifest = api.command_manifest()
        tools = [t for t in api.tools() if t["name"].startswith("weechat_cmd_")]
        self.assertEqual(len(tools), len(runtime.records))
        self.assertEqual(len({t["name"] for t in tools}), len(tools))
        item = next(
            c
            for c in manifest["commands"]
            if c["name"] == "test" and c["plugin"] == "core"
        )
        self.assertTrue(api.call(item["tool_name"], inputs(item))["dispatched"])
        self.assertEqual(runtime.calls[-1], ("core", "/command core /test list"))
        runtime.records = [r for r in runtime.records if r["plugin"] != "core"]
        with self.assertRaises(ValueError):
            api.call(item["tool_name"], inputs(item))
        runtime.records.append(record("new_plugin_command", "newplugin"))
        self.assertTrue(
            any(
                c["name"] == "new_plugin_command"
                for c in api.command_manifest()["commands"]
            )
        )

    def test_history_scope_unchanged_and_opt_in(self):
        runtime = Runtime()
        api = WeeChatAPI(runtime, full_control=True)
        self.assertIn("core.weechat", api.command_buffers())
        self.assertNotIn("core.weechat", api.buffers())
        with self.assertRaises(ValueError):
            api.resolve(api.uri("core.weechat"))
        self.assertFalse(
            any(
                t["name"].startswith("weechat_cmd_")
                for t in WeeChatAPI(runtime, full_control=False).tools()
            )
        )
