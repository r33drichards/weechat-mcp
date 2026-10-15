# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic checks for the cross-process native fixture file protocol."""

import json
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import native_buffer_live_test as live


class NativeFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.control = Path(temporary.name) / "control.json"
        self.response = self.control.with_suffix(".result")

    def assert_atomic_publication(self, destination, expected, publish):
        previous = destination.read_text() if destination.exists() else None
        write_text = Path.write_text
        writes = []

        def interrupted_write(path, content, *args, **kwargs):
            # Pause publication with incomplete JSON on disk, just as another
            # process could observe between open/truncate and close.
            writes.append(path)
            self.assertEqual(path.parent, destination.parent)
            self.assertNotEqual(path, destination)
            write_text(path, content[:1], *args, **kwargs)
            if previous is None:
                self.assertFalse(destination.exists())
            else:
                self.assertEqual(destination.read_text(), previous)
            return write_text(path, content, *args, **kwargs)

        with patch.object(Path, "write_text", interrupted_write):
            publish()
        self.assertEqual(json.loads(destination.read_text()), expected)
        self.assertEqual(len(writes), 1)
        self.assertFalse(writes[0].exists())

    def load_fixture(self):
        weechat = Mock(WEECHAT_RC_OK=0)
        repo = Path(__file__).resolve().parent
        with (
            patch.dict(sys.modules, {"weechat": weechat}),
            patch.dict(
                os.environ,
                NATIVE_TEST_CONTROL=str(self.control),
                NATIVE_TEST_REPO=str(repo),
                NATIVE_TEST_SEED="0",
            ),
        ):
            return runpy.run_path(str(repo / "fixtures/native_buffers.py"))

    def test_control_is_published_atomically(self):
        self.response.write_text(json.dumps({"id": 2, "ok": True}))
        for previous in (None, {"id": 1, "action": "clear"}):
            with self.subTest(previous=previous):
                if previous is not None:
                    self.control.write_text(json.dumps(previous))
                self.assert_atomic_publication(
                    self.control,
                    {"id": 2, "action": "append"},
                    lambda: live.run_fixture(self.control, 2, "append"),
                )

    def test_success_and_error_responses_are_published_atomically(self):
        fixture = self.load_fixture()
        for action, result in (
            ("append", {"id": 2, "ok": True}),
            ("unknown", {"id": 2, "error": "Unknown fixture action"}),
        ):
            for previous in (None, {"id": 1, "ok": True}):
                with self.subTest(action=action, previous=previous):
                    self.control.write_text(json.dumps({"id": 2, "action": action}))
                    if previous is None:
                        self.response.unlink(missing_ok=True)
                    else:
                        self.response.write_text(json.dumps(previous))
                    self.assert_atomic_publication(
                        self.response, result, lambda: fixture["tick"]("", 0)
                    )
                    self.assertFalse(self.control.exists())

    def test_poll_reads_one_snapshot_and_ignores_missing_or_stale_results(self):
        read_text = Path.read_text
        reads = []

        def count_read(path, *args, **kwargs):
            reads.append(path)
            return read_text(path, *args, **kwargs)

        def poll_results(predicate):
            self.assertIsNone(predicate())
            self.response.write_text(json.dumps({"id": 1, "ok": True}))
            self.assertIsNone(predicate())
            self.response.write_text(json.dumps({"id": 2, "ok": True}))
            self.assertEqual(predicate(), {"id": 2, "ok": True})
            self.assertEqual(reads, [self.response] * 3)
            return {"id": 2, "ok": True}

        with (
            patch.object(Path, "read_text", count_read),
            patch.object(live, "wait_for", poll_results),
        ):
            live.run_fixture(self.control, 2, "append")

    def test_fixture_error_is_not_accepted_as_success(self):
        self.response.write_text(json.dumps({"id": 2, "error": "fixture failed"}))
        with self.assertRaisesRegex(AssertionError, "fixture failed"):
            live.run_fixture(self.control, 2, "append")


if __name__ == "__main__":
    unittest.main()
