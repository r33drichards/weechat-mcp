# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio
import base64
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from mcp_events import EventError, EventManager, matches

SECRET = "whsec_" + base64.b64encode(b"test-key-only-not-a-credential!!!").decode()
SECOND_SECRET = (
    "whsec_" + base64.b64encode(b"different-test-key-only-32-bytes!").decode()
)
CHANNEL = "irc.test.#one"


class Backend:
    def __init__(self):
        self.snapshots: dict[str, dict[str, Any]] = {
            CHANNEL: {
                "epoch": "epoch1",
                "lines": [],
                "own_nick": "dot",
                "evicted_through": 0,
            }
        }
        self.calls = []

    async def request(self, operation, **params):
        self.calls.append((operation, params))
        if operation != "event_history":
            raise AssertionError("Events must never invoke a write tool")
        channel = unquote(params["uri"].removeprefix("weechat://buffer/"))
        if channel not in self.snapshots:
            raise ValueError("scope revoked")
        return copy.deepcopy(self.snapshots[channel])

    def message(
        self, sequence, message="hello", nick="alice", channel=CHANNEL, **extra
    ):
        self.snapshots[channel]["lines"].append(
            dict(
                sequence=sequence,
                date="1700000000",
                nick=nick,
                message=message,
                is_message=True,
                is_self=False,
                **extra,
            )
        )


class Sender:
    def __init__(self):
        self.requests = []
        self.status = 204
        self.challenge_status = 200
        self.challenge_match = True

    async def post(self, url, body, headers):
        self.requests.append((url, body, headers))
        event = json.loads(body)
        if event.get("type") == "verification":
            return self.challenge_status, json.dumps(
                {"challenge": event["challenge"] if self.challenge_match else "wrong"}
            ).encode()
        return self.status, b""

    @property
    def deliveries(self):
        return [
            request for request in self.requests if "eventId" in json.loads(request[1])
        ]


class EventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "events.db")
        self.backend, self.sender = Backend(), Sender()
        self.now = 1700000000.0
        self.allowed = {"owner"}
        self.manager = self.make_manager()

    def make_manager(self):
        return EventManager(
            self.path,
            self.backend,
            lambda owner: owner in self.allowed,
            sender=self.sender,
            clock=lambda: self.now,
        )

    async def asyncTearDown(self):
        await self.manager.close()
        self.directory.cleanup()

    def params(self, **extra):
        value = {
            "name": "message.created",
            "arguments": {"channel_id": CHANNEL, "trigger": "message"},
            "delivery": {
                "mode": "webhook",
                "url": "https://receiver.example/callback/chat-one",
                "secret": SECRET,
            },
        }
        value.update(extra)
        return value

    async def subscribe(self, **extra):
        return await self.manager.subscribe("owner", self.params(**extra))

    async def test_startup_discovery_and_join_never_subscribe(self):
        self.manager.start()
        await self.manager.list_events("owner")
        self.backend.message(1)
        await self.manager.collect()
        await self.manager.deliver_once()
        self.assertEqual(
            self.manager.db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0],
            0,
        )
        self.assertFalse(self.sender.requests)

    async def test_explicit_subscription_is_future_only_and_deduplicates(self):
        self.backend.message(1, "before opt-in")
        first = await self.subscribe()
        self.assertEqual(len(self.sender.requests), 1)
        self.backend.message(2, "after opt-in")
        await self.manager.collect()
        await self.manager.collect()
        await self.manager.deliver_once()
        await self.manager.deliver_once()
        self.assertEqual(len(self.sender.deliveries), 1)
        event = json.loads(self.sender.deliveries[0][1])
        self.assertEqual(event["data"]["text"], "after opt-in")
        self.assertEqual(event["name"], "message.created")
        self.assertNotIn("type", event)
        self.assertEqual(event["eventId"], self.sender.deliveries[0][2]["webhook-id"])
        self.assertEqual(
            self.sender.deliveries[0][2]["X-MCP-Subscription-Id"], first["id"]
        )

    async def test_channel_trigger_and_chat_isolation(self):
        other = "irc.test.#two"
        self.backend.snapshots[other] = dict(epoch="epoch1", lines=[], own_nick="dot")
        one = await self.subscribe(
            arguments={"channel_id": CHANNEL, "trigger": "mention", "nick": "dot"}
        )
        two_params = self.params(arguments={"channel_id": other, "trigger": "message"})
        two_params["delivery"]["url"] = "https://receiver.example/callback/chat-two"
        two = await self.manager.subscribe("owner", two_params)
        self.assertNotEqual(one["id"], two["id"])
        self.backend.message(1, "not a mention")
        self.backend.message(2, "DOT: hello")
        self.backend.message(3, "another room", channel=other)
        await self.manager.collect()
        await self.manager.deliver_once()
        self.assertEqual(
            [(r[0], json.loads(r[1])["data"]["text"]) for r in self.sender.deliveries],
            [
                ("https://receiver.example/callback/chat-one", "DOT: hello"),
                ("https://receiver.example/callback/chat-two", "another room"),
            ],
        )

    async def test_same_channel_two_chats_have_distinct_subscriptions(self):
        first = await self.subscribe()
        second_params = self.params()
        second_params["delivery"]["url"] += "-two"
        second = await self.manager.subscribe("owner", second_params)
        self.assertNotEqual(first["id"], second["id"])

    async def test_idempotent_canonical_arguments_refresh(self):
        one = await self.subscribe()
        self.now += 30
        two = await self.subscribe(
            arguments={"trigger": "message", "channel_id": CHANNEL}
        )
        self.assertEqual(one["id"], two["id"])
        self.assertNotEqual(one["refreshBefore"], two["refreshBefore"])
        self.assertEqual(len(self.sender.requests), 1)
        self.assertEqual(
            self.manager.db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0],
            1,
        )

    async def test_owner_isolation_and_revocation(self):
        self.allowed.add("other")
        one = await self.subscribe()
        two = await self.manager.subscribe("other", self.params())
        self.assertNotEqual(one["id"], two["id"])
        await self.manager.unsubscribe("other", self.params())
        self.assertEqual(
            self.manager.db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0],
            1,
        )
        self.backend.message(1)
        await self.manager.collect()
        self.allowed.clear()
        await self.manager.deliver_once()
        self.assertFalse(self.sender.deliveries)
        self.assertEqual(
            self.manager.db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0],
            0,
        )

    async def test_scope_rechecked_before_send_and_revokes(self):
        await self.subscribe()
        self.backend.message(1)
        await self.manager.collect()
        del self.backend.snapshots[CHANNEL]
        await self.manager.deliver_once()
        self.assertFalse(self.sender.deliveries)
        self.assertEqual(
            self.manager.db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0],
            0,
        )

    async def test_unsubscribe_is_idempotent_and_cancels_pending(self):
        await self.subscribe()
        self.backend.message(1)
        await self.manager.collect()
        for _ in range(2):
            self.assertEqual(await self.manager.unsubscribe("owner", self.params()), {})
        await self.manager.deliver_once()
        self.assertFalse(self.sender.deliveries)
        self.assertEqual(
            self.manager.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0
        )

    async def test_validation_and_callback_failure_do_not_store(self):
        for params in (
            self.params(name="wrong"),
            self.params(arguments={"channel_id": CHANNEL}),
            self.params(arguments={"channel_id": CHANNEL, "trigger": "mention"}),
            self.params(
                arguments={"channel_id": "irc.test.#secret", "trigger": "message"}
            ),
            self.params(ttlMs=True),
            self.params(ttlMs=-1),
        ):
            with self.assertRaises(EventError):
                await self.manager.subscribe("owner", params)
        self.sender.challenge_match = False
        with self.assertRaises(EventError) as caught:
            await self.subscribe()
        self.assertEqual(caught.exception.code, -32015)
        self.assertEqual(caught.exception.reason, "challenge_failed")
        self.assertEqual(
            self.manager.db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0],
            0,
        )

    async def test_callback_needs_2xx_and_cache_is_owner_scoped(self):
        self.sender.challenge_status = 302
        with self.assertRaises(EventError):
            await self.subscribe()
        self.sender.challenge_status = 200
        await self.subscribe()
        await self.subscribe()
        self.allowed.add("other")
        await self.manager.subscribe("other", self.params())
        self.assertEqual(len(self.sender.requests), 3)

    async def test_secret_not_in_catalog_response_or_error(self):
        result = await self.subscribe()
        catalog = await self.manager.list_events("owner")
        self.assertNotIn(SECRET, json.dumps([result, catalog]))
        invalid = self.params()
        invalid["delivery"]["secret"] = "not-secret-sentinel"
        with self.assertRaises(EventError) as caught:
            await self.manager.subscribe("owner", invalid)
        self.assertNotIn("not-secret-sentinel", str(caught.exception))

    async def test_expiration_honored_and_null_has_finite_grant(self):
        sub = await self.subscribe(ttlMs=500)
        self.assertEqual(sub["refreshBefore"], "2023-11-14T22:13:20.500000Z")
        self.backend.message(1)
        await self.manager.collect()
        self.now += 0.6
        await self.manager.deliver_once()
        self.assertFalse(self.sender.deliveries)
        result = await self.subscribe(ttlMs=None)
        self.assertIsNotNone(result["refreshBefore"])

    async def test_restart_keeps_outbox_identity_and_ack_cursor(self):
        await self.subscribe()
        self.backend.message(1)
        await self.manager.collect()
        queued = self.manager.db.execute("SELECT payload FROM outbox").fetchone()[0]
        await self.manager.close()
        self.manager = self.make_manager()
        await self.manager.collect()
        await self.manager.deliver_once()
        self.assertEqual(self.sender.deliveries[0][1].decode(), queued)
        result = await self.subscribe()
        self.assertEqual(result["cursor"], json.loads(queued)["cursor"])

    async def test_retry_is_ordered_stable_and_freshly_signed(self):
        initial = await self.subscribe()
        self.backend.message(1, "first")
        self.backend.message(2, "second")
        await self.manager.collect()
        self.sender.status = 503
        await self.manager.deliver_once()
        refreshed = await self.subscribe()
        self.assertEqual(refreshed["cursor"], initial["cursor"])
        await self.manager.deliver_once()
        self.assertEqual(len(self.sender.deliveries), 1)
        self.now += 3
        self.sender.status = 204
        await self.manager.deliver_once()
        await self.manager.deliver_once()
        first, retry, second = self.sender.deliveries
        self.assertEqual(first[1], retry[1])
        self.assertNotEqual(
            first[2]["webhook-timestamp"], retry[2]["webhook-timestamp"]
        )
        self.assertNotEqual(
            first[2]["webhook-signature"], retry[2]["webhook-signature"]
        )
        self.assertEqual(json.loads(second[1])["data"]["text"], "second")

    async def test_410_413_and_permanent_error_never_retry_or_skip(self):
        for status in (410, 413, 400):
            with self.subTest(status=status):
                await self.manager.unsubscribe("owner", self.params())
                await self.subscribe()
                high = len(self.backend.snapshots[CHANNEL]["lines"])
                self.backend.message(high + 1)
                self.backend.message(high + 2)
                await self.manager.collect()
                self.sender.status = status
                before = len(self.sender.deliveries)
                await self.manager.deliver_once()
                self.now += 600
                await self.manager.deliver_once()
                self.assertEqual(len(self.sender.deliveries), before + 1)

    async def test_retry_attempts_are_bounded(self):
        await self.subscribe(ttlMs=86400000)
        self.backend.message(1)
        await self.manager.collect()
        self.sender.status = 503
        for _ in range(12):
            await self.manager.deliver_once()
            self.now += 400
        self.assertEqual(len(self.sender.deliveries), 8)

    async def test_secret_rotation_signs_both_then_forgets_old(self):
        await self.subscribe()
        rotated = self.params()
        rotated["delivery"]["secret"] = SECOND_SECRET
        await self.manager.subscribe("owner", rotated)
        self.backend.message(1)
        await self.manager.collect()
        await self.manager.deliver_once()
        self.assertEqual(
            len(self.sender.deliveries[-1][2]["webhook-signature"].split()), 2
        )
        self.now += 301
        self.backend.message(2)
        await self.manager.collect()
        await self.manager.deliver_once()
        self.assertEqual(
            len(self.sender.deliveries[-1][2]["webhook-signature"].split()), 1
        )
        self.assertIsNone(
            self.manager.db.execute("SELECT old_secret FROM subscriptions").fetchone()[
                0
            ]
        )

    async def test_replay_cursor_and_retention_truncation(self):
        initial = await self.subscribe()
        self.backend.message(1)
        self.backend.message(2)
        await self.manager.collect()
        await self.manager.deliver_once()
        first_cursor = json.loads(self.sender.deliveries[0][1])["cursor"]
        await self.manager.deliver_once()
        await self.subscribe(cursor=first_cursor)
        await self.manager.deliver_once()
        self.assertEqual(self.sender.deliveries[1][1], self.sender.deliveries[2][1])
        with self.manager.db:
            self.manager.db.execute("UPDATE outbox SET created=0 WHERE status='sent'")
        await self.manager.deliver_once()
        replay = await self.subscribe(cursor=initial["cursor"])
        self.assertTrue(replay["truncated"])

    async def test_cursor_bound_to_subscription_and_cannot_jump_backlog(self):
        initial = await self.subscribe()
        self.backend.message(1)
        await self.manager.collect()
        for cursor in ("sub_other:1", initial["id"] + ":9999", initial["id"] + ":-1"):
            with self.assertRaises(EventError):
                await self.subscribe(cursor=cursor)

    async def test_history_loss_is_visible_without_unsupported_control_events(self):
        await self.subscribe()
        self.backend.snapshots[CHANNEL]["evicted_through"] = 10
        self.backend.message(11)
        await self.manager.collect()
        result = await self.subscribe()
        self.assertTrue(result["truncated"])
        await self.manager.deliver_once()
        self.assertTrue(
            all("type" not in json.loads(item[1]) for item in self.sender.deliveries)
        )

    async def test_first_sparse_native_id_after_empty_opt_in_is_not_a_gap(self):
        await self.subscribe()
        self.backend.snapshots[CHANNEL]["native_retained_from"] = 1700000000000000
        self.backend.message(1700000000000001)
        await self.manager.collect()
        result = await self.subscribe()
        self.assertFalse(result["truncated"])
        await self.manager.deliver_once()
        self.assertEqual(len(self.sender.deliveries), 1)

    async def test_sparse_arrival_after_empty_cleared_native_baseline_is_not_a_gap(
        self,
    ):
        self.backend.snapshots[CHANNEL]["last_assigned_id"] = 500
        await self.subscribe()
        self.assertEqual(
            self.manager.db.execute("SELECT intake FROM subscriptions").fetchone()[0],
            501,
        )
        self.backend.snapshots[CHANNEL]["native_retained_from"] = 1000000
        self.backend.message(1000001)
        await self.manager.collect()
        result = await self.subscribe()
        self.assertFalse(result["truncated"])

    async def test_native_retention_beyond_prior_intake_is_conservatively_reported(
        self,
    ):
        self.backend.message(1001)
        await self.subscribe()
        self.backend.snapshots[CHANNEL]["lines"] = []
        self.backend.snapshots[CHANNEL]["native_retained_from"] = 2000
        self.backend.message(2001)
        await self.manager.collect()
        result = await self.subscribe()
        self.assertTrue(result["truncated"])

    async def test_producer_epoch_change_flags_truncated_and_continues(self):
        await self.subscribe()
        self.backend.snapshots[CHANNEL]["epoch"] = "epoch2"
        self.backend.message(1)
        await self.manager.collect()
        result = await self.subscribe()
        self.assertTrue(result["truncated"])
        await self.manager.deliver_once()
        self.assertFalse(self.sender.deliveries)
        self.backend.message(2, "after new lifetime baseline")
        await self.manager.collect()
        await self.manager.deliver_once()
        self.assertEqual(len(self.sender.deliveries), 1)
        self.assertEqual(
            json.loads(self.sender.deliveries[0][1])["data"]["text"],
            "after new lifetime baseline",
        )

    async def test_temporary_channel_unavailability_preserves_grant_and_outbox(self):
        await self.subscribe()
        self.backend.message(1)
        await self.manager.collect()
        original = self.backend.request

        async def unavailable(*args, **kwargs):
            raise ConnectionError("not joined yet")

        self.backend.request = unavailable  # ty: ignore[invalid-assignment]  # Offline fault injection.
        await self.manager.close()
        self.manager = self.make_manager()
        await self.manager.collect()
        await self.manager.deliver_once()
        self.assertEqual(
            self.manager.db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.manager.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1
        )
        self.backend.request = original  # ty: ignore[invalid-assignment]
        self.now += 2
        await self.manager.deliver_once()
        self.assertEqual(len(self.sender.deliveries), 1)

    async def test_crash_after_final_attempt_counter_cannot_send_ninth_attempt(self):
        await self.subscribe()
        self.backend.message(1)
        await self.manager.collect()
        with self.manager.db:
            self.manager.db.execute("UPDATE outbox SET attempts=8")
        await self.manager.deliver_once()
        self.assertFalse(self.sender.deliveries)
        with self.assertRaisesRegex(EventError, "Delivery stopped"):
            await self.subscribe()

    async def test_stopped_grant_refresh_returns_actionable_error(self):
        await self.subscribe()
        self.backend.message(1)
        await self.manager.collect()
        self.sender.status = 410
        await self.manager.deliver_once()
        with self.assertRaisesRegex(EventError, "unsubscribe"):
            await self.subscribe()
        self.assertEqual(len(self.sender.deliveries), 1)

    async def test_zero_cursor_partial_prune_is_truncated(self):
        initial = await self.subscribe()
        self.backend.message(1)
        self.backend.message(2)
        await self.manager.collect()
        await self.manager.deliver_once()
        await self.manager.deliver_once()
        with self.manager.db:
            self.manager.db.execute(
                "UPDATE outbox SET created=0 WHERE position=(SELECT MIN(position) FROM outbox)"
            )
        await self.manager.deliver_once()
        replay = await self.subscribe(cursor=initial["cursor"])
        self.assertTrue(replay["truncated"])

    async def test_slow_callback_does_not_block_intake_or_other_chat_unsubscribe(self):
        await self.subscribe()
        other = self.params()
        other["delivery"]["url"] += "-other"
        await self.manager.subscribe("owner", other)
        self.backend.message(1)
        await self.manager.collect()
        started, release = asyncio.Event(), asyncio.Event()
        original = self.sender.post

        async def slow(url, body, headers):
            if url.endswith("chat-one") and "eventId" in json.loads(body):
                started.set()
                await release.wait()
            return await original(url, body, headers)

        self.sender.post = slow  # ty: ignore[invalid-assignment]  # Offline blocked receiver.
        sending = asyncio.create_task(self.manager.deliver_once())
        await started.wait()
        self.backend.message(2)
        await asyncio.wait_for(self.manager.collect(), 0.5)
        await asyncio.wait_for(self.manager.unsubscribe("owner", other), 0.5)
        cancelling = asyncio.create_task(
            self.manager.unsubscribe("owner", self.params())
        )
        await asyncio.sleep(0)
        self.assertFalse(cancelling.done())
        release.set()
        await sending
        await cancelling
        await self.manager.deliver_once()
        self.assertEqual(
            sum(url.endswith("chat-one") for url, _, _ in self.sender.deliveries), 1
        )

    async def test_worker_drains_burst_without_per_event_one_second_sleep(self):
        await self.subscribe()
        for i in range(20):
            self.backend.message(i + 1)
        self.manager.start()

        async def wait_for_delivery():
            while len(self.sender.deliveries) < 20:
                await asyncio.sleep(0.01)

        # Twenty events with a one-second sleep would take ~20 seconds. Allow
        # fsync-heavy SQLite commits on shared CI runners without hiding that
        # regression behind a wall-clock deadline shorter than disk latency.
        await asyncio.wait_for(wait_for_delivery(), 5)

    async def test_worker_cancel_is_not_lost_to_concurrent_wakeup(self):
        self.manager.task = task = asyncio.create_task(self.manager.run())
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.manager.wake.set()
            await asyncio.sleep(0)
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=1)
            self.assertIn(task, done)
            self.assertTrue(task.cancelled())
        finally:
            # Rescue a broken implementation without hanging the test teardown.
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def delayed_history(self):
        reply = asyncio.get_running_loop().create_future()
        started = asyncio.Event()

        async def delayed(operation, **params):
            self.assertEqual(operation, "event_history")
            started.set()
            # Mirror the inherited IPC request completion/cancel race on 3.10.
            return await asyncio.wait_for(reply, 10)

        self.backend.request = delayed  # ty: ignore[invalid-assignment]
        return reply, started

    async def close_on_history_reply(self, reply, snapshot):
        async def reply_and_close():
            # Complete the inner future and cancel its waiting worker without
            # yielding between the two operations.
            reply.set_result(snapshot)
            await self.manager.close()

        closing = asyncio.create_task(reply_and_close())
        try:
            done, _ = await asyncio.wait({closing}, timeout=1)
            self.assertIn(closing, done)
            await closing
        finally:
            # Rescue old implementations whose first cancel was swallowed.
            if not closing.done():
                for task in (self.manager.task, self.manager.intake_task):
                    if task is not None:
                        task.cancel()
                await asyncio.gather(closing, return_exceptions=True)

    async def test_close_exits_intake_when_history_completes_concurrently(self):
        await self.subscribe()
        snapshot = copy.deepcopy(self.backend.snapshots[CHANNEL])
        reply, started = self.delayed_history()
        self.manager.intake_task = asyncio.create_task(self.manager.ingest_loop())
        await started.wait()
        await self.close_on_history_reply(reply, snapshot)
        self.assertTrue(self.manager.closed)

    async def test_close_does_not_send_when_history_completes_concurrently(self):
        await self.subscribe()
        self.backend.message(1)
        await self.manager.collect()
        snapshot = copy.deepcopy(self.backend.snapshots[CHANNEL])
        reply, started = self.delayed_history()
        self.manager.task = asyncio.create_task(self.manager.run())
        await started.wait()
        await self.close_on_history_reply(reply, snapshot)
        self.assertEqual(self.sender.deliveries, [])

    async def test_backend_timeout_does_not_kill_dispatcher(self):
        await self.subscribe()
        self.backend.message(1)
        await self.manager.collect()
        original = self.backend.request

        async def timeout(*args, **kwargs):
            raise asyncio.TimeoutError()

        self.backend.request = timeout  # ty: ignore[invalid-assignment]
        self.manager.start()
        await asyncio.sleep(0.02)
        self.assertFalse(self.manager.task.done())
        self.backend.request = original  # ty: ignore[invalid-assignment]
        self.now += 2
        self.manager.wake.set()

        async def delivered():
            while not self.sender.deliveries:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(delivered(), 1)

    async def test_concurrent_subscriptions_cannot_bypass_quota(self):
        release = asyncio.Event()
        all_started = asyncio.Event()
        started = 0
        original = self.sender.post

        async def barrier(url, body, headers):
            nonlocal started
            started += 1
            if started == 101:
                all_started.set()
            await release.wait()
            return await original(url, body, headers)

        self.sender.post = barrier  # ty: ignore[invalid-assignment]
        tasks = []
        for index in range(101):
            params = self.params()
            params["delivery"]["url"] += "-" + str(index)
            tasks.append(asyncio.create_task(self.manager.subscribe("owner", params)))
        await asyncio.wait_for(all_started.wait(), 2)
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(sum(isinstance(result, EventError) for result in results), 1)
        self.assertEqual(
            self.manager.db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0],
            100,
        )

    async def test_sent_retention_does_not_count_as_pending_queue_limit(self):
        subscribed = await self.subscribe()
        with self.manager.db:
            self.manager.db.executemany(
                "INSERT INTO outbox(subscription,event_key,payload,status,created) VALUES(?,?,?,'sent',?)",
                [
                    (subscribed["id"], str(index), "{}", self.now)
                    for index in range(10000)
                ],
            )
        self.backend.message(1)
        await self.manager.collect()
        self.assertEqual(
            self.manager.db.execute("SELECT suspended FROM subscriptions").fetchone()[
                0
            ],
            0,
        )
        await self.manager.deliver_once()
        self.assertEqual(len(self.sender.deliveries), 1)
        await self.manager.deliver_once()
        self.assertEqual(
            self.manager.db.execute(
                "SELECT COUNT(*) FROM outbox WHERE status='sent'"
            ).fetchone()[0],
            10000,
        )
        self.assertGreater(
            self.manager.db.execute(
                "SELECT pruned_through FROM subscriptions"
            ).fetchone()[0],
            0,
        )

    async def test_private_database_directory_required(self):
        os.chmod(self.directory.name, 0o755)
        try:
            with self.assertRaisesRegex(ValueError, "private owner-only"):
                self.make_manager()
        finally:
            os.chmod(self.directory.name, 0o700)

    async def test_store_is_private_and_single_process(self):
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        with self.assertRaises(BlockingIOError):
            self.make_manager()


class FilterTests(unittest.TestCase):
    def test_only_actual_incoming_messages_match(self):
        args = {"trigger": "message"}
        line = {"nick": "alice", "message": "hi", "is_message": True}
        self.assertTrue(matches(args, line, "dot"))
        for override in (
            {"nick": "DOT"},
            {"is_self": True},
            {"is_message": False},
            {"nick": ""},
        ):
            self.assertFalse(matches(args, dict(line, **override), "dot"))

    def test_mentions_use_irc_casefold_and_word_boundaries(self):
        args = {"trigger": "mention", "nick": "[Dot]"}
        for text in ("hello @{dot}!", "[DOT]: hi"):
            self.assertTrue(
                matches(
                    args, {"nick": "alice", "message": text, "is_message": True}, "own"
                )
            )
        self.assertFalse(
            matches(
                args, {"nick": "alice", "message": "x[dot]y", "is_message": True}, "own"
            )
        )
