# SPDX-License-Identifier: GPL-3.0-or-later
"""Extract a reviewable command inventory from WeeChat's C registrations.

This is a conservative source reader, not a C preprocessor, compiler, or grammar
inference engine. It preserves unresolved expressions, includes build-conditional
registrations, and does not assert that a command exists in a running WeeChat.
Descriptions are source-language strings, not translated/rendered runtime help.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Mapping

SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "1"
_REGISTRATION = re.compile(r"\b(weechat_hook_command|hook_command)\s*\(")
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_FIELDS = ("name", "description", "args", "args_description", "completion")


class UnresolvedExpression(ValueError):
    """The expression cannot be resolved without executing/preprocessing C."""


def mask_c(text: str, *, strings: bool = False, directives: bool = False) -> str:
    """Replace comments (and optionally literals/directives) with equal-size spaces.

    Newlines and offsets survive, including inside multiline comments. Delimiters
    in comments, escaped strings, and character literals never affect balancing.
    """
    chars = list(text)
    index = 0
    while index < len(text):
        start = index
        if text.startswith("//", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
        elif text[index] in "\"'":
            quote = text[index]
            index += 1
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                elif text[index] == quote:
                    index += 1
                    break
                else:
                    index += 1
            if not strings:
                continue
        else:
            index += 1
            continue
        for pos in range(start, min(index, len(text))):
            if chars[pos] not in "\r\n":
                chars[pos] = " "
    result = "".join(chars)
    if directives:
        lines = result.splitlines(keepends=True)
        continued = False
        for index, line in enumerate(lines):
            if continued or line.lstrip().startswith("#"):
                continued = line.rstrip("\r\n").endswith("\\")
                lines[index] = "".join(c if c in "\r\n" else " " for c in line)
        result = "".join(lines)
    return result


def _closing(masked: str, opening: int) -> int:
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = []
    for index in range(opening, len(masked)):
        char = masked[index]
        if char in pairs:
            stack.append(pairs[char])
        elif char in ")]}":
            if not stack or stack.pop() != char:
                raise UnresolvedExpression("unbalanced C delimiters")
            if not stack:
                return index
    raise UnresolvedExpression("unterminated C expression")


def split_arguments(expression: str) -> list[str]:
    """Split a C argument list, respecting all balanced delimiters/literals."""
    masked = mask_c(expression, strings=True)
    result = []
    start = index = 0
    while index < len(masked):
        if masked[index] in "([{":
            index = _closing(masked, index) + 1
            continue
        if masked[index] == ",":
            result.append(expression[start:index].strip())
            start = index + 1
        index += 1
    tail = expression[start:].strip()
    if tail or result:
        result.append(tail)
    return result


def _decode_string(text: str, opening: int) -> tuple[str, int]:
    """Decode C escapes without evaluating Python or corrupting source UTF-8."""
    result = []
    index = opening + 1
    escapes = {
        "a": "\a",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "\\": "\\",
        '"': '"',
        "'": "'",
        "?": "?",
    }
    while index < len(text):
        char = text[index]
        index += 1
        if char == '"':
            return "".join(result), index
        if char != "\\":
            result.append(char)
            continue
        if index >= len(text):
            break
        char = text[index]
        index += 1
        if char in escapes:
            result.append(escapes[char])
        elif char in "\r\n":
            if char == "\r" and index < len(text) and text[index] == "\n":
                index += 1
        elif char in "01234567":
            match = re.match(r"[0-7]{0,2}", text[index:])
            assert match is not None
            digits = char + match.group(0)
            index += len(digits) - 1
            result.append(chr(int(digits, 8)))
        elif char in "xuU":
            pattern = {
                "x": r"[0-9a-fA-F]+",
                "u": r"[0-9a-fA-F]{4}",
                "U": r"[0-9a-fA-F]{8}",
            }[char]
            match = re.match(pattern, text[index:])
            if not match:
                raise UnresolvedExpression("invalid C numeric string escape")
            index += len(match.group(0))
            try:
                value = int(match.group(0), 16)
                if 0xD800 <= value <= 0xDFFF:
                    raise ValueError("surrogate")
                result.append(chr(value))
            except ValueError as error:
                raise UnresolvedExpression("unsupported C string code point") from error
        else:
            raise UnresolvedExpression(f"unsupported C escape: \\{char}")
    raise UnresolvedExpression("unterminated C string literal")


def resolve_c_string(
    expression: str,
    macros: Mapping[str, str] | None = None,
    _seen: frozenset[str] = frozenset(),
) -> str | None:
    """Resolve literals, translation markers, help concatenation, and safe macros.

    NULL is a resolved null, distinct from an unresolved expression (an exception).
    Only unambiguous object-like macros supplied by the caller are expanded.
    """
    macros = macros or {}
    expression = mask_c(expression).strip()
    if expression in {"NULL", "0"}:
        return None
    if not expression:
        raise UnresolvedExpression("empty expression")
    values = []
    index = 0
    masked = mask_c(expression, strings=True)
    while index < len(expression):
        if expression[index].isspace():
            index += 1
            continue
        literal = re.match(r'(?:u8|u|U|L)?"', expression[index:])
        if literal:
            value, index = _decode_string(expression, index + len(literal.group()) - 1)
            values.append(value)
            continue
        if expression[index] == "(":
            end = _closing(masked, index)
            value = resolve_c_string(expression[index + 1 : end], macros, _seen)
            if value is None:
                if values or expression[end + 1 :].strip():
                    raise UnresolvedExpression("null in string concatenation")
                return None
            values.append(value)
            index = end + 1
            continue
        identifier = _IDENTIFIER.match(expression, index)
        if not identifier:
            raise UnresolvedExpression("nonconstant string expression")
        name = identifier.group()
        index = identifier.end()
        while index < len(expression) and expression[index].isspace():
            index += 1
        if index < len(expression) and expression[index] == "(":
            end = _closing(masked, index)
            arguments = split_arguments(expression[index + 1 : end])
            if name in {"N_", "_", "gettext", "AI"} and len(arguments) == 1:
                value = resolve_c_string(arguments[0], macros, _seen)
            elif name in {"CMD_ARGS_DESC", "WEECHAT_CMD_ARGS_DESC"}:
                parts = []
                for arg in arguments:
                    part = resolve_c_string(arg, macros, _seen)
                    if part is None:
                        raise UnresolvedExpression("null help-description component")
                    parts.append(part)
                marker = resolve_c_string(
                    macros.get("WEECHAT_HOOK_COMMAND_STR_FORMATTED", '"[fmt]"'),
                    macros,
                    _seen,
                )
                if marker is None:
                    raise UnresolvedExpression("null help-description marker")
                value = "\n".join([marker, *parts])
            else:
                raise UnresolvedExpression(f"unsupported function-like macro: {name}")
            if value is None:
                raise UnresolvedExpression("null string wrapper")
            values.append(value)
            index = end + 1
        elif name in macros and name not in _seen:
            value = resolve_c_string(macros[name], macros, _seen | {name})
            if value is None:
                raise UnresolvedExpression("null object-like macro")
            values.append(value)
        else:
            raise UnresolvedExpression(f"unresolved identifier: {name}")
    return "".join(values)


def collect_macros(texts: Mapping[str, str]) -> dict[str, str]:
    """Collect object macros, declining conflicting definitions across the tree."""
    definitions: dict[str, set[str]] = {}
    for text in texts.values():
        clean = mask_c(text)
        clean = re.sub(r"\\\r?\n", "", clean)
        for match in re.finditer(
            r"(?m)^\s*#\s*define\s+([A-Za-z_]\w*)([^\r\n]*)", clean
        ):
            name, suffix = match.groups()
            if suffix.startswith("("):
                continue  # Function-like macros have no whitespace before '('.
            if suffix.strip():
                definitions.setdefault(name, set()).add(suffix.strip())
    return {
        name: next(iter(values))
        for name, values in definitions.items()
        if len(values) == 1
    }


def _location(text: str, path: str, start: int, end: int | None = None) -> dict:
    location = {"path": path, "line": text.count("\n", 0, start) + 1}
    if end is not None:
        location["end_line"] = text.count("\n", 0, end) + 1
    return location


def _callback_definitions(text: str, path: str) -> dict[str, dict]:
    masked = mask_c(text, strings=True, directives=True)
    result = {}
    for match in re.finditer(
        r"(?m)^[ \t]*(?:[A-Za-z_]\w*[ \t*]+)*([A-Za-z_]\w*)[ \t]*\(", masked
    ):
        name = match.group(1)
        if name in {"if", "for", "while", "switch"}:
            continue
        try:
            end = _closing(masked, match.end() - 1)
        except UnresolvedExpression:
            continue
        rest = re.match(r"\s*", masked[end + 1 :])
        assert rest is not None
        body_start = end + 1 + rest.end()
        empty_macro = name == "COMMAND_EMPTY"
        if not empty_macro and (body_start >= len(masked) or masked[body_start] != "{"):
            continue
        if name in {"IRC_COMMAND_CALLBACK", "COMMAND_CALLBACK", "COMMAND_EMPTY"}:
            argument = masked[match.end() : end].strip()
            if not re.fullmatch(r"[A-Za-z_]\w*", argument):
                continue
            prefix = "irc_command_" if name == "IRC_COMMAND_CALLBACK" else "command_"
            name = prefix + argument
        body_end = end if empty_macro else _closing(masked, body_start)
        evidence = []
        body_line = text.count("\n", 0, body_start) + 1
        body_text = "" if empty_macro else text[body_start : body_end + 1]
        body_masked = mask_c(body_text)
        for offset, line in enumerate(body_masked.splitlines()):
            if re.search(
                r"\bargc\b|\bargv(?:_eol)?\s*\[|COMMAND_(?:MIN_ARGS|CHECK_SERVER)", line
            ):
                evidence.append({"line": body_line + offset, "text": line.strip()})
        result[name] = {
            **_location(text, path, match.start(1), body_end),
            "evidence": evidence[:12],
            "evidence_truncated": len(evidence) > 12,
        }
    return result


def _following_keep_spaces(
    text: str,
    clean: str,
    start: int,
    end: int,
    macros: Mapping[str, str],
) -> tuple[bool | None, str | None]:
    """Recognize only the immediate setter of this call's assigned hook."""
    before = clean[
        max(clean.rfind(";", 0, start), clean.rfind("{", 0, start)) + 1 : start
    ]
    assignment = re.search(
        r"([A-Za-z_]\w*(?:(?:->|\.)[A-Za-z_]\w*|\[[^\]]+\])*)\s*=\s*$", before
    )
    if not assignment:
        return None, None
    variable = assignment.group(1)
    tail = clean[end + 1 :]
    match = re.match(r"\s*;\s*([^;{}]+);", tail)
    if not match:
        return None, None
    statement = match.group(1).strip()
    expanded = macros.get(statement, statement)
    setter = re.fullmatch(r"(?:weechat_)?hook_set\s*\((.*)\)", expanded, re.S)
    if not setter:
        return None, None
    try:
        args = split_arguments(setter.group(1))
        if len(args) != 3 or args[0].strip() != variable:
            return None, None
        if resolve_c_string(args[1], macros) != "keep_spaces_right":
            return None, None
        value = resolve_c_string(args[2], macros)
        if value in {"0", "1"}:
            return value == "1", statement
    except UnresolvedExpression:
        pass
    return None, statement


