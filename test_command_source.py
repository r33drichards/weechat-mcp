# SPDX-License-Identifier: GPL-3.0-or-later
"""Conservative source extraction: source facts, never invented argument types."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.extract_command_source import (
    UnresolvedExpression,
    collect_macros,
    extract_commands,
    extract_inventory,
    main,
    mask_c,
    resolve_c_string,
    split_arguments,
)


def hook(name='"test"', args='"<text>"', callback="&test_cb", core=False):
    function = "hook_command" if core else "weechat_hook_command"
    plugin = "NULL, " if core else ""
    return f'{function}({plugin}{name}, "description", {args}, "help", NULL, {callback}, NULL, NULL)'


class CStringTests(unittest.TestCase):
    def test_literals_escapes_unicode_and_translation_wrappers(self):
        self.assertEqual(
            resolve_c_string(r'N_("café " "\\\"\n\t\101\x42\u03bb")'), 'café \\"\n\tABλ'
        )
        self.assertEqual(resolve_c_string('_(AI(("hello"))) " world"'), "hello world")
        self.assertEqual(resolve_c_string('u8"café" L"!"'), "café!")
        self.assertEqual(resolve_c_string('"one\\\ntwo"'), "onetwo")
        self.assertIsNone(resolve_c_string(" /* absent */ NULL "))

    def test_help_macro_keeps_formatted_raw_source(self):
        self.assertEqual(
            resolve_c_string(
                'WEECHAT_CMD_ARGS_DESC(N_("text: body"), "", AI("  /test hi"))'
            ),
            "[fmt]\ntext: body\n\n  /test hi",
        )

    def test_unsupported_values_do_not_turn_into_empty_strings(self):
        for value in [
            "dynamic",
            '"known" + suffix',
            'format("%s", name)',
            "N_(NULL)",
            r'"\q"',
            r'"\x"',
            '"unterminated',
        ]:
            with self.subTest(value=value), self.assertRaises(UnresolvedExpression):
                resolve_c_string(value)

    def test_object_macros_conflicts_recursion_and_multiline_values(self):
        macros = collect_macros(
            {
                "one.h": '#define PREFIX "a" \\\n "b"\n#define WRAPPER(x) x\n#define GROUP ("group")\n#define BAD "one"\n',
                "two.h": '#define BAD "two"\n#define SAME "ok"\n#define SAME "ok"\n',
            }
        )
        self.assertEqual(resolve_c_string("PREFIX GROUP SAME", macros), "abgroupok")
        self.assertNotIn("WRAPPER", macros)
        self.assertNotIn("BAD", macros)
        with self.assertRaises(UnresolvedExpression):
            resolve_c_string("LOOP", {"LOOP": "AGAIN", "AGAIN": "LOOP"})

    def test_balancing_ignores_strings_chars_and_comments(self):
        values = split_arguments(
            r""""a, )", f(1, (2)), {'x', '\''}, arr[2], /*,)*/ last"""
        )
        self.assertEqual(len(values), 5)
        self.assertEqual(values[1], "f(1, (2))")
        self.assertTrue(values[4].endswith("last"))
        original = 'a /*(\n)*/ b "literal // text" // end\nc'
        masked = mask_c(original)
        self.assertEqual(len(masked), len(original))
        self.assertEqual(masked.count("\n"), original.count("\n"))
        self.assertIn('"literal // text"', masked)


class CommandExtractionTests(unittest.TestCase):
    def test_core_and_plugin_registration_argument_offsets(self):
        core = extract_commands(hook(core=True) + ";", "src/core/core-command.c")[0]
        plugin = extract_commands(hook() + ";", "src/plugins/irc/irc-command.c")[0]
        self.assertEqual(core["name"], "test")
        self.assertEqual(core["plugin"], "core")
        self.assertEqual(plugin["plugin"], "irc")
        self.assertEqual(plugin["args"], "<text>")
        self.assertEqual(plugin["completion"], "")
        self.assertEqual(plugin["callback"], "test_cb")
        self.assertEqual(plugin["unparsed"], {})
        self.assertIsNone(plugin["keep_spaces_right"])
        self.assertNotIn("parameters", plugin)

    def test_comments_strings_macros_declarations_and_definitions_are_not_hooks(self):
        source = f"""
// {hook()};
/* {hook()}; */
char *example = "weechat_hook_command(never)";
#define weechat_hook_command(a,b,c,d,e,f,g,h) \\
    hook_command(plugin,a,b,c,d,e,f,g,h)
struct t_hook *hook_command(struct t_plugin *p, const char *name) {{ return 0; }}
extern struct t_hook *hook_command(struct t_plugin *p, const char *name);
void init(void) {{ {hook()}; }}
"""
        commands = extract_commands(source)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["name"], "test")
        self.assertEqual(commands[0]["location"]["line"], 9)

    def test_source_expressions_preserve_comments_and_unsupported_evidence(self):
        source = (
            hook(
                name="/* source */ runtime_name",
                args='format("%s,)", spec)',
                callback="data->callback",
            )
            + ";"
        )
        command = extract_commands(source, revision="fixture")[0]
        self.assertIsNone(command["name"])
        self.assertIsNone(command["args"])
        self.assertIsNone(command["callback"])
        self.assertEqual(command["source_revision"], "fixture")
        self.assertEqual(
            command["source_expressions"]["name"], "/* source */ runtime_name"
        )
        self.assertEqual(
            command["unparsed"]["args"]["expression"], 'format("%s,)", spec)'
        )
        self.assertIn("callback", command["unparsed"])

    def test_priority_prefix_is_separate_from_command_name(self):
        command = extract_commands(hook(name='"50000|command"', core=True) + ";")[0]
        self.assertEqual(command["name"], "command")
        self.assertEqual(command["registration_name"], "50000|command")
        self.assertEqual(command["priority"], 50000)
        self.assertEqual(command["source_expressions"]["name"], '"50000|command"')

    def test_keep_spaces_macro_and_direct_setter_are_tied_to_assigned_hook(self):
        source = f"""
