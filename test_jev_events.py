# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import test_mcp_events as fixtures
from jev_filter import FilterError
from mcp_events import EventError, EventManager


class Judge:
    def __init__(self):
        self.calls = []
        self.result: Any = "send"
        self.gate = None

    async def decide(self, context):
        self.calls.append(context)
        if self.gate:
            await self.gate.wait()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class JevEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "events.db")
        self.backend, self.sender = fixtures.Backend(), fixtures.Sender()
        self.now = 1700000000.0
        self.allowed = {"owner"}
        self.manager = self.make_manager()

    asyncTearDown = fixtures.EventTests.asyncTearDown
    params = fixtures.EventTests.params
    subscribe = fixtures.EventTests.subscribe

    def make_manager(self):
        if not hasattr(self, "judge"):
            self.judge = Judge()
        return EventManager(
            self.path,
            self.backend,
            lambda value: value in self.allowed,
            sender=self.sender,
            clock=lambda: self.now,
            judge=self.judge,
        )

    async def filtered(self, preference="Send questions", **options):
        return await self.subscribe(
            arguments={
                "channel_id": fixtures.CHANNEL,
                "trigger": "message",
                "jev": {"preference": preference, **options},
            }
        )

    async def test_opt_in_and_schema(self):
        await self.subscribe()
        self.backend.message(1)
        await self.manager.collect()
        await self.manager.deliver_once()
        self.assertFalse(self.judge.calls)
        self.manager.judge = None
        with self.assertRaisesRegex(EventError, "not enabled"):
            await self.filtered()
        self.manager.judge = self.judge
        for pref in ("", "  ", "x" * 2049):
            with self.assertRaises(EventError):
                await self.filtered(pref)
        for limit in (-1, 51, True):
            with self.assertRaises(EventError):
                await self.filtered(history_limit=limit)
        with self.assertRaises(EventError):
            await self.subscribe(
                arguments={
                    "channel_id": fixtures.CHANNEL,
                    "trigger": "mention",
                    "nick": "dip",
                    "jev": {"preference": "test"},
                }
            )

    async def test_all_messages_context_no_future_lines_and_no_webhook_expansion(self):
        self.backend.message(1, "before subscription")
        await self.filtered(history_limit=2)
        self.backend.message(2, "own history", nick="dot")
        self.backend.message(3, "status")
        self.backend.snapshots[fixtures.CHANNEL]["lines"][-1]["is_message"] = False
        self.backend.message(4, "a question without mention?")
        self.backend.message(5, "future")
        await self.manager.collect()
        await self.manager.deliver_once()
        context = self.judge.calls[0]
        self.assertEqual(context["preference"], "Send questions")
        self.assertEqual(
            [x["text"] for x in context["history"]],
            ["before subscription", "own history"],
        )
        self.assertEqual(context["message"]["text"], "a question without mention?")
        event = json.loads(self.sender.deliveries[0][1])
        self.assertNotIn("history", event)
        self.assertNotIn("jev", event)

    async def test_drop_is_durable_does_not_block_next_and_cannot_replay(self):
        sub = await self.filtered()
        self.backend.message(1, "drop me")
        self.backend.message(2, "send me")
        await self.manager.collect()
        self.judge.result = "drop"
        await self.manager.deliver_once()
        self.assertFalse(self.sender.deliveries)
        self.judge.result = "send"
        await self.manager.deliver_once()
        self.assertEqual(len(self.sender.deliveries), 1)
        await self.manager.close()
        self.manager = self.make_manager()
        await self.filtered()
        p = self.params(
            arguments={
                "channel_id": fixtures.CHANNEL,
                "trigger": "message",
                "jev": {"preference": "Send questions"},
            },
            cursor=sub["id"] + ":0",
        )
        await self.manager.subscribe("owner", p)
        await self.manager.deliver_once()
        self.assertEqual(len(self.judge.calls), 2)
        self.assertEqual(
            [json.loads(x[1])["data"]["text"] for x in self.sender.deliveries],
            ["send me", "send me"],
        )

    async def test_send_decision_cached_across_webhook_retry_and_restart(self):
        await self.filtered()
        self.backend.message(1)
        await self.manager.collect()
        self.sender.status = 503
        await self.manager.deliver_once()
        await self.manager.close()
        self.manager = self.make_manager()
        self.now += 10
        self.sender.status = 204
        self.judge.result = "drop"
        await self.manager.deliver_once()
        self.assertEqual(len(self.judge.calls), 1)
        self.assertEqual(len(self.sender.deliveries), 2)
        self.assertEqual(self.sender.deliveries[0][1], self.sender.deliveries[1][1])

    async def test_error_suspends_without_forward_or_silent_drop(self):
        for result in (FilterError("timeout"), "anything", True):
            with self.subTest(result=result):
                sub = await self.filtered(str(result))
                self.backend.message(
                    len(self.backend.snapshots[fixtures.CHANNEL]["lines"]) + 1
                )
                await self.manager.collect()
                self.judge.result = result
                await self.manager.deliver_once()
                row = self.manager.db.execute(
                    "SELECT suspended FROM subscriptions WHERE id=?", (sub["id"],)
                ).fetchone()
                self.assertEqual(row[0], 1)
                self.assertFalse(self.sender.deliveries)
                with self.assertRaisesRegex(EventError, "Delivery stopped"):
                    await self.filtered(str(result))

    async def test_preference_isolation_and_changed_preference_new_grant(self):
        one = await self.filtered("questions")
        two = await self.filtered("deployments")
        self.assertNotEqual(one["id"], two["id"])
        self.backend.message(1)
        await self.manager.collect()
        await self.manager.deliver_once()
        self.assertEqual(
            {c["preference"] for c in self.judge.calls}, {"questions", "deployments"}
        )

    async def test_bounded_context_and_own_message_not_triggered(self):
        for seq in range(1, 60):
            self.backend.message(seq, "x" * 4096)
        await self.filtered(history_limit=50)
        self.backend.message(60, "current")
        self.backend.message(61, "own", nick="dot")
        await self.manager.collect()
        await self.manager.deliver_once()
        context = self.judge.calls[0]
        self.assertLess(len(context["history"]), 8)
        self.assertTrue(context["history_truncated"])
        await self.manager.deliver_once()
        self.assertEqual(len(self.judge.calls), 1)

    async def test_slow_judge_intake_continues_and_unsubscribe_waits(self):
        await self.filtered()
        self.backend.message(1)
        await self.manager.collect()
        self.judge.gate = asyncio.Event()
        delivery = asyncio.create_task(self.manager.deliver_once())
        while not self.judge.calls:
            await asyncio.sleep(0)
        self.backend.message(2)
        await self.manager.collect()
        self.assertEqual(
            self.manager.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 2
        )
        params = self.params(
            arguments={
                "channel_id": fixtures.CHANNEL,
                "trigger": "message",
                "jev": {"preference": "Send questions"},
            }
        )
        stopping = asyncio.create_task(self.manager.unsubscribe("owner", params))
        await asyncio.sleep(0)
        self.assertFalse(stopping.done())
        self.judge.gate.set()
        await delivery
        await stopping
        self.assertFalse(self.manager.db.execute("SELECT * FROM outbox").fetchall())

    async def test_expiry_or_revocation_during_judge_prevents_callback(self):
        await self.filtered()
        self.backend.message(1)
        await self.manager.collect()
        self.judge.gate = asyncio.Event()
        task = asyncio.create_task(self.manager.deliver_once())
        while not self.judge.calls:
            await asyncio.sleep(0)
        self.allowed.clear()
        self.judge.gate.set()
        await task
        self.assertFalse(self.sender.deliveries)

    async def test_revocation_during_scope_read_prevents_provider_disclosure(self):
        await self.filtered()
        self.backend.message(1)
        await self.manager.collect()
        original = self.backend.request

        async def revoke(*args, **kwargs):
            result = await original(*args, **kwargs)
            self.allowed.clear()
            return result

        with patch.object(self.backend, "request", revoke):
            await self.manager.deliver_once()
        self.assertFalse(self.judge.calls)
        self.assertFalse(self.sender.deliveries)

    async def test_revocation_after_decision_scope_read_prevents_webhook(self):
        await self.filtered()
        self.backend.message(1)
        await self.manager.collect()
        original = self.backend.request
        reads = 0

        async def revoke(*args, **kwargs):
            nonlocal reads
            result = await original(*args, **kwargs)
            reads += 1
            if reads == 2:
                self.allowed.clear()
            return result

        with patch.object(self.backend, "request", revoke):
            await self.manager.deliver_once()
        self.assertEqual(len(self.judge.calls), 1)
        self.assertFalse(self.sender.deliveries)

    async def test_zero_history_and_drop_retention(self):
        self.backend.message(1, "old")
        await self.filtered(history_limit=0)
        self.backend.message(2, "new")
        await self.manager.collect()
        self.judge.result = "drop"
        await self.manager.deliver_once()
        self.assertEqual(self.judge.calls[0]["history"], [])
        self.now += 86401
        await self.manager.deliver_once()
        self.assertEqual(
            self.manager.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0
        )

    def test_container_includes_optional_module(self):
        dockerfile = Path(__file__).with_name("Dockerfile").read_text()
        self.assertIn("jev_filter.py", dockerfile)