def extract_commands(
    text: str,
    path: str = "input.c",
    *,
    macros: Mapping[str, str] | None = None,
    revision: str | None = None,
    default_keep_spaces_right: bool | None = None,
    callback_locations: Mapping[str, dict | None] | None = None,
) -> list[dict]:
    """Extract registrations from one source file, retaining unknown arguments."""
    macros = macros or collect_macros({path: text})
    callback_locations = callback_locations or _callback_definitions(text, path)
    clean = mask_c(text, directives=True)
    masked = mask_c(text, strings=True, directives=True)
    commands = []
    for match in _REGISTRATION.finditer(masked):
        opening = match.end() - 1
        try:
            end = _closing(masked, opening)
            arguments = split_arguments(text[opening + 1 : end])
        except UnresolvedExpression as error:
            commands.append(
                {
                    "name": None,
                    "location": _location(text, path, match.start()),
                    "source_revision": revision,
                    "unparsed": {
                        "registration": {
                            "expression": text[match.start() :],
                            "reason": str(error),
                        }
                    },
                }
            )
            continue
        if masked[end + 1 :].lstrip().startswith("{"):
            continue  # Function definition, not registration.
        offset = int(match.group(1) == "hook_command")
        if arguments and re.match(
            r"(?:struct\s|const\s|char\s*\*)", mask_c(arguments[0]).strip()
        ):
            continue  # Prototype/declaration.
        command: dict = {field: None for field in _FIELDS}
        command.update(
            {
                "registration_function": match.group(1),
                "location": _location(text, path, match.start(), end),
                "source_revision": revision,
                "callback": None,
                "callback_location": None,
                "callback_evidence": [],
                "callback_evidence_truncated": False,
                "registration_name": None,
                "priority": None,
                "keep_spaces_right": default_keep_spaces_right,
                "keep_spaces_right_evidence": "hook_command default"
                if default_keep_spaces_right is not None
                else None,
                "source_expressions": {},
                "unparsed": {},
            }
        )
        parts = Path(path).parts
        plugin = (
            parts[parts.index("plugins") + 1]
            if "plugins" in parts and len(parts) > parts.index("plugins") + 2
            else None
        )
        command["plugin"] = (
            "core"
            if offset and arguments and mask_c(arguments[0]).strip() == "NULL"
            else plugin
        )
        if len(arguments) != 8 + offset:
            command["unparsed"]["registration"] = {
                "expression": text[match.start() : end + 1],
                "reason": f"expected {8 + offset} arguments, found {len(arguments)}",
            }
            commands.append(command)
            continue
        if offset:
            command["source_expressions"]["plugin"] = arguments[0]
        for index, field in enumerate(_FIELDS, offset):
            expression = arguments[index]
            command["source_expressions"][field] = expression
            try:
                # hook_command copies null string arguments as empty strings.
                command[field] = resolve_c_string(expression, macros) or ""
            except UnresolvedExpression as error:
                command["unparsed"][field] = {
                    "expression": expression,
                    "reason": str(error),
                }
        command["registration_name"] = command["name"]
        if command["name"] is not None and "|" in command["name"]:
            prefix, command["name"] = command["name"].split("|", 1)
            if re.fullmatch(r"[+-]?\d+", prefix):
                command["priority"] = int(prefix)
        callback = arguments[offset + 5]
        command["source_expressions"]["callback"] = callback
        symbol = re.fullmatch(r"&?\s*([A-Za-z_]\w*)", mask_c(callback).strip())
        if symbol and symbol.group(1) != "NULL":
            command["callback"] = symbol.group(1)
            definition = callback_locations.get(symbol.group(1))
            if definition:
                command["callback_location"] = {
                    key: value
                    for key, value in definition.items()
                    if key not in {"evidence", "evidence_truncated"}
                }
                command["callback_evidence"] = definition.get("evidence", [])
                command["callback_evidence_truncated"] = definition.get(
                    "evidence_truncated", False
                )
        else:
            command["unparsed"]["callback"] = {
                "expression": callback,
                "reason": "dynamic callback expression",
            }
        keep, evidence = _following_keep_spaces(text, clean, match.start(), end, macros)
        if evidence is not None:
            command["keep_spaces_right"] = keep
            command["keep_spaces_right_evidence"] = evidence
        commands.append(command)
    return commands