#define KEEP_SPACES weechat_hook_set(ptr_hook, "keep_spaces_right", "1")
void init(void) {{
    ptr_hook = {hook('"one"')};
    KEEP_SPACES;
    alias->hook = {hook('"two"')};
    weechat_hook_set(alias->hook, "keep_spaces_right", "0");
    other_hook = {hook('"three"')};
    KEEP_SPACES;
    {hook('"four"')};
    KEEP_SPACES;
}}
"""
        commands = extract_commands(source)
        self.assertEqual(
            [c["keep_spaces_right"] for c in commands], [True, False, None, None]
        )
        self.assertEqual(commands[0]["keep_spaces_right_evidence"], "KEEP_SPACES")

    def test_dynamic_keep_spaces_cannot_be_claimed_as_default(self):
        source = f'ptr_hook = {hook()}; weechat_hook_set(ptr_hook, "keep_spaces_right", setting);'
        command = extract_commands(source, default_keep_spaces_right=False)[0]
        self.assertIsNone(command["keep_spaces_right"])
        self.assertIn("setting", command["keep_spaces_right_evidence"])

    def test_callback_locations_and_bounded_non_authoritative_evidence(self):
        source = "\n".join(
            [
                "COMMAND_CALLBACK(test)",
                "{",
                '    COMMAND_MIN_ARGS(2, "");',
                *["    if (argc > 2) argv[1] = argv[2];" for _ in range(20)],
                "}",
                "COMMAND_EMPTY(away)",
                "IRC_COMMAND_CALLBACK(join)",
                "{",
                "    if (argc < 2) return 0;",
                "}",
                hook(callback="&command_test") + ";",
                hook('"away"', callback="&command_away") + ";",
                hook('"join"', callback="&irc_command_join") + ";",
            ]
        )
        commands = extract_commands(source, "src/core/core-command.c")
        self.assertEqual(commands[0]["callback_location"]["line"], 1)
        self.assertEqual(len(commands[0]["callback_evidence"]), 12)
        self.assertTrue(commands[0]["callback_evidence_truncated"])
        self.assertEqual(
            commands[0]["callback_evidence"][0]["text"], 'COMMAND_MIN_ARGS(2, "");'
        )
        self.assertIsNotNone(commands[1]["callback_location"])
        self.assertIsNotNone(commands[2]["callback_location"])

    def test_bad_argument_counts_and_unterminated_calls_remain_evidence(self):
        command = extract_commands('weechat_hook_command("bad", "too few");')[0]
        self.assertIsNone(command["name"])
        self.assertIn(
            "expected 8 arguments", command["unparsed"]["registration"]["reason"]
        )
        command = extract_commands('weechat_hook_command("unfinished",')[0]
        self.assertIn("unterminated", command["unparsed"]["registration"]["reason"])
        self.assertIn("unfinished", command["unparsed"]["registration"]["expression"])


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write(
            "src/core/hook/hook-command.c",
            "void defaults(void) { new_hook_command->keep_spaces_right = 0; }",
        )
        self.write("src/plugins/demo/demo.h", '#define DEMO_ARGS "<key> <value>"\n')
        self.write(
            "src/plugins/demo/callback.c",
            "int demo_cb(void) { if (argc < 2) return 0; return 1; }",
        )
        self.write(
            "src/plugins/demo/command.c",
            hook(args="DEMO_ARGS", callback="&demo_cb") + ";",
        )

    def write(self, path, value):
        full = self.root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(value, encoding="utf-8")

    def test_inventory_revision_defaults_origins_and_content_digest(self):
        result = extract_inventory(self.root, "test-revision")
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["source"]["revision"], "test-revision")
        self.assertEqual(result["source"]["files_scanned"], 4)
        self.assertEqual(len(result["source"]["content_sha256"]), 64)
        command = result["commands"][0]
        self.assertEqual(command["args"], "<key> <value>")
        self.assertFalse(command["keep_spaces_right"])
        self.assertEqual(
            command["callback_location"]["path"], "src/plugins/demo/callback.c"
        )
        self.assertEqual(command["source_revision"], "test-revision")
        self.assertTrue(any("not runtime" in value for value in result["limitations"]))
        self.assertEqual(result, extract_inventory(self.root, "test-revision"))
        self.write("src/plugins/demo/demo.h", '#define DEMO_ARGS "<changed>"\n')
        self.assertNotEqual(
            result["source"]["content_sha256"],
            extract_inventory(self.root)["source"]["content_sha256"],
        )

    def test_conflicting_global_callback_symbols_do_not_get_wrong_origins(self):
        self.write("src/plugins/other/callback.c", "int demo_cb(void) { return 2; }")
        self.assertIsNone(
            extract_inventory(self.root)["commands"][0]["callback_location"]
        )
        self.write(
            "src/plugins/demo/command.c",
            "int demo_cb(void) { return 3; }\n" + hook(callback="&demo_cb") + ";",
        )
        self.assertEqual(
            extract_inventory(self.root)["commands"][0]["callback_location"]["path"],
            "src/plugins/demo/command.c",
        )

    def test_cli_stdout_output_file_and_no_source_error(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main([str(self.root), "--revision", "cli"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["source"]["revision"], "cli")
        output = self.root / "inventory.json"
        self.assertEqual(main([str(self.root), "--output", str(output)]), 0)
        self.assertTrue(json.loads(output.read_text())["commands"])
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            main([str(self.root / "missing")])
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
