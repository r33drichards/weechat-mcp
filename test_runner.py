# SPDX-License-Identifier: GPL-3.0-or-later
import tempfile
import unittest

from runner import HistoryGap, Runner


def snapshot(*lines, epoch="first", evicted=0):
    return {
        "epoch": epoch,
        "omitted_through_id": evicted - 1 if evicted else None,
        "oldest_available_id": min((seq - 1 for seq, _, _ in lines), default=None),
        "lines": [
            {"id": seq - 1, "tags": ["nick_" + nick, "irc_privmsg"], "message": msg}
            for seq, nick, msg in lines
        ],
    }


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + "/agent.sqlite"
        self.runner = Runner(self.path, "agentA", 2)

    def tearDown(self):
        self.runner.close()
        self.tmp.cleanup()

    def test_mentions_ignore_self_and_substrings(self):
        added = self.runner.ingest(
            snapshot(
                (1, "agentA", "agentA: own"),
                (2, "other", "agentAB: no"),
                (3, "other", "@agentA: yes"),
                (4, "", "agentA: status"),
            )
        )
        self.assertEqual(added, 1)

    def test_duplicate_notifications_and_restart(self):
        s = snapshot((1, "other", "agentA: hi"))
        self.assertEqual(self.runner.ingest(s), 1)
        self.assertEqual(self.runner.ingest(s), 0)
        self.runner.close()
        self.runner = Runner(self.path, "agentA", 2)
        self.assertEqual(self.runner.ingest(s), 0)
        turn = self.runner.start_turn()
        self.runner.finish_turn(turn)
        self.assertEqual(self.runner.counts(), {"done": 1})

    def test_queue_while_busy_and_cap(self):
        self.runner.ingest(snapshot((1, "other", "agentA: first")))
        first = self.runner.start_turn()
        self.runner.ingest(
            snapshot((2, "other", "agentA: queued"), (3, "other", "agentA: capped"))
        )
        self.assertIsNone(self.runner.start_turn())
        self.assertEqual(self.runner.counts(), {"running": 1, "pending": 2})
        self.runner.finish_turn(first)
        second = self.runner.start_turn()
        self.assertEqual(second["sequence"], 2)
        self.runner.finish_turn(second)
        self.assertIsNone(self.runner.start_turn())
        self.assertEqual(self.runner.counts(), {"done": 2, "pending": 1})

    def test_reconnect_catches_new_only(self):
        self.runner.ingest(snapshot((1, "other", "agentA: first")))
        self.assertEqual(
            self.runner.ingest(
                snapshot((1, "other", "agentA: first"), (2, "other", "agentA: second"))
            ),
            1,
        )

    def test_epoch_reset(self):
        self.runner.ingest(snapshot((1, "other", "agentA: first")))
        with self.assertRaisesRegex(HistoryGap, "lifetime changed"):
            self.runner.ingest(snapshot((1, "other", "agentA: new"), epoch="second"))

    def test_public_pagination_epoch_change_does_not_requeue_completed_native_line(
        self,
    ):
        first = snapshot((1, "other", "agentA: existing"), epoch="public-before")
        first["data_epoch"] = "stable-native-buffer"
        self.assertEqual(self.runner.ingest(first), 1)
        turn = self.runner.start_turn()
        self.runner.finish_turn(turn)
        reloaded = dict(first, epoch="public-after")
        self.assertEqual(self.runner.ingest(reloaded), 0)
        self.assertEqual(self.runner.counts(), {"done": 1})

    def test_gap_fails_closed(self):
        with self.assertRaises(HistoryGap):
            self.runner.ingest(snapshot((5, "other", "agentA: lost"), evicted=3))
        self.assertEqual(self.runner.counts(), {})

    def test_uncertain_running_turn_not_repeated(self):
        self.runner.ingest(snapshot((1, "other", "agentA: hi")))
        self.runner.start_turn()
        self.runner.close()
        self.runner = Runner(self.path, "agentA")
        self.assertIsNone(self.runner.start_turn())

    def test_identity_mismatch(self):
        with self.assertRaises(ValueError):
            Runner(self.path, "agentB")


if __name__ == "__main__":
    unittest.main()
