# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic, credential-free coverage recovery and restart regressions."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from mcp_events import EventError, EventManager
from test_mcp_events import CHANNEL, SECRET, Backend, Sender


def line(n):
    return dict(
        sequence=n,
        date="1700000000",
        nick="alice",
        message=f"line {n}",
        is_message=True,
        is_self=False,
    )


class PagedBackend(Backend):
    def __init__(self):
        super().__init__()
        self.pages = {}
        self.fail_page = False

    async def request(self, operation, **params):
        if params.get("cursor"):
            self.calls.append((operation, params))
            if self.fail_page:
                raise ConnectionError("temporarily unavailable")
            return copy.deepcopy(self.pages[params["cursor"]])
        return await super().request(operation, **params)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "events.db")
        self.backend, self.sender = PagedBackend(), Sender()
        self.now = 1700000000.0
        self.manager = self.open()

    def open(self):
        return EventManager(
            self.path,
            self.backend,
            lambda owner: owner == "owner",
            sender=self.sender,
            clock=lambda: self.now,
        )

    async def asyncTearDown(self):
        await self.manager.close()
        self.directory.cleanup()

    def params(self, **extra):
        return {
            "name": "message.created",
            "arguments": {"channel_id": CHANNEL, "trigger": "message"},
            "delivery": {
                "mode": "webhook",
                "url": "https://receiver.example/callback/recovery",
                "secret": SECRET,
            },
            **extra,
        }

    async def subscribe(self, mode="prove", expected=None, **extra):
        recovery = {"mode": mode}
        if expected is not None:
            recovery["expectedIncarnation"] = expected
        return await self.manager.subscribe(
            "owner", self.params(recovery=recovery, **extra)
        )

    def row(self):
        return self.manager.db.execute("SELECT * FROM subscriptions").fetchone()

    def queued(self):
        return [
            json.loads(r[0])["data"]["message_id"]
            for r in self.manager.db.execute(
                "SELECT payload FROM outbox ORDER BY position"
            )
        ]

    def gap(self):
        self.backend.snapshots[CHANNEL].update(
            lines=[line(n) for n in range(501, 601)],
            evicted_through=500,
            next_cursor="older",
        )
        self.backend.pages["older"] = {
            "epoch": "epoch1",
            "lines": [line(n) for n in range(1, 501)],
            "own_nick": "dot",
            "evicted_through": 0,
            "next_cursor": None,
        }

    async def anchored(self, mode="prove"):
        self.backend.message(1)
        result = await self.subscribe(mode)
        self.backend.message(2)
        await self.manager.collect()
        return result

    async def test_provable_window_gap_replays_in_order_once_and_survives_restart(self):
        original = await self.anchored()
        self.gap()
        await self.manager.collect()
        self.assertEqual(self.queued(), [f"epoch1:{n}" for n in range(2, 601)])
        result = await self.subscribe(
            cursor=original["cursor"], expected=original["coverage"]["incarnation"]
        )
        self.assertFalse(result["truncated"])
        self.assertEqual(result["coverage"]["proof"], "retained_native_anchor")
        self.assertEqual(result["coverage"]["recoveredGeneration"], 1)
        self.assertEqual(result["coverage"]["continuity"], "retained-subscription")
        await self.manager.close()
        self.manager = self.open()
        again = await self.subscribe()
        self.assertEqual(
            again["coverage"]["incarnation"], original["coverage"]["incarnation"]
        )
        self.assertEqual(len(self.queued()), 599)
        self.assertEqual(
            self.manager.db.execute("SELECT state FROM coverage_audit").fetchone()[0],
            "recovered",
        )

    async def test_unproven_gap_holds_queue_and_rate_limits_attempts(self):
        await self.anchored()
        self.gap()
        self.backend.fail_page = True
        await self.manager.collect()
        self.assertEqual(self.row()["intake"], 2)
        self.assertEqual(self.queued(), ["epoch1:2"])
        await self.manager.deliver_once()
        self.assertEqual(self.sender.deliveries, [])
        calls = len(self.backend.calls)
        attempts = self.row()["recovery_attempts"]
        for _ in range(5):
            await self.subscribe()
        self.assertEqual(self.row()["recovery_attempts"], attempts)
        self.assertEqual(
            sum(bool(p.get("cursor")) for _, p in self.backend.calls[calls:]), 0
        )
        self.now += 31
        self.backend.fail_page = False
        await self.manager.collect()
        self.assertEqual(self.row()["coverage_state"], "continuous")
        self.assertEqual(len(self.queued()), 599)

    async def test_resume_records_loss_keeps_truncated_and_detects_next_gap(self):
        await self.anchored("resume")
        self.backend.snapshots[CHANNEL].update(
            epoch="epoch2", lines=[line(40)], evicted_through=0
        )
        await self.manager.collect()
        result = await self.subscribe("resume")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["coverage"]["status"], "degraded")
        self.assertEqual(result["coverage"]["acknowledgedGeneration"], 1)
        self.assertEqual(
            self.queued(),
            ["epoch1:2"],
            "never replay a new epoch's possible pre-opt-in history",
        )
        self.backend.message(41)
        await self.manager.collect()
        self.assertEqual(self.queued(), ["epoch1:2", "epoch2:41"])
        self.backend.snapshots[CHANNEL].update(epoch="epoch3", lines=[line(80)])
        await self.manager.collect()
        result = await self.subscribe("resume")
        self.assertEqual(result["coverage"]["acknowledgedGeneration"], 2)
        audits = self.manager.db.execute(
            "SELECT state,boundary FROM coverage_audit ORDER BY generation"
        ).fetchall()
        self.assertEqual(len(audits), 2)
        self.assertTrue(all(r["state"] == "acknowledged-loss" for r in audits))
        self.assertIn("Historical messages may be unavailable", audits[1]["boundary"])

    async def test_legacy_sticky_gap_is_never_fabricated_complete(self):
        original = await self.anchored()
        with self.manager.db:
            self.manager.db.execute(
                "UPDATE subscriptions SET truncated=1,coverage_generation=0,incarnation=NULL"
            )
        await self.manager.close()
        self.manager = self.open()
        held = await self.subscribe()
        self.assertEqual(held["coverage"]["reason"], "legacy_unknown")
        self.assertEqual(held["coverage"]["status"], "gap")
        self.assertTrue(held["truncated"])
        self.now += 31
        resumed = await self.subscribe("resume")
        self.assertTrue(resumed["truncated"])
        self.assertEqual(resumed["coverage"]["status"], "degraded")
        self.assertEqual(self.queued(), ["epoch1:2"])
        self.assertNotEqual(
            resumed["coverage"]["incarnation"], original["coverage"]["incarnation"]
        )

    async def test_same_id_recreation_changes_incarnation_and_never_proves_continuity(
        self,
    ):
        first = await self.subscribe()
        await self.manager.unsubscribe("owner", self.params())
        second = await self.subscribe(cursor=first["cursor"])
        self.assertEqual(second["id"], first["id"])
        self.assertNotEqual(
            second["coverage"]["incarnation"], first["coverage"]["incarnation"]
        )
        self.assertNotIn("continuity", second["coverage"])
        with self.assertRaisesRegex(EventError, "incarnation changed"):
            await self.manager.subscribe(
                "owner",
                self.params(
                    recovery={
                        "mode": "prove",
                        "expectedIncarnation": first["coverage"]["incarnation"],
                    }
                ),
            )

    async def test_poison_outbox_never_gets_reset_or_replayed_by_recovery(self):
        await self.anchored()
        with self.manager.db:
            self.manager.db.execute("UPDATE outbox SET status='failed',attempts=8")
            self.manager.db.execute("UPDATE subscriptions SET suspended=1")
        self.gap()
        await self.manager.collect()
        row = self.manager.db.execute(
            "SELECT * FROM outbox ORDER BY position LIMIT 1"
        ).fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("failed", 8))
        self.assertEqual(self.row()["suspended"], 1)
        await self.manager.deliver_once()
        self.assertEqual(self.sender.deliveries, [])

    async def test_missing_anchor_and_page_epoch_change_do_not_clear_gap(self):
        await self.anchored()
        self.gap()
        self.backend.pages["older"]["epoch"] = "other"
        await self.manager.collect()
        self.assertEqual(self.row()["coverage_state"], "recovering")
        self.assertEqual(self.queued(), ["epoch1:2"])
        self.now += 31
        self.backend.pages["older"]["epoch"] = "epoch1"
        self.backend.pages["older"]["lines"] = [line(n) for n in range(3, 501)]
        await self.manager.collect()
        self.assertTrue(self.row()["truncated"])
        self.assertEqual(self.row()["intake"], 2)

    async def test_opted_in_expired_queue_is_retained_and_renewed_without_recreation(
        self,
    ):
        first = await self.anchored()
        self.now += 172801
        await self.manager.deliver_once()
        self.assertEqual(self.queued(), ["epoch1:2"])
        renewed = await self.subscribe(
            cursor=first["cursor"], expected=first["coverage"]["incarnation"]
        )
        self.assertEqual(
            renewed["coverage"]["incarnation"], first["coverage"]["incarnation"]
        )
        self.assertEqual(renewed["coverage"]["continuity"], "retained-subscription")

    async def test_recovery_policy_is_strictly_validated(self):
        for value in [
            {"mode": "force"},
            {"mode": "prove", "force": True},
            {"mode": "resume", "expectedIncarnation": "bad"},
            True,
        ]:
            with self.assertRaises(EventError):
                await self.manager.subscribe("owner", self.params(recovery=value))

    async def test_gap_commit_is_atomic_if_outbox_write_fails(self):
        await self.anchored()
        self.gap()
        with self.manager.db:
            self.manager.db.execute(
                "CREATE TRIGGER fail_recovery BEFORE INSERT ON outbox WHEN NEW.event_key != '' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
            )
        with self.assertRaisesRegex(Exception, "synthetic failure"):
            await self.manager.collect()
        self.assertEqual(self.row()["coverage_state"], "recovering")
        self.assertEqual(self.row()["intake"], 2)
        self.assertEqual(self.queued(), ["epoch1:2"])
        self.assertEqual(
            self.manager.db.execute("SELECT state FROM coverage_audit").fetchone()[0],
            "recovering",
        )

    async def test_bounded_page_budget_is_not_treated_as_irretrievable_loss(self):
        await self.anchored("resume")
        self.gap()
        self.backend.pages["older"]["next_cursor"] = "older"
        self.backend.pages["older"]["lines"] = [line(n) for n in range(3, 501)]
        await self.manager.collect()
        self.assertEqual(self.row()["coverage_state"], "recovering")
        self.assertEqual(self.queued(), ["epoch1:2"])
        self.assertIsNone(
            self.manager.db.execute("SELECT resolved FROM coverage_audit").fetchone()[0]
        )

    async def test_proof_does_not_erase_an_earlier_acknowledged_loss(self):
        await self.anchored("resume")
        self.backend.snapshots[CHANNEL].update(
            epoch="epoch2", lines=[line(1)], evicted_through=0
        )
        await self.manager.collect()
        self.backend.message(2)
        await self.manager.collect()
        self.gap()
        self.backend.snapshots[CHANNEL]["epoch"] = "epoch2"
        self.backend.pages["older"]["epoch"] = "epoch2"
        await self.manager.collect()
        result = await self.subscribe("resume")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["coverage"]["status"], "degraded")
        self.assertEqual(result["coverage"]["lossGeneration"], 1)
        self.assertEqual(result["coverage"]["recoveredGeneration"], 2)
        self.assertNotIn("acknowledgedGeneration", result["coverage"])
        self.assertEqual(
            self.manager.db.execute(
                "SELECT state FROM coverage_audit ORDER BY generation DESC LIMIT 1"
            ).fetchone()[0],
            "recovered",
        )

    async def test_opted_in_transient_exhaustion_recovers_same_head_after_restart(self):
        await self.subscribe(ttlMs=86400000)
        self.backend.message(1)
        self.backend.message(2)
        await self.manager.collect()
        for status in [429, 503] * 5:
            self.sender.status = status
            await self.manager.deliver_once()
            self.now += 901
        self.assertEqual(len(self.sender.deliveries), 10)
        self.assertEqual(self.row()["suspended"], 0)
        events = [json.loads(x[1])["eventId"] for x in self.sender.deliveries]
        self.assertEqual(len(set(events)), 1)
        # Reserve the next eligible send and simulate loss of its response.
        pending = self.manager.db.execute(
            "SELECT * FROM outbox ORDER BY position LIMIT 1"
        ).fetchone()
        with self.manager.db:
            self.manager.db.execute(
                "UPDATE outbox SET attempts=11,due=?,failure_class='inflight' WHERE position=?",
                (self.now + 900, pending["position"]),
            )
        await self.manager.close()
        self.manager = self.open()
        self.sender.status = 204
        await self.manager.deliver_once()
        self.assertEqual(len(self.sender.deliveries), 10)
        self.now += 901
        await self.manager.deliver_once()
        self.assertEqual(
            json.loads(self.sender.deliveries[-1][1])["eventId"], events[0]
        )
        await self.manager.deliver_once()
        self.assertNotEqual(
            json.loads(self.sender.deliveries[-1][1])["eventId"], events[0]
        )

    async def test_auto_mode_never_retries_permanent_poison_or_clears_unknown_exhaustion(
        self,
    ):
        for status in (400, 401, 403, 410, 413):
            await self.manager.unsubscribe("owner", self.params())
            self.backend.snapshots[CHANNEL]["lines"] = []
            await self.subscribe()
            self.backend.message(1)
            await self.manager.collect()
            self.sender.status = status
            before = len(self.sender.deliveries)
            await self.manager.deliver_once()
            self.now += 1000
            await self.manager.deliver_once()
            self.assertEqual(len(self.sender.deliveries), before + 1)
            self.assertEqual(self.row()["suspended"], 1)
            with self.assertRaisesRegex(EventError, "Delivery stopped"):
                await self.subscribe()

    async def test_saved_cursor_replay_does_not_reset_transient_pending_cooldown(self):
        first = await self.subscribe()
        self.backend.message(1)
        self.backend.message(2)
        await self.manager.collect()
        await self.manager.deliver_once()
        self.sender.status = 429
        await self.manager.deliver_once()
        with self.manager.db:
            self.manager.db.execute(
                "UPDATE outbox SET attempts=10,due=?,failure_class='inflight' WHERE status='pending'",
                (self.now + 900,),
            )
        before = self.manager.db.execute(
            "SELECT attempts,due,failure_class FROM outbox ORDER BY position DESC LIMIT 1"
        ).fetchone()
        await self.subscribe(
            cursor=first["cursor"], expected=first["coverage"]["incarnation"]
        )
        after = self.manager.db.execute(
            "SELECT attempts,due,failure_class FROM outbox ORDER BY position DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(tuple(before), tuple(after))

    async def test_scheduler_retries_held_gap_without_new_native_messages(self):
        await self.anchored()
        self.gap()
        self.backend.fail_page = True
        await self.manager.collect()
        attempts = self.row()["recovery_attempts"]
        await self.manager.deliver_once()
        self.assertEqual(self.row()["recovery_attempts"], attempts)
        self.now += 31
        self.backend.fail_page = False
        await self.manager.deliver_once()
        self.assertEqual(self.row()["coverage_state"], "continuous")
        self.assertEqual(len(self.sender.deliveries), 1)

    async def test_normal_renewal_opts_out_without_deleting_queue_or_gap_audit(self):
        original = await self.anchored("resume")
        self.backend.snapshots[CHANNEL].update(epoch="epoch2", lines=[line(1)])
        await self.manager.collect()
        self.assertEqual(self.row()["coverage_state"], "degraded")
        await self.manager.subscribe("owner", self.params())
        self.assertEqual(self.row()["recovery_mode"], "off")
        self.backend.snapshots[CHANNEL].update(epoch="epoch3", lines=[line(10)])
        await self.manager.collect()
        result = await self.manager.subscribe("owner", self.params())
        self.assertTrue(result["truncated"])
        self.assertEqual(result["coverage"]["status"], "gap")
        self.assertEqual(
            result["coverage"]["incarnation"], original["coverage"]["incarnation"]
        )
        self.assertEqual(self.queued(), ["epoch1:2"])
        states = [
            r[0]
            for r in self.manager.db.execute(
                "SELECT state FROM coverage_audit ORDER BY generation"
            )
        ]
        self.assertEqual(states, ["acknowledged-loss", "gap"])

    async def test_optout_retains_previously_opted_queue_after_expiry(self):
        first = await self.anchored("resume")
        await self.manager.subscribe("owner", self.params())
        self.now += 172801
        await self.manager.deliver_once()
        self.assertEqual(self.queued(), ["epoch1:2"])
        self.assertEqual(self.row()["recovery_mode"], "off")
        self.assertEqual(self.row()["recovery_enrolled"], 1)
        self.assertEqual(self.row()["incarnation"], first["coverage"]["incarnation"])

    async def test_recovery_cursor_only_requeues_sent_payloads_and_leaves_poison_untouched(
        self,
    ):
        first = await self.subscribe()
        for n in range(1, 4):
            self.backend.message(n)
        await self.manager.collect()
        await self.manager.deliver_once()
        rows = self.manager.db.execute(
            "SELECT position,payload FROM outbox ORDER BY position"
        ).fetchall()
        with self.manager.db:
            self.manager.db.execute(
                "UPDATE outbox SET attempts=10,due=?,failure_class='inflight' WHERE position=?",
                (self.now + 900, rows[1]["position"]),
            )
            self.manager.db.execute(
                "UPDATE outbox SET status='failed',attempts=8,failure_class='permanent' WHERE position=?",
                (rows[2]["position"],),
            )
        before = [
            tuple(r)
            for r in self.manager.db.execute(
                "SELECT status,attempts,due,failure_class,payload FROM outbox WHERE position>? ORDER BY position",
                (rows[0]["position"],),
            )
        ]
        await self.subscribe(
            cursor=first["cursor"], expected=first["coverage"]["incarnation"]
        )
        after = [
            tuple(r)
            for r in self.manager.db.execute(
                "SELECT status,attempts,due,failure_class,payload FROM outbox WHERE position>? ORDER BY position",
                (rows[0]["position"],),
            )
        ]
        self.assertEqual(before, after)
        sent = self.manager.db.execute(
            "SELECT status,payload FROM outbox WHERE position=?", (rows[0]["position"],)
        ).fetchone()
        self.assertEqual(sent["status"], "pending")
        self.assertEqual(sent["payload"], rows[0]["payload"])

    async def test_legacy_zero_cursor_cannot_claim_incarnation_continuity(self):
        first = await self.subscribe()
        result = await self.subscribe(cursor=first["cursor"])
        self.assertNotIn("continuity", result["coverage"])
        self.assertEqual(result["coverage"]["status"], "gap")
        self.now += 31
        result = await self.subscribe("resume", cursor=first["cursor"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["coverage"]["status"], "degraded")
        self.assertNotIn("continuity", result["coverage"])

    async def test_exact_retained_nonzero_receipt_proves_legacy_enrollment(self):
        await self.subscribe()
        self.backend.message(1)
        await self.manager.collect()
        await self.manager.deliver_once()
        cursor = json.loads(self.sender.deliveries[-1][1])["cursor"]
        result = await self.subscribe(cursor=cursor)
        self.assertEqual(result["coverage"]["continuity"], "retained-subscription")
        self.assertFalse(result["truncated"])

    async def test_recreated_same_id_ack_beyond_old_cursor_cannot_prove_old_row(self):
        first = await self.subscribe()
        self.backend.message(1)
        await self.manager.collect()
        await self.manager.deliver_once()
        cursor = json.loads(self.sender.deliveries[-1][1])["cursor"]
        await self.manager.unsubscribe("owner", self.params())
        second = await self.subscribe()
        self.backend.message(2)
        await self.manager.collect()
        await self.manager.deliver_once()
        self.assertGreater(self.row()["acknowledged"], int(cursor.rsplit(":", 1)[1]))
        result = await self.subscribe(cursor=cursor)
        self.assertEqual(result["id"], first["id"])
        self.assertEqual(
            result["coverage"]["incarnation"], second["coverage"]["incarnation"]
        )
        self.assertNotIn("continuity", result["coverage"])
        self.assertEqual(result["coverage"]["status"], "gap")
        self.now += 31
        resumed = await self.subscribe("resume", cursor=cursor)
        self.assertEqual(resumed["coverage"]["proof"], "acknowledged_loss")
        self.assertTrue(resumed["truncated"])
        self.assertNotIn("continuity", resumed["coverage"])

    async def test_pruned_enrollment_anchor_is_audited_loss_not_continuity(self):
        await self.subscribe()
        self.backend.message(1)
        await self.manager.collect()
        await self.manager.deliver_once()
        cursor = json.loads(self.sender.deliveries[-1][1])["cursor"]
        self.now += 86401
        await self.manager.deliver_once()
        result = await self.subscribe("resume", cursor=cursor)
        self.assertNotIn("continuity", result["coverage"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["coverage"]["proof"], "acknowledged_loss")
        self.assertEqual(result["coverage"]["reason"], "replay_pruned")
