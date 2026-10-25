# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic, offline tests. No TypeSafe credentials or real chat data required."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from jev_filter import DEFAULT_MODEL, JEV_ENDPOINT, FilterError, JevFilter

CONTEXT = {
    "preference": "Forward release questions",
    "history": [],
    "message": {"text": "When is the release?"},
}


def reply(choice="send"):
    return json.dumps(
        {
            "answers": {
                "forward": {
                    "type": "choice",
                    "choice": choice,
                    "confidence": 1,
                    "probabilities": {"send": 1},
                }
            }
        }
    ).encode()


class JevFilterTests(unittest.IsolatedAsyncioTestCase):
    def test_opt_in_and_config(self):
        self.assertIsNone(JevFilter.from_env({}))
        self.assertIsNone(JevFilter.from_env({"JEV_API_KEY": "test-key"}))
        for flag in ("yes", "unexpected"):
            with self.assertRaisesRegex(FilterError, "invalid_enable_flag"):
                JevFilter.from_env({"JEV_FILTER_ENABLED": flag})
        with self.assertRaisesRegex(FilterError, "invalid_api_key"):
            JevFilter.from_env({"JEV_FILTER_ENABLED": "true"})
        for key in ("", "has space", "key\r\nInjected: true", "é"):
            with self.assertRaises(FilterError):
                JevFilter(key)
        for timeout in (0, 31, True, float("nan")):
            with self.assertRaisesRegex(FilterError, "invalid_timeout"):
                JevFilter("test", timeout=timeout)
        self.assertEqual(
            JevFilter.from_env(
                {"JEV_FILTER_ENABLED": "1", "JEV_API_KEY": "test"}
            ).model,
            DEFAULT_MODEL,
        )

    async def test_contract_send_drop_and_metadata_exclusion(self):
        for decision in ("send", "drop"):
            sender = AsyncMock()
            sender.post.return_value = (200, reply(decision))
            client = JevFilter("synthetic-key", sender=sender)
            self.assertEqual(
                await client.decide({**CONTEXT, "private": "excluded"}), decision
            )
            url, body, headers = sender.post.call_args.args
            self.assertEqual(url, JEV_ENDPOINT)
            self.assertEqual(headers["Authorization"], "Bearer synthetic-key")
            request = json.loads(body)
            self.assertEqual(request["state"], CONTEXT)
            self.assertEqual(request["model"], DEFAULT_MODEL)
            self.assertEqual(request["questions"]["forward"]["type"], "choice")
            self.assertEqual(
                set(request["questions"]["forward"]["criteria"]), {"send", "drop"}
            )
            self.assertNotIn(b"synthetic-key", body)

    async def test_invalid_responses_fail_closed(self):
        bad = [
            b"",
            b"not json",
            b"[]",
            b"null",
            b"{}",
            b"\xff",
            reply("SEND"),
            reply(True),
            reply({}),
            b'{"answers":{"forward":{"type":"noul","noul":1}}}',
            b'{"answers":{},"answers":{}}',
            b'{"x":NaN}',
            b"x" * 17000,
        ]
        for raw in bad:
            with self.subTest(raw=raw[:40]):
                sender = AsyncMock()
                sender.post.return_value = (200, raw)
                with self.assertRaisesRegex(FilterError, "invalid_response"):
                    await JevFilter("test", sender=sender).decide(CONTEXT)

    async def test_failures_sanitized_and_not_retried(self):
        sender = AsyncMock()
        sender.post.side_effect = RuntimeError("private credentials and chat")
        with self.assertRaisesRegex(FilterError, "^provider_unavailable$"):
            await JevFilter("test", sender=sender).decide(CONTEXT)
        self.assertEqual(sender.post.await_count, 1)
        for status in (301, 401, 429, 529):
            sender = AsyncMock()
            sender.post.return_value = (status, b"private provider error")
            with self.assertRaisesRegex(FilterError, "^provider_rejected$"):
                await JevFilter("test", sender=sender).decide(CONTEXT)

    async def test_timeout_and_cancellation(self):
        async def slow(*args):
            await asyncio.sleep(60)

        sender = AsyncMock()
        sender.post.side_effect = slow
        with self.assertRaisesRegex(FilterError, "timeout"):
            await JevFilter("test", sender=sender, timeout=0.01).decide(CONTEXT)
        sender.post.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await JevFilter("test", sender=sender).decide(CONTEXT)

    async def test_bad_and_oversized_context_never_sent(self):
        sender = AsyncMock()
        client = JevFilter("test", sender=sender)
        for context in (
            {},
            {**CONTEXT, "preference": " "},
            {**CONTEXT, "history": "not list"},
            {**CONTEXT, "message": {"n": float("nan")}},
            {**CONTEXT, "preference": "x" * 65536},
        ):
            with self.assertRaises(FilterError):
                await client.decide(context)
        sender.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
