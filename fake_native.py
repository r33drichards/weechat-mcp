# SPDX-License-Identifier: GPL-3.0-or-later
"""Strict small fake of the native hdata graph, shared by offline tests."""

from typing import Any


class NativeFake:
    def __init__(self):
        self.nodes: dict[str, dict[str, Any]] = {
            "p": {"id": 1234567890123, "type": 0, "own_lines": "lp", "next_line_id": 0},
            "secret": {
                "id": 9876543210123,
                "type": 0,
                "own_lines": "ls",
                "next_line_id": 0,
            },
            "lp": {"lines_count": 0, "first_line": "", "last_line": ""},
            "ls": {"lines_count": 0, "first_line": "", "last_line": ""},
        }
        self.rows = {"p": [], "secret": []}
        self.gets = []

    def add_line(self, message, pointer="p", **values):
        seq = self.nodes[pointer]["next_line_id"]
        self.nodes[pointer]["next_line_id"] = seq + 1
        key = pointer + ":" + str(seq)
        prev = self.rows[pointer][-1] if self.rows[pointer] else ""
        self.nodes[key] = {"data": key + ":data", "prev_line": prev, "next_line": ""}
        self.nodes[key + ":data"] = {
            "id": seq,
            "date": 1700000000 + seq,
            "date_usec": 0,
            "date_printed": 1700000000 + seq,
            "date_usec_printed": 123,
            "displayed": 1,
            "highlight": 0,
            "prefix": "alice",
            "message": message,
            "tags_array": ["nick_alice", "irc_privmsg"],
            **values,
        }
        if prev:
            self.nodes[prev]["next_line"] = key
        self.rows[pointer].append(key)
        self.sync_lines(pointer)
        return seq

    def sync_lines(self, pointer):
        rows = self.rows[pointer]
        self.nodes[self.nodes[pointer]["own_lines"]].update(
            lines_count=len(rows),
            first_line=rows[0] if rows else "",
            last_line=rows[-1] if rows else "",
        )

    def evict(self, count, pointer="p"):
        for key in self.rows[pointer][:count]:
            del self.nodes[key + ":data"]
            del self.nodes[key]
        self.rows[pointer] = self.rows[pointer][count:]
        if self.rows[pointer]:
            self.nodes[self.rows[pointer][0]]["prev_line"] = ""
        self.sync_lines(pointer)

    def hdata_get(self, name):
        assert name in ("buffer", "lines", "line", "line_data")
        return name

    def hdata_get_var_type_string(self, hdata, field):
        types = {
            "buffer": {"id": "longlong", "next_line_id": "integer"},
            "line_data": {
                "id": "integer",
                "date": "time",
                "date_printed": "time",
                "date_usec": "integer",
                "date_usec_printed": "integer",
                "displayed": "char",
                "highlight": "char",
            },
        }
        return types.get(hdata, {}).get(field, "")

    def get(self, hdata, pointer, field):
        self.gets.append((hdata, pointer, field))
        return self.nodes[pointer][field]

    hdata_pointer = get
    hdata_longlong = get
    hdata_long = get
    hdata_time = get
    hdata_char = get

    def hdata_integer(self, hdata, pointer, field):
        if field == "tags_count":
            return len(self.nodes[pointer]["tags_array"])
        return self.get(hdata, pointer, field)

    def hdata_string(self, hdata, pointer, field):
        if "|" in field:
            index, key = field.split("|")
            return self.nodes[pointer][key][int(index)]
        return self.get(hdata, pointer, field)

    def string_remove_color(self, text, _):
        return text

    def buffer_get_integer(self, pointer, field):
        return self.nodes[pointer][field]
