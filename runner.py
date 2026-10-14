# SPDX-License-Identifier: GPL-3.0-or-later
"""Durable mention-trigger queue; model invocation is deliberately host-owned."""

import json
import re
import sqlite3


def irc_fold(text):
    return text.lower().translate(str.maketrans("[]\\^", "{}|~"))


class HistoryGap(RuntimeError):
    pass


class Runner:
    """One database per agent/buffer. Atomic cursor + queue; bounded turns.

    start_turn/finish_turn permit intake while a host/model is busy. A crash in the
    interval between IRC send and finish_turn is at-least-once, not exactly-once.
    The caller must reconcile uncertain sends before reprocessing a running turn.
    """

    def __init__(self, path, nick, max_turns=2):
        self.db = sqlite3.connect(path)
        self.nick = irc_fold(nick)
        self.max_turns = max_turns
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS cursor (epoch TEXT PRIMARY KEY, sequence INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS inbox (
            epoch TEXT, sequence INTEGER, payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY(epoch,sequence));
          CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        with self.db:
            row = self.db.execute(
                "SELECT value FROM settings WHERE key='nick'"
            ).fetchone()
            if row and row[0] != self.nick:
                raise ValueError("Database belongs to another agent")
            self.db.execute(
                "INSERT OR IGNORE INTO settings VALUES ('nick',?)", (self.nick,)
            )

    def ingest(self, snapshot):
        epoch = snapshot.get("data_epoch", snapshot["epoch"])
        lines = snapshot["lines"]
        if not isinstance(epoch, str) or not epoch:
            raise ValueError("Resource needs a nonempty epoch")
        # Native IDs are per buffer and can be sparse. Detect omitted actual rows
        # and conservatively reject a saved cursor older than native retention.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT sequence FROM cursor WHERE epoch=?", (epoch,)
            ).fetchone()
            if (
                row is None
                and self.db.execute("SELECT 1 FROM cursor LIMIT 1").fetchone()
            ):
                raise HistoryGap(
                    "Native buffer lifetime changed; reconcile before starting a new cursor"
                )
            cursor = row[0] if row else 0
            if not lines:
                last_id = snapshot.get("last_assigned_id")
                if row and last_id is not None and last_id + 1 > cursor:
                    raise HistoryGap("Native lines were cleared before intake")
                return 0
            omitted = snapshot.get("omitted_through_id")
            retained_from = snapshot.get("oldest_available_id")
            if (omitted is not None and omitted + 1 > cursor) or (
                cursor > 0 and retained_from is not None and retained_from > cursor
            ):
                raise HistoryGap(
                    "Retained resource history no longer covers saved cursor"
                )
            added = 0
            high = cursor
            for line in lines:
                native_id = line["id"]
                if type(native_id) is not int or native_id < 0:
                    raise ValueError("Invalid native line ID")
                seq = native_id + 1
                high = max(high, seq)
                if seq <= cursor:
                    continue
                tags = line["tags"]
                author = irc_fold(
                    next((tag[5:] for tag in tags if tag.startswith("nick_")), "")
                )
                text = irc_fold(line.get("message", ""))
                # Only actual nick-tagged IRC messages; never notices/status prompts.
                if (
                    author
                    and set(tags) & {"irc_privmsg", "irc_action"}
                    and "notify_self" not in tags
                    and author != self.nick
                    and re.search(
                        r"(?<![\w{}|~\-])@?" + re.escape(self.nick) + r"(?![\w{}|~\-])",
                        text,
                    )
                ):
                    result = self.db.execute(
                        "INSERT OR IGNORE INTO inbox(epoch,sequence,payload) VALUES (?,?,?)",
                        (epoch, seq, json.dumps(line)),
                    )
                    added += result.rowcount
            self.db.execute(
                "INSERT INTO cursor VALUES (?,?) ON CONFLICT(epoch) DO UPDATE SET sequence=excluded.sequence",
                (epoch, high),
            )
            return added

    def start_turn(self):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT 1 FROM inbox WHERE status='running'").fetchone():
                return None
            used = self.db.execute(
                "SELECT COUNT(*) FROM inbox WHERE status IN ('running','done')"
            ).fetchone()[0]
            if used >= self.max_turns:
                return None
            row = self.db.execute(
                "SELECT epoch,sequence,payload FROM inbox WHERE status='pending' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if not row:
                return None
            self.db.execute(
                "UPDATE inbox SET status='running' WHERE epoch=? AND sequence=?",
                row[:2],
            )
            return {"epoch": row[0], "sequence": row[1], "message": json.loads(row[2])}

    def finish_turn(self, turn):
        with self.db:
            changed = self.db.execute(
                "UPDATE inbox SET status='done' WHERE epoch=? AND sequence=? AND status='running'",
                (turn["epoch"], turn["sequence"]),
            ).rowcount
            if changed != 1:
                raise ValueError("Turn is not running")

    def counts(self):
        return dict(
            self.db.execute("SELECT status,COUNT(*) FROM inbox GROUP BY status")
        )

    def close(self):
        self.db.close()