def _git_revision(root: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def extract_inventory(source_root: str | Path, revision: str | None = None) -> dict:
    """Read a checkout's src tree without compiling or modifying it."""
    root = Path(source_root).resolve()
    source_dir = root / "src" if (root / "src").is_dir() else root
    files = sorted(
        path
        for path in source_dir.rglob("*")
        if path.suffix in {".c", ".h"} and path.is_file()
    )
    if not files:
        raise ValueError(f"no C source files found in {source_dir}")
    texts = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in files
    }
    revision = revision or _git_revision(root)
    macros = collect_macros(texts)
    callbacks: dict[str, dict | None] = {}
    callbacks_by_file = {}
    for path, text in texts.items():
        if path.endswith(".c"):
            callbacks_by_file[path] = _callback_definitions(text, path)
            for name, location in callbacks_by_file[path].items():
                # Resolve file-local callbacks locally below; only unique cross-file symbols.
                if name in callbacks:
                    callbacks[name] = None
                else:
                    callbacks[name] = location
    default = None
    default_evidence = None
    for path, text in texts.items():
        if path.endswith("hook-command.c"):
            match = re.search(
                r"new_hook_command->keep_spaces_right\s*=\s*([01])\s*;", mask_c(text)
            )
            if match:
                default = match.group(1) == "1"
                default_evidence = {
                    **_location(text, path, match.start()),
                    "expression": match.group(0),
                }
                break
    commands = []
    for path, text in texts.items():
        if path.endswith(".c"):
            local_callbacks = {**callbacks, **callbacks_by_file[path]}
            commands.extend(
                extract_commands(
                    text,
                    path,
                    macros=macros,
                    revision=revision,
                    default_keep_spaces_right=default,
                    callback_locations=local_callbacks,
                )
            )
    digest = hashlib.sha256()
    for path, text in texts.items():
        digest.update(path.encode("utf-8") + b"\0" + text.encode("utf-8") + b"\0")
    return {
        "schema_version": SCHEMA_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "source": {
            "revision": revision,
            "content_sha256": digest.hexdigest(),
            "files_scanned": len(files),
            "keep_spaces_right_default": default_evidence,
        },
        "limitations": [
            "Static source inventory, not runtime availability; preprocessor conditions are not evaluated.",
            "Unresolved expressions are retained; no argument types or command grammar are inferred.",
            "Description macros retain source-language strings and raw [fmt] formatting markers.",
            "keep_spaces_right is the observed immediate setter or source default; later mutations are not traced.",
            "Callback evidence is bounded source context, not an authoritative argument schema or complete control-flow analysis.",
        ],
        "commands": commands,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path, help="WeeChat source checkout")
    parser.add_argument(
        "--revision", help="explicit source revision for non-git source trees"
    )
    parser.add_argument(
        "--output", "-o", type=Path, help="JSON output (default: stdout)"
    )
    args = parser.parse_args(argv)
    try:
        inventory = extract_inventory(args.source_root, args.revision)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    output = json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
