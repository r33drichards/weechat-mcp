# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit, owner-scoped MCP Events subscriptions and a durable webhook outbox.

Nothing subscribes on startup, buffer join, discovery or ordinary tool calls.
The callback supplied by the MCP host is the chat/session identity. Secrets stay
in the private runtime database, never in event definitions or method results.
"""

import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import time
import weakref
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from jsonschema import ValidationError, validate

from event_webhooks import (
    SecureWebhookSender,
    WebhookError,
    signed_headers,
    validate_secret,
    validate_url,
)
from jev_filter import FilterError

EVENT_NAME = "message.created"
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "channel_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": "Exact exposed IRC buffer name from weechat_buffers, such as irc.server.#channel.",
        },
        "trigger": {
            "type": "string",
            "enum": ["message", "mention"],
            "description": "Every incoming chat message, or only whole-nick mentions.",
        },
        "jev": {
            "type": "object",
            "properties": {
                "preference": {"type": "string", "minLength": 1, "maxLength": 2048},
                "history_limit": {"type": "integer", "minimum": 0, "maximum": 50},
            },
            "required": ["preference"],
            "additionalProperties": False,
            "description": "Opt-in Jev send/drop filter. Sends this channel context and preference to Typesafe; requires operator enablement. Only for trigger=message.",
        },
        "nick": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "pattern": r"^[^\s,\x00-\x1f\x7f]+$",
            "description": "Nick to match when trigger is mention; IRC case folding applies.",
        },
    },
    "required": ["channel_id", "trigger"],
    "additionalProperties": False,
    "allOf": [
        {
            "if": {"required": ["jev"]},
            "then": {"properties": {"trigger": {"const": "message"}}},
        }
    ],
    "oneOf": [
        {
            "properties": {"trigger": {"const": "message"}},
            "not": {"required": ["nick"]},
        },
        {"properties": {"trigger": {"const": "mention"}}, "required": ["nick"]},
    ],
}
PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        key: {"type": "string"} for key in ("channel_id", "message_id", "nick", "text")
    },
    "required": ["channel_id", "message_id", "nick", "text"],
    "additionalProperties": False,
}
DEFINITION = {
    "name": EVENT_NAME,
    "description": "New incoming IRC messages in one explicitly chosen exposed channel. Subscribe only when the user asks this chat to monitor that channel and chooses a trigger. Never subscribe on startup, join, discovery or tool use. Own messages and status lines are excluded. Stop with events/unsubscribe.",
    "delivery": ["webhook"],
    "inputSchema": INPUT_SCHEMA,
    "payloadSchema": PAYLOAD_SCHEMA,
}


class EventError(Exception):
    def __init__(self, code, message, reason=None):
        super().__init__(message)
        self.code, self.message, self.reason = code, message, reason


def canonical(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def utc(seconds):
    return (
        datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")
    )


def irc_fold(value):
    return value.lower().translate(str.maketrans("[]\\^", "{}|~"))


def matches(arguments, line, own_nick):
    nick = line.get("nick", "")
    if not line.get("is_message") or line.get("is_self") or not nick:
        return False
    if own_nick and irc_fold(nick) == irc_fold(own_nick):
        return False
    if arguments["trigger"] == "message":
        return True
    return bool(
        re.search(
            r"(?<![\w{}|~\-])@?"
            + re.escape(irc_fold(arguments["nick"]))
            + r"(?![\w{}|~\-])",
            irc_fold(line["message"]),
        )
    )


def filter_context(arguments, snapshot, current, message):
    """Snapshot bounded prior context, never future lines or another channel.

    The current message stays intact. Native capture already bounds event bytes;
    the adapter bounds the whole request and stops the grant if it cannot fit.
    Context can include the user's own chat lines but not status/notices.
    """
    limit = arguments["jev"].get("history_limit", 20)
    history = []
    used = 0
    truncated = bool(snapshot.get("evicted_through", 0))
    for line in reversed(snapshot["lines"]):
        if line["sequence"] >= current["sequence"] or not line.get("is_message"):
            continue
        value = {
            "message_id": snapshot["epoch"] + ":" + str(line["sequence"]),
            "nick": line.get("nick", ""),
            "text": line["message"],
        }
        size = len(canonical(value).encode())
        if len(history) >= limit or used + size > 32768:
            truncated = True
            break
        history.append(value)
        used += size
    return {
        "channel_id": arguments["channel_id"],
        "preference": arguments["jev"]["preference"],
        "message": message,
        "history": list(reversed(history)),
        "history_truncated": truncated,
    }


class EventManager:
    """Single-process dispatcher. SQLite transactions commit intake with outbox.

    Replay is bounded to retained outbox records plus native WeeChat snapshots.
    Never claim exactly-once delivery: a crash after HTTP acceptance can resend.
    Per-subscription delivery is ordered so emitted cursors cannot skip backlog.
    """

    def __init__(
        self, path, backend, owner_allowed, sender=None, clock=time.time, judge=None
    ):
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("MCP_EVENTS_DB must be an absolute runtime path")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("Events database needs a private owner-only directory")
        self.file = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.file)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ValueError("Events database needs owner-only permissions")
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(path)
        except BaseException:
            os.close(self.file)
            raise
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA foreign_keys=ON;
          PRAGMA secure_delete=ON;
          CREATE TABLE IF NOT EXISTS subscriptions (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
            arguments TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL,
            old_secret TEXT, rotate_until REAL NOT NULL DEFAULT 0,
            expires REAL NOT NULL, epoch TEXT NOT NULL, intake INTEGER NOT NULL,
            acknowledged INTEGER NOT NULL DEFAULT 0, truncated INTEGER NOT NULL DEFAULT 0,
            suspended INTEGER NOT NULL DEFAULT 0, pruned_through INTEGER NOT NULL DEFAULT 0, seen_native_line INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS outbox (
            position INTEGER PRIMARY KEY AUTOINCREMENT,
            subscription TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            event_key TEXT NOT NULL, payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            due REAL NOT NULL DEFAULT 0, created REAL NOT NULL,
            UNIQUE(subscription, event_key));
          CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(subscription,status,position);
        """)
        # Additive migration preserves existing unfiltered outboxes and identities.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(outbox)")}
        for name in ("filter_context", "decision"):
            if name not in columns:
                self.db.execute(f"ALTER TABLE outbox ADD COLUMN {name} TEXT")
        columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(subscriptions)")
        }
        for name, definition in {
            "incarnation": "TEXT",
            "recovery_mode": "TEXT NOT NULL DEFAULT 'off'",
            "coverage_generation": "INTEGER NOT NULL DEFAULT 0",
            "coverage_state": "TEXT NOT NULL DEFAULT 'continuous'",
            "coverage_reason": "TEXT",
            "coverage_anchor": "TEXT",
            "recovery_due": "REAL NOT NULL DEFAULT 0",
            "recovery_attempts": "INTEGER NOT NULL DEFAULT 0",
            "coverage_lost": "INTEGER NOT NULL DEFAULT 0",
            "loss_generation": "INTEGER NOT NULL DEFAULT 0",
            "recovery_enrolled": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                self.db.execute(
                    f"ALTER TABLE subscriptions ADD COLUMN {name} {definition}"
                )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS coverage_audit(subscription TEXT NOT NULL,incarnation TEXT NOT NULL,generation INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,boundary TEXT NOT NULL,created REAL NOT NULL,resolved REAL,PRIMARY KEY(subscription,incarnation,generation))"
        )
        for row in self.db.execute(
            "SELECT * FROM subscriptions WHERE incarnation IS NULL"
        ).fetchall():
            self.db.execute(
                "UPDATE subscriptions SET incarnation=? WHERE id=?",
                (secrets.token_hex(16), row["id"]),
            )
        # Old sticky flags lack a trustworthy original boundary. Never infer one.
        self.clock = clock
        for row in self.db.execute(
            "SELECT * FROM subscriptions WHERE truncated=1 AND coverage_generation=0"
        ).fetchall():
            self._gap(row, "legacy_unknown")
        if "failure_class" not in {
            row[1] for row in self.db.execute("PRAGMA table_info(outbox)")
        }:
            self.db.execute("ALTER TABLE outbox ADD COLUMN failure_class TEXT")
        self.db.execute(
            "UPDATE subscriptions SET recovery_enrolled=1 WHERE recovery_mode!='off' AND recovery_enrolled=0"
        )
        self.db.commit()
        self.judge = judge
        self.backend, self.owner_allowed = backend, owner_allowed
        self.sender, self.clock = sender or SecureWebhookSender(), clock
        self.locks = weakref.WeakValueDictionary()
        self.intake_wake = asyncio.Event()
        self.intake_task = None
        self.send_slots = asyncio.Semaphore(8)
        self.wake = asyncio.Event()
        self.dirty = set()
        self.verified = {}
        self.task = None
        self.closed = False
        self.stopping = False

    def _coverage(self, row, continuity=False):
        result = {
            "version": 1,
            "incarnation": row["incarnation"],
            "generation": row["coverage_generation"],
            "status": row["coverage_state"],
        }
        if row["coverage_reason"]:
            result["reason"] = row["coverage_reason"]
        latest = self.db.execute(
            "SELECT state FROM coverage_audit WHERE subscription=? AND incarnation=? AND generation=?",
            (row["id"], row["incarnation"], row["coverage_generation"]),
        ).fetchone()
        if row["loss_generation"]:
            result["lossGeneration"] = row["loss_generation"]
        if latest and latest["state"] == "recovered":
            result.update(
                recoveredGeneration=row["coverage_generation"],
                proof="retained_native_anchor",
            )
        elif row["coverage_state"] == "degraded":
            result.update(
                acknowledgedGeneration=row["loss_generation"], proof="acknowledged_loss"
            )
        if continuity:
            result["continuity"] = "retained-subscription"
        return result

    def _gap(self, row, reason, snapshot=None):
        if row["coverage_state"] in ("gap", "recovering"):
            return
        generation = row["coverage_generation"] + 1
        detected_reason = reason
        if row["recovery_mode"] == "off":
            reason = "legacy_unknown"
        anchor = {
            "detectedReason": detected_reason,
            "epoch": row["epoch"],
            "intake": row["intake"],
            "acknowledged": row["acknowledged"],
            "prunedThrough": row["pruned_through"],
            "observedEpoch": snapshot.get("epoch") if snapshot else None,
            "observedTip": max((x["sequence"] for x in snapshot["lines"]), default=0)
            if snapshot
            else None,
        }
        recoverable = reason == "native_window_gap" and row["recovery_mode"] != "off"
        state = "recovering" if recoverable else "gap"
        self.db.execute(
            "UPDATE subscriptions SET truncated=1,coverage_generation=?,coverage_state=?,coverage_reason=?,coverage_anchor=?,recovery_due=0,recovery_attempts=0 WHERE id=?",
            (generation, state, reason, canonical(anchor), row["id"]),
        )
        self.db.execute(
            "INSERT INTO coverage_audit(subscription,incarnation,generation,state,reason,boundary,created) VALUES(?,?,?,?,?,?,?)",
            (
                row["id"],
                row["incarnation"],
                generation,
                state,
                reason,
                canonical(anchor),
                self.clock(),
            ),
        )

    async def _reconcile(self, ident):
        """Bounded read-only native paging; commit only a complete same-epoch chain.

        Caller owns the subscription lock. No cursor/line content is persisted in
        recovery metadata. A restart safely restarts the bounded read attempt.
        """
        row = self.db.execute(
            "SELECT * FROM subscriptions WHERE id=?", (ident,)
        ).fetchone()
        if (
            not row
            or row["recovery_mode"] == "off"
            or row["coverage_state"] not in ("gap", "recovering")
            or row["recovery_due"] > self.clock()
        ):
            return
        generation = row["coverage_generation"]
        # Reserve attempts before I/O; repeated subscribe/collect cannot spin.
        attempts = row["recovery_attempts"] + 1
        with self.db:
            self.db.execute(
                "UPDATE subscriptions SET recovery_attempts=?,recovery_due=? WHERE id=?",
                (
                    attempts,
                    self.clock() + min(900, 30 * 2 ** min(attempts - 1, 5)),
                    ident,
                ),
            )
        args = json.loads(row["arguments"])
        deadline = time.monotonic() + 15

        async def read_page(cursor=None):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            return await asyncio.wait_for(
                self._history(args["channel_id"], cursor), min(5, remaining)
            )

        try:
            latest = await read_page()
            anchor = json.loads(row["coverage_anchor"])
            pages = [latest]
            proven = False
            loss_known = (
                row["coverage_reason"] != "native_window_gap"
                or latest["epoch"] != anchor["epoch"]
            )
            if (
                row["coverage_reason"] == "native_window_gap"
                and latest["epoch"] == anchor["epoch"]
            ):
                size = len(canonical(latest).encode())
                seen = set()
                for page_index in range(20):
                    page = pages[-1]
                    if page["epoch"] != anchor["epoch"]:
                        break
                    # Native IDs may be sparse: require the exact saved anchor,
                    # not arithmetic or an oldest-ID comparison.
                    if any(x["sequence"] == anchor["intake"] for x in page["lines"]):
                        proven = True
                        break
                    cursor = page.get("next_cursor")
                    if not cursor:
                        loss_known = (
                            True  # Native chain ended without the saved anchor.
                        )
                        break
                    if cursor in seen or page_index == 19:
                        break
                    seen.add(cursor)
                    page = await read_page(cursor)
                    size += len(canonical(page).encode())
                    if size > 4 * 1024 * 1024:
                        break
                    pages.append(page)
            current = self.db.execute(
                "SELECT * FROM subscriptions WHERE id=?", (ident,)
            ).fetchone()
            if (
                not current
                or current["incarnation"] != row["incarnation"]
                or current["coverage_generation"] != generation
                or current["coverage_state"] not in ("gap", "recovering")
            ):
                return
            if current["expires"] <= self.clock() or not self.owner_allowed(
                current["owner"]
            ):
                return
            if proven:
                lines = [
                    line
                    for page in reversed(pages)
                    for line in page["lines"]
                    if line["sequence"] > anchor["intake"]
                ]
                # A page chain must be strictly native-order, with no duplicates.
                ids = [line["sequence"] for line in lines]
                if ids != sorted(set(ids)):
                    return
                queued = self.db.execute(
                    "SELECT COUNT(*) FROM outbox WHERE subscription=? AND status NOT IN ('sent','dropped')",
                    (ident,),
                ).fetchone()[0]
                if (
                    queued
                    + sum(
                        matches(args, line, latest.get("own_nick", ""))
                        for line in lines
                    )
                    > 10000
                ):
                    return
                recovered = {
                    **latest,
                    "lines": lines,
                    "evicted_through": 0,
                    "native_retained_from": None,
                }
                with self.db:
                    self.db.execute(
                        "UPDATE subscriptions SET truncated=coverage_lost,coverage_state=CASE WHEN coverage_lost=1 THEN 'degraded' ELSE 'continuous' END,recovery_attempts=0 WHERE id=?",
                        (ident,),
                    )
                    self._ingest(ident, recovered)
                    self.db.execute(
                        "UPDATE coverage_audit SET state='recovered',resolved=? WHERE subscription=? AND incarnation=? AND generation=?",
                        (self.clock(), ident, row["incarnation"], generation),
                    )
                self.wake.set()
            elif (
                row["recovery_mode"] == "resume"
                and loss_known
                and row["coverage_reason"] != "queue_capacity"
            ):
                # Explicit loss policy: preserve queued work and never reorder
                # it by backfilling prior events under new outbox cursors.
                high = row["intake"]
                if latest["epoch"] != row["epoch"]:
                    high = max((x["sequence"] for x in latest["lines"]), default=0)
                    if isinstance(latest.get("last_assigned_id"), int):
                        high = max(high, latest["last_assigned_id"] + 1)
                elif row["coverage_reason"] == "native_window_gap":
                    # Replay only retained post-anchor lines. Missing older
                    # lines are explicitly acknowledged, not invented.
                    high = max(high, latest.get("evicted_through", 0))
                with self.db:
                    boundary = json.loads(row["coverage_anchor"])
                    boundary.update(
                        resumeEpoch=latest["epoch"],
                        resumeIntake=high,
                        policy="resume",
                        warning="Historical messages may be unavailable; queued work was preserved.",
                    )
                    self.db.execute(
                        "UPDATE subscriptions SET epoch=?,intake=?,coverage_state='degraded',coverage_lost=1,loss_generation=coverage_generation,coverage_anchor=?,recovery_attempts=0 WHERE id=?",
                        (latest["epoch"], high, canonical(boundary), ident),
                    )
                    self.db.execute(
                        "UPDATE coverage_audit SET state='acknowledged-loss',boundary=?,resolved=? WHERE subscription=? AND incarnation=? AND generation=?",
                        (
                            canonical(boundary),
                            self.clock(),
                            ident,
                            row["incarnation"],
                            generation,
                        ),
                    )
                    self._ingest(
                        ident,
                        {**latest, "evicted_through": 0, "native_retained_from": None},
                    )
                self.wake.set()
        except (ConnectionError, asyncio.TimeoutError, EventError):
            # Remains durably held; the existing bounded backoff schedules retry.
            return

    def _lock(self, ident):
        lock = self.locks.get(ident)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[ident] = lock
        return lock

    def _owner(self, owner):
        if not isinstance(owner, str) or not owner or not self.owner_allowed(owner):
            raise EventError(-32001, "Event owner is not authorized")

    def _identity(self, owner, params):
        self._owner(owner)
        if params.get("name") != EVENT_NAME:
            raise EventError(-32602, "Unknown event name")
        arguments = params.get("arguments")
        try:
            validate(arguments, INPUT_SCHEMA)
        except ValidationError:
            raise EventError(-32602, "Invalid channel or trigger arguments") from None
        delivery = params.get("delivery", {})
        if delivery.get("mode") != "webhook":
            raise EventError(-32602, "Only webhook delivery is supported")
        try:
            url = validate_url(delivery.get("url"))
        except WebhookError as error:
            raise EventError(
                -32015, "Callback endpoint rejected", error.reason
            ) from None
        ident = (
            "sub_"
            + hashlib.sha256(
                canonical([owner, url, EVENT_NAME, arguments]).encode()
            ).hexdigest()
        )
        return ident, arguments, url

    async def _history(self, channel, cursor=None):
        try:
            return await self.backend.request(
                "event_history",
                uri="weechat://buffer/" + quote(channel, safe=""),
                **({"cursor": cursor} if cursor is not None else {}),
            )
        except ValueError:
            raise EventError(-32001, "Channel is not an exposed IRC channel") from None

    async def list_events(self, owner, cursor=None):
        self._owner(owner)
        if cursor is not None:
            raise EventError(-32602, "Invalid event catalog cursor")
        return {"events": [DEFINITION]}

    def _cursor(self, ident, position):
        return ident + ":" + str(position)

    def _parse_cursor(self, ident, cursor):
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not re.fullmatch(
            re.escape(ident) + r":(?:0|[1-9][0-9]{0,18})", cursor
        ):
            raise EventError(-32602, "Invalid subscription cursor")
        return int(cursor.rsplit(":", 1)[1])

    async def _verify(self, owner, ident, url, secret):
        now = self.clock()
        key = (owner, url)
        if self.verified.get(key, 0) > now:
            return
        challenge = secrets.token_urlsafe(32)
        body = canonical({"type": "verification", "challenge": challenge}).encode()
        headers = signed_headers(
            "verify_" + secrets.token_hex(16), body, secret, ident, int(now)
        )
        try:
            status, response = await self.sender.post(url, body, headers)
            value = json.loads(response)
            echoed = value.get("challenge") if isinstance(value, dict) else None
            if (
                not 200 <= status < 300
                or not isinstance(echoed, str)
                or not hmac.compare_digest(echoed, challenge)
            ):
                raise WebhookError("challenge_failed")
        except (ValueError, UnicodeError, TypeError):
            raise EventError(
                -32015, "Callback verification failed", "challenge_failed"
            ) from None
        except WebhookError as error:
            raise EventError(
                -32015, "Callback verification failed", error.reason
            ) from None
        if self.clock() - now > 30:
            raise EventError(-32015, "Callback verification failed", "timeout")
        # Cache is memory-only and bounded by time AND size, keyed to principal.
        self.verified = {
            key: expiry for key, expiry in self.verified.items() if expiry > now
        }
        if len(self.verified) >= 1000:
            self.verified.pop(next(iter(self.verified)))
        self.verified[key] = now + 300

    async def subscribe(self, owner, params):
        ident, arguments, url = self._identity(owner, params)
        if "jev" in arguments and self.judge is None:
            raise EventError(-32602, "Jev filtering is not enabled by the operator")
        if "jev" in arguments and not arguments["jev"]["preference"].strip():
            raise EventError(-32602, "Jev preference must not be blank")
        secret = params["delivery"].get("secret")
        try:
            validate_secret(secret)
        except WebhookError:
            raise EventError(-32602, "Invalid webhook signing secret") from None
        cursor = self._parse_cursor(ident, params.get("cursor"))
        recovery = params.get("recovery")
        if recovery is not None and (
            not isinstance(recovery, dict)
            or set(recovery) - {"mode", "expectedIncarnation"}
            or recovery.get("mode") not in ("prove", "resume")
            or (
                recovery.get("expectedIncarnation") is not None
                and not re.fullmatch(
                    r"[a-f0-9]{32}", str(recovery["expectedIncarnation"])
                )
            )
        ):
            raise EventError(-32602, "Invalid recovery policy")
        ttl = params.get("ttlMs", 3600000)
        if ttl is not None and (type(ttl) is not int or ttl <= 0):
            raise EventError(-32602, "ttlMs must be a positive integer or null")
        # Finite lifetime even when the host requests no expiration.
        lifetime = min(ttl / 1000 if ttl is not None else 3600, 86400)
        async with self._lock(ident):
            snapshot = await self._history(arguments["channel_id"])
            old = self.db.execute(
                "SELECT * FROM subscriptions WHERE id=?", (ident,)
            ).fetchone()
            if (
                old is None
                and self.db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
                >= 100
            ):
                raise EventError(-32000, "Event subscription limit reached")
            if (
                recovery
                and recovery.get("expectedIncarnation")
                and (not old or old["incarnation"] != recovery["expectedIncarnation"])
            ):
                raise EventError(-32000, "Subscription incarnation changed")
            if old and old["suspended"]:
                raise EventError(
                    -32000,
                    "Delivery stopped; unsubscribe and explicitly subscribe again",
                )
            if old and cursor is not None and cursor > old["acknowledged"]:
                raise EventError(-32602, "Cursor is ahead of acknowledged delivery")
            await self._verify(owner, ident, url, secret)
            self._owner(owner)
            latest = await self._history(
                arguments["channel_id"]
            )  # Recheck scope after verification.
            expires = self.clock() + lifetime
            # Intake/retention can change the row while callback verification awaits.
            old = self.db.execute(
                "SELECT * FROM subscriptions WHERE id=?", (ident,)
            ).fetchone()
            if (
                recovery
                and recovery.get("expectedIncarnation")
                and (not old or old["incarnation"] != recovery["expectedIncarnation"])
            ):
                raise EventError(-32000, "Subscription incarnation changed")
            if old and old["suspended"]:
                raise EventError(
                    -32000,
                    "Delivery stopped; unsubscribe and explicitly subscribe again",
                )
            if old and cursor is not None and cursor > old["acknowledged"]:
                raise EventError(-32602, "Cursor is ahead of acknowledged delivery")
            # A numeric range is not an incarnation proof: same-ID recreation
            # can later ACK past an old cursor. Require the exact retained receipt
            # (AUTOINCREMENT positions are never reused), or a pinned incarnation.
            continuity = bool(
                old
                and recovery
                and recovery.get("expectedIncarnation") == old["incarnation"]
            )
            if (
                old
                and cursor is not None
                and cursor > 0
                and cursor <= old["acknowledged"]
            ):
                continuity = (
                    continuity
                    or self.db.execute(
                        "SELECT 1 FROM outbox WHERE subscription=? AND position=? AND status IN ('sent','dropped')",
                        (ident, cursor),
                    ).fetchone()
                    is not None
                )
            with self.db:
                if old:
                    # Omitting the option is a real opt-out, not a sticky
                    # grant to keep accepting historical loss indefinitely.
                    self.db.execute(
                        "UPDATE subscriptions SET recovery_mode=?,recovery_enrolled=MAX(recovery_enrolled,?) WHERE id=?",
                        (
                            recovery["mode"] if recovery else "off",
                            int(bool(recovery)),
                            ident,
                        ),
                    )
                    self.db.execute(
                        """UPDATE subscriptions SET secret=?,old_secret=?,rotate_until=?,expires=?,suspended=0 WHERE id=?""",
                        (
                            secret,
                            old["secret"]
                            if old["secret"] != secret
                            else old["old_secret"],
                            self.clock() + 300
                            if old["secret"] != secret
                            else old["rotate_until"],
                            expires,
                            ident,
                        ),
                    )
                    current = self.db.execute(
                        "SELECT * FROM subscriptions WHERE id=?", (ident,)
                    ).fetchone()
                    if (
                        recovery
                        and not recovery.get("expectedIncarnation")
                        and "cursor" in params
                        and not continuity
                    ):
                        # Enrollment with an absent/pruned legacy anchor needs an
                        # honest gap decision. Never claim same-incarnation proof.
                        boundary = json.loads(current["coverage_anchor"] or "{}")
                        if not (
                            current["coverage_state"] == "degraded"
                            and boundary.get("enrollmentCursor", "missing") == cursor
                        ):
                            self._gap(
                                current,
                                "replay_pruned"
                                if cursor is not None
                                and 0 < cursor <= old["pruned_through"]
                                else "legacy_unknown",
                                latest,
                            )
                            boundary = json.loads(
                                self.db.execute(
                                    "SELECT coverage_anchor FROM subscriptions WHERE id=?",
                                    (ident,),
                                ).fetchone()[0]
                            )
                            boundary["enrollmentCursor"] = cursor
                            self.db.execute(
                                "UPDATE subscriptions SET coverage_anchor=? WHERE id=?",
                                (canonical(boundary), ident),
                            )
                            self.db.execute(
                                "UPDATE coverage_audit SET boundary=? WHERE subscription=? AND incarnation=? AND generation=(SELECT coverage_generation FROM subscriptions WHERE id=?)",
                                (
                                    canonical(boundary),
                                    ident,
                                    current["incarnation"],
                                    ident,
                                ),
                            )
                    elif cursor is not None and cursor < old["pruned_through"]:
                        self._gap(current, "replay_pruned")
                    if cursor is not None and cursor < old["acknowledged"]:
                        self.db.execute(
                            "UPDATE subscriptions SET acknowledged=?,truncated=MAX(truncated,?) WHERE id=?",
                            (
                                cursor,
                                int(cursor < old["pruned_through"]),
                                ident,
                            ),
                        )
                        self.db.execute(
                            "UPDATE outbox SET status='pending',attempts=0,due=0 WHERE subscription=? AND position>? AND "
                            + (
                                "status='sent'"
                                if recovery or old["recovery_mode"] != "off"
                                else "status!='dropped'"
                            ),
                            (ident, cursor),
                        )
                    self._ingest(ident, latest)
                else:
                    # The callback awaits outside a global lock; enforce quota again
                    # inside the atomic insertion to cover concurrent identities.
                    if (
                        self.db.execute(
                            "SELECT COUNT(*) FROM subscriptions"
                        ).fetchone()[0]
                        >= 100
                    ):
                        raise EventError(-32000, "Event subscription limit reached")
                    high = max(
                        (line["sequence"] for line in snapshot["lines"]), default=0
                    )
                    last_assigned = snapshot.get("last_assigned_id")
                    if isinstance(last_assigned, int):
                        high = max(high, last_assigned + 1)
                    self.db.execute(
                        """INSERT INTO subscriptions(id,owner,name,arguments,url,secret,expires,epoch,intake,truncated,seen_native_line,incarnation,recovery_mode,recovery_enrolled) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            ident,
                            owner,
                            EVENT_NAME,
                            canonical(arguments),
                            url,
                            secret,
                            expires,
                            snapshot["epoch"],
                            high,
                            int(cursor is not None),
                            int(bool(snapshot["lines"])),
                            secrets.token_hex(16),
                            recovery["mode"] if recovery else "off",
                            int(bool(recovery)),
                        ),
                    )
            row = self.db.execute(
                "SELECT * FROM subscriptions WHERE id=?", (ident,)
            ).fetchone()
            if row["truncated"] and row["coverage_generation"] == 0:
                with self.db:
                    self._gap(row, "legacy_unknown")
            await self._reconcile(ident)
            row = self.db.execute(
                "SELECT * FROM subscriptions WHERE id=?", (ident,)
            ).fetchone()
            self.notify(arguments["channel_id"])
            return {
                "id": ident,
                "refreshBefore": utc(expires),
                "cursor": self._cursor(ident, row["acknowledged"]),
                "truncated": bool(row["truncated"]),
                "coverage": self._coverage(row, continuity),
            }

    async def unsubscribe(self, owner, params):
        ident, _, _ = self._identity(owner, params)
        async with self._lock(ident):
            with self.db:
                self.db.execute(
                    "DELETE FROM subscriptions WHERE id=? AND owner=?", (ident, owner)
                )
        self.wake.set()
        return {}

    def _ingest(self, ident, snapshot):
        row = self.db.execute(
            "SELECT * FROM subscriptions WHERE id=?", (ident,)
        ).fetchone()
        if not row:
            return
        if row["recovery_mode"] != "off" and row["coverage_state"] in (
            "gap",
            "recovering",
        ):
            return
        same_epoch = row["epoch"] == snapshot["epoch"]
        high = row["intake"]
        if not same_epoch:
            # A new/unknown native buffer lifetime cannot prove which retained
            # lines arrived after this grant. Preserve queued events, flag the
            # interruption, and resume from the current tip rather than replay
            # pre-opt-in history with new event identities.
            high = max((line["sequence"] for line in snapshot["lines"]), default=0)
            last_assigned = snapshot.get("last_assigned_id")
            if isinstance(last_assigned, int):
                high = max(high, last_assigned + 1)
        retained_from = snapshot.get("native_retained_from")
        truncated = (
            not same_epoch
            or snapshot.get("evicted_through", 0) > high
            # Native IDs may be sparse timestamps. Do not infer loss on the
            # first arrival after an empty opt-in; later loss is conservative
            # when the saved position no longer reaches native retention.
            or (
                row["seen_native_line"]
                and high > 0
                and retained_from is not None
                and retained_from > high
            )
        )
        if truncated:
            reason = (
                "native_epoch_changed"
                if not same_epoch
                else "native_window_gap"
                if snapshot.get("evicted_through", 0) > row["intake"]
                and snapshot.get("next_cursor")
                else "native_retention_gap"
            )
            self._gap(row, reason, snapshot)
            if row["recovery_mode"] != "off":
                return
        args = json.loads(row["arguments"])
        retained = self.db.execute(
            "SELECT COUNT(*) FROM outbox WHERE subscription=? AND status NOT IN ('sent','dropped')",
            (ident,),
        ).fetchone()[0]
        for line in snapshot["lines"]:
            seq = line["sequence"]
            if seq <= high:
                continue
            if not matches(args, line, snapshot.get("own_nick", "")):
                high = seq
                continue
            if retained >= 10000:
                self._gap(row, "queue_capacity", snapshot)
                truncated = True
                self.db.execute(
                    "UPDATE subscriptions SET suspended=1 WHERE id=?", (ident,)
                )
                break
            high = seq
            event_key = hashlib.sha256(
                canonical([args["channel_id"], snapshot["epoch"], seq]).encode()
            ).hexdigest()
            try:
                timestamp = utc(float(line["date"]))
            except (ValueError, TypeError, OverflowError):
                timestamp = utc(self.clock())
            event = {
                "eventId": "evt_" + event_key,
                "name": EVENT_NAME,
                "timestamp": timestamp,
                "data": {
                    "channel_id": args["channel_id"],
                    "message_id": snapshot["epoch"] + ":" + str(seq),
                    "nick": line["nick"],
                    "text": line["message"],
                },
            }
            added = self.db.execute(
                "INSERT OR IGNORE INTO outbox(subscription,event_key,payload,created) VALUES(?,?,?,?)",
                (ident, event_key, canonical(event), self.clock()),
            )
            if added.rowcount:
                retained += 1
                event["cursor"] = self._cursor(ident, added.lastrowid)
                payload = canonical(event)
                if len(payload.encode()) > 262144:
                    # Never truncate signed data silently or advance beyond it.
                    self.db.execute(
                        "UPDATE outbox SET status='failed' WHERE position=?",
                        (added.lastrowid,),
                    )
                    self.db.execute(
                        "UPDATE subscriptions SET suspended=1 WHERE id=?", (ident,)
                    )
                context = None
                if "jev" in args:
                    context = canonical(
                        filter_context(args, snapshot, line, event["data"])
                    )
                self.db.execute(
                    "UPDATE outbox SET payload=?,filter_context=? WHERE position=?",
                    (payload, context, added.lastrowid),
                )
        self.db.execute(
            "UPDATE subscriptions SET epoch=?,intake=?,truncated=MAX(truncated,?),seen_native_line=? WHERE id=?",
            (
                snapshot["epoch"],
                high,
                int(truncated),
                int(
                    bool(snapshot["lines"]) or (same_epoch and row["seen_native_line"])
                ),
                ident,
            ),
        )

    def notify(self, channel=None):
        # Called synchronously from the IPC reader; never await another IPC request there.
        self.dirty.add(channel)
        self.wake.set()
        self.intake_wake.set()

    async def collect(self, channel=None):
        rows = self.db.execute(
            "SELECT * FROM subscriptions WHERE expires>?", (self.clock(),)
        ).fetchall()
        snapshots = {}
        for row in rows:
            if self.stopping:
                return
            args = json.loads(row["arguments"])
            buffer = args["channel_id"]
            if channel is not None and buffer != channel:
                continue
            if not self.owner_allowed(row["owner"]):
                with self.db:
                    self.db.execute(
                        "DELETE FROM subscriptions WHERE id=?", (row["id"],)
                    )
                continue
            try:
                if buffer not in snapshots:
                    snapshots[buffer] = await self._history(buffer)
            except ConnectionError:
                self.dirty.add(buffer)
                continue
            except EventError:
                with self.db:
                    self.db.execute(
                        "DELETE FROM subscriptions WHERE id=?", (row["id"],)
                    )
                continue
            if self.stopping:
                return
            with self.db:
                self._ingest(row["id"], snapshots[buffer])
            # Slow callback delivery must never block intake. Reconciliation
            # is optional here and retries on the next wake if the lock is busy.
            lock = self._lock(row["id"])
            if not lock.locked():
                async with lock:
                    await self._reconcile(row["id"])

    async def deliver_once(self):
        now = self.clock()
        with self.db:
            self.db.execute(
                "UPDATE subscriptions SET old_secret=NULL WHERE rotate_until<=?", (now,)
            )
            self.db.execute(
                """UPDATE subscriptions SET pruned_through=MAX(pruned_through,
                COALESCE((SELECT MAX(position) FROM outbox WHERE subscription=subscriptions.id
                AND status IN ('sent','dropped') AND created<?),0))""",
                (now - 86400,),
            )
            self.db.execute(
                "DELETE FROM outbox WHERE status IN ('sent','dropped') AND created<?",
                (now - 86400,),
            )
            self.db.execute(
                "DELETE FROM subscriptions WHERE expires<? AND recovery_enrolled=0",
                (now - 86400,),
            )
            for item in self.db.execute("SELECT id FROM subscriptions").fetchall():
                cutoff = self.db.execute(
                    "SELECT position FROM outbox WHERE subscription=? AND status='sent' ORDER BY position DESC LIMIT 1 OFFSET 9999",
                    (item["id"],),
                ).fetchone()
                if cutoff:
                    removed = self.db.execute(
                        "SELECT MAX(position) FROM outbox WHERE subscription=? AND status IN ('sent','dropped') AND position<?",
                        (item["id"], cutoff[0]),
                    ).fetchone()[0]
                    if removed:
                        self.db.execute(
                            "UPDATE subscriptions SET pruned_through=MAX(pruned_through,?) WHERE id=?",
                            (removed, item["id"]),
                        )
                        self.db.execute(
                            "DELETE FROM outbox WHERE subscription=? AND status IN ('sent','dropped') AND position<=?",
                            (item["id"], removed),
                        )
        # Retry one due coverage reconciliation per scheduler round, even if no
        # further channel messages arrive. No writes occur before its due time.
        retry = self.db.execute(
            "SELECT id FROM subscriptions WHERE expires>? AND recovery_mode!='off' AND coverage_state IN ('gap','recovering') AND recovery_due<=? ORDER BY recovery_due,id LIMIT 1",
            (now, now),
        ).fetchone()
        if retry:
            lock = self._lock(retry["id"])
            if not lock.locked():
                async with lock:
                    await self._reconcile(retry["id"])
        rows = self.db.execute(
            "SELECT id FROM subscriptions WHERE expires>? AND suspended=0", (now,)
        ).fetchall()
        await asyncio.gather(*(self._deliver(row["id"]) for row in rows))

    async def _deliver(self, ident):
        async with self.send_slots, self._lock(ident):
            sub = self.db.execute(
                "SELECT * FROM subscriptions WHERE id=? AND expires>? AND suspended=0",
                (ident, self.clock()),
            ).fetchone()
            if sub is None or (
                sub["recovery_mode"] != "off"
                and sub["coverage_state"] in ("gap", "recovering")
            ):
                return
            if not self.owner_allowed(sub["owner"]):
                with self.db:
                    self.db.execute(
                        "DELETE FROM subscriptions WHERE id=?", (sub["id"],)
                    )
                return
            pending = self.db.execute(
                "SELECT * FROM outbox WHERE subscription=? AND status NOT IN ('sent','dropped') ORDER BY position LIMIT 1",
                (sub["id"],),
            ).fetchone()
            if (
                not pending
                or pending["status"] == "failed"
                or pending["due"] > self.clock()
            ):
                return
            try:
                # Recheck live scope immediately before every network send.
                await self._history(json.loads(sub["arguments"])["channel_id"])
            except (ConnectionError, asyncio.TimeoutError):
                with self.db:
                    self.db.execute(
                        "UPDATE outbox SET due=? WHERE position=?",
                        (self.clock() + 1, pending["position"]),
                    )
                return
            except EventError:
                with self.db:
                    self.db.execute(
                        "DELETE FROM subscriptions WHERE id=?", (sub["id"],)
                    )
                return
            if (
                self.stopping
                or sub["expires"] <= self.clock()
                or not self.owner_allowed(sub["owner"])
            ):
                return
            if pending["filter_context"] is not None and pending["decision"] is None:
                try:
                    if self.judge is None:
                        raise FilterError("filter_unavailable")
                    decision = await asyncio.wait_for(
                        self.judge.decide(json.loads(pending["filter_context"])),
                        timeout=12,
                    )
                    if decision not in ("send", "drop"):
                        raise FilterError("invalid_decision")
                except (FilterError, asyncio.TimeoutError):
                    # An unavailable classifier is not a semantic drop. Stop this
                    # grant visibly rather than leaking messages or silently losing them.
                    with self.db:
                        self.db.execute(
                            "UPDATE outbox SET status='failed' WHERE position=?",
                            (pending["position"],),
                        )
                        self.db.execute(
                            "UPDATE subscriptions SET suspended=1 WHERE id=?", (ident,)
                        )
                    return
                # Persist before webhook I/O. Webhook retries/replay never rerun Jev.
                with self.db:
                    self.db.execute(
                        "UPDATE outbox SET decision=? WHERE position=?",
                        (decision, pending["position"]),
                    )
                    if decision == "drop":
                        self.db.execute(
                            "UPDATE outbox SET status='dropped' WHERE position=?",
                            (pending["position"],),
                        )
                        self.db.execute(
                            "UPDATE subscriptions SET acknowledged=? WHERE id=?",
                            (pending["position"], ident),
                        )
                if decision == "drop":
                    return
                # Awaiting Jev may outlive expiry, owner revocation or channel scope.
                if (
                    self.stopping
                    or sub["expires"] <= self.clock()
                    or not self.owner_allowed(sub["owner"])
                ):
                    return
                try:
                    await self._history(json.loads(sub["arguments"])["channel_id"])
                except (ConnectionError, asyncio.TimeoutError):
                    return
                except EventError:
                    with self.db:
                        self.db.execute(
                            "DELETE FROM subscriptions WHERE id=?", (ident,)
                        )
                    return
            if (
                self.stopping
                or sub["expires"] <= self.clock()
                or not self.owner_allowed(sub["owner"])
            ):
                return
            keys = [sub["secret"]]
            if sub["old_secret"] and sub["rotate_until"] > self.clock():
                keys.append(sub["old_secret"])
            body = pending["payload"].encode()
            event = json.loads(body)
            headers = signed_headers(
                event["eventId"], body, keys, sub["id"], int(self.clock())
            )
            automatic = sub["recovery_mode"] != "off"
            if pending["attempts"] >= 8 and (
                not automatic
                or pending["failure_class"] not in ("transient", "inflight")
            ):
                with self.db:
                    self.db.execute(
                        "UPDATE outbox SET status='failed' WHERE position=?",
                        (pending["position"],),
                    )
                    self.db.execute(
                        "UPDATE subscriptions SET suspended=1 WHERE id=?", (ident,)
                    )
                return
            attempts = pending["attempts"] + 1
            # Count before I/O, including crashes with uncertain receipt.
            with self.db:
                self.db.execute(
                    "UPDATE outbox SET attempts=?,due=?,failure_class='inflight' WHERE position=?",
                    (
                        attempts,
                        self.clock() + min(900, 2 ** min(attempts, 10))
                        if automatic
                        else pending["due"],
                        pending["position"],
                    ),
                )
            reason = None
            try:
                status, _ = await self.sender.post(sub["url"], body, headers)
            except WebhookError as error:
                status, reason = 0, error.reason
            with self.db:
                if 200 <= status < 300:
                    self.db.execute(
                        "UPDATE outbox SET status='sent',failure_class=NULL WHERE position=?",
                        (pending["position"],),
                    )
                    self.db.execute(
                        "UPDATE subscriptions SET acknowledged=? WHERE id=?",
                        (pending["position"], sub["id"]),
                    )
                elif (
                    status in (408, 429)
                    or status >= 500
                    or (
                        status == 0
                        and reason in ("timeout", "dns_error", "connection_failed")
                    )
                ) and (attempts < 8 or automatic):
                    self.db.execute(
                        "UPDATE outbox SET due=?,failure_class='transient' WHERE position=?",
                        (
                            self.clock()
                            + min(900 if automatic else 300, 2 ** min(attempts, 10)),
                            pending["position"],
                        ),
                    )
                else:
                    self.db.execute(
                        "UPDATE outbox SET status='failed',failure_class='permanent' WHERE position=?",
                        (pending["position"],),
                    )
                    self.db.execute(
                        "UPDATE subscriptions SET suspended=1 WHERE id=?",
                        (sub["id"],),
                    )

    async def ingest_loop(self):
        while not self.stopping:
            await self.intake_wake.wait()
            self.intake_wake.clear()
            dirty, self.dirty = self.dirty, set()
            try:
                if None in dirty:
                    await self.collect()
                else:
                    for channel in dirty:
                        await self.collect(channel)
            except (ConnectionError, asyncio.TimeoutError):
                self.dirty.add(None)
            if self.stopping:
                return
            self.wake.set()
            if self.dirty:
                # Retry temporarily unavailable local channels without dropping grants.
                await asyncio.sleep(1)
                self.intake_wake.set()

    async def run(self):
        while not self.stopping:
            self.wake.clear()
            await self.deliver_once()
            if self.stopping:
                return
            ready = self.db.execute(
                """SELECT 1 FROM subscriptions s JOIN outbox o ON o.subscription=s.id
                WHERE s.expires>? AND s.suspended=0 AND (s.recovery_mode='off' OR s.coverage_state NOT IN ('gap','recovering')) AND o.status='pending' AND o.due<=?
                AND o.position=(SELECT MIN(position) FROM outbox WHERE subscription=s.id AND status NOT IN ('sent','dropped'))
                LIMIT 1""",
                (self.clock(), self.clock()),
            ).fetchone()
            if ready:
                # Drain bursts promptly, yielding to intake and unsubscribe handlers.
                await asyncio.sleep(0)
                continue
            # Python 3.10 wait_for can swallow external cancellation when its
            # inner wait completes concurrently. Await the event directly so
            # shutdown cannot lose cancellation to an intake wakeup.
            timer = asyncio.get_running_loop().call_later(1, self.wake.set)
            try:
                await self.wake.wait()
            finally:
                timer.cancel()

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run())
            self.intake_task = asyncio.create_task(self.ingest_loop())
            self.notify()

    async def close(self):
        if self.closed:
            return
        # Also bound shutdown if an IPC wait swallowed cancellation on 3.10.
        self.stopping = True
        tasks = [task for task in (self.task, self.intake_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.db.close()
        os.close(self.file)
        self.closed = True
