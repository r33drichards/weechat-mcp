# SPDX-License-Identifier: GPL-3.0-or-later
"""Real in-memory MCP wire tests; no network, real grants, or subscriptions."""

import asyncio
import copy
import hashlib
import io
import json
import logging
import unittest
from contextvars import ContextVar
from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.server.middleware import Middleware
from mcp import types
from mcp.shared.exceptions import MCPError
from pydantic import SecretStr

from event_protocol import EventResult, SubscribeEventParams, install_event_protocol
from mcp_events import EventError

SECRET = "whsec_" + "eHl6" * 12
PARAMS: dict[str, Any] = {
    "name": "message.created",
    "arguments": {"channel_id": "irc.test.#chat", "trigger": "message"},
    "delivery": {
        "mode": "webhook",
        "url": "https://receiver.example/events/chat-one",
        "secret": SECRET,
    },
}


class FakeManager:
    def __init__(self):
        self.calls = []
        self.subscriptions = {}
        self.error = None

    def record(self, method, owner, params):
        self.calls.append((method, owner, copy.deepcopy(params)))
        if self.error is not None:
            raise self.error

    @staticmethod
    def identity(owner, params):
        value = [owner, params["delivery"]["url"], params["name"], params["arguments"]]
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    async def list_events(self, owner, cursor=None):
        self.record("list", owner, cursor)
        return {
            "events": [
                {
                    "name": "message.created",
                    "delivery": ["webhook"],
                    "inputSchema": {"type": "object"},
                    "payloadSchema": {"type": "object"},
                }
            ],
            "nextCursor": "next-page" if cursor is None else None,
        }

    async def subscribe(self, owner, params):
        self.record("subscribe", owner, params)
        identifier = self.identity(owner, params)
        self.subscriptions[identifier] = copy.deepcopy(params)
        return {
            "id": identifier,
            "refreshBefore": None
            if params.get("ttlMs", 1) is None
            else "2026-10-15T00:00:00Z",
            "cursor": None,
            "truncated": False,
            "extensionField": {"kept": True},
        }

    async def unsubscribe(self, owner, params):
        self.record("unsubscribe", owner, params)
        self.subscriptions.pop(self.identity(owner, params), None)
        return {}


async def request(client, method, params=None):
    # The unparameterized SDK Request silently coerces params to RequestParams
    # and drops custom fields. Explicit dict params exercise the real wire.
    result = await client.session.send_request(
        types.Request[dict[str, Any] | None, str](method=method, params=params),
        EventResult,
    )
    return result.model_dump(by_alias=True, exclude_unset=True)


class EventProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = FakeManager()
        self.principal: Any = "owner-one"
        self.server = FastMCP(
            "event-wire-test", experimental_capabilities={"existing": {}}
        )
        self.server.tool(lambda value: value, name="echo")
        install_event_protocol(self.server, self.manager, lambda: self.principal)

    async def test_explicit_recovery_option_survives_wire_and_rejects_force_policy(
        self,
    ):
        recovery = {"mode": "resume", "expectedIncarnation": "a" * 32}
        async with Client(self.server) as client:
            await request(client, "events/subscribe", {**PARAMS, "recovery": recovery})
            self.assertEqual(self.manager.calls[-1][2]["recovery"], recovery)
            for invalid in [
                {"mode": "force"},
                {"mode": "prove", "clear": True},
                {"mode": "prove", "expectedIncarnation": "invalid"},
            ]:
                with self.assertRaises(MCPError):
                    await request(
                        client, "events/subscribe", {**PARAMS, "recovery": invalid}
                    )
        self.assertEqual(len(self.manager.calls), 1)

    async def test_discovery_preserves_existing_capabilities_and_tools(self):
        async with Client(self.server) as client:
            result = await client.session.send_discover("2026-07-28")
            self.assertEqual(result["capabilities"]["events"], {})
            for name in ("tools", "resources", "prompts", "extensions", "experimental"):
                self.assertIn(name, result["capabilities"])
            self.assertIn("existing", result["capabilities"]["experimental"])
            self.assertIn("2026-07-28", result["supportedVersions"])
            self.assertIn("cacheScope", result)
            self.assertIn("ttlMs", result)
            self.assertEqual(result["resultType"], "complete")
            tools = await client.list_tools()
            self.assertEqual([tool.name for tool in tools], ["echo"])
        self.assertEqual(self.manager.calls, [])

    async def test_list_catalog_cursor_and_owner(self):
        async with Client(self.server) as client:
            result = await request(client, "events/list")
            self.assertEqual(result["events"][0]["name"], "message.created")
            self.assertEqual(result["events"][0]["payloadSchema"], {"type": "object"})
            self.assertEqual(result["nextCursor"], "next-page")
            result = await request(client, "events/list", {"cursor": "next-page"})
            self.assertIsNone(result["nextCursor"])
        self.assertEqual(
            self.manager.calls,
            [("list", "owner-one", None), ("list", "owner-one", "next-page")],
        )

    async def test_subscribe_preserves_null_vs_omitted_ttl_and_payload(self):
        async with Client(self.server) as client:
            result = await request(client, "events/subscribe", PARAMS)
            self.assertNotIn("ttlMs", self.manager.calls[-1][2])
            self.assertNotIn("cursor", self.manager.calls[-1][2])
            self.assertNotIn("_meta", self.manager.calls[-1][2])
            self.assertEqual(self.manager.calls[-1][2]["delivery"]["secret"], SECRET)
            self.assertIsNone(result["cursor"])
            self.assertEqual(result["extensionField"], {"kept": True})
            result = await request(
                client, "events/subscribe", {**PARAMS, "ttlMs": None, "cursor": None}
            )
            self.assertIsNone(self.manager.calls[-1][2]["ttlMs"])
            self.assertIsNone(self.manager.calls[-1][2]["cursor"])
            self.assertIsNone(result["refreshBefore"])
            await request(client, "events/subscribe", {**PARAMS, "ttlMs": 50})
            self.assertEqual(self.manager.calls[-1][2]["ttlMs"], 50)

    async def test_dedup_receives_canonical_arguments_and_owner_callback_context(self):
        async with Client(self.server) as client:
            one = await request(client, "events/subscribe", PARAMS)
            reordered = {
                **PARAMS,
                "arguments": dict(reversed(list(PARAMS["arguments"].items()))),
            }
            same = await request(client, "events/subscribe", reordered)
            self.assertEqual(one["id"], same["id"])
            self.principal = "owner-two"
            other_owner = await request(client, "events/subscribe", PARAMS)
            self.assertNotEqual(one["id"], other_owner["id"])
            other_callback = await request(
                client,
                "events/subscribe",
                {
                    **PARAMS,
                    "delivery": {
                        **PARAMS["delivery"],
                        "url": "https://receiver.example/events/chat-two",
                    },
                },
            )
            self.assertNotEqual(other_owner["id"], other_callback["id"])
        self.assertEqual(len(self.manager.subscriptions), 3)

    async def test_unsubscribe_is_owner_scoped_and_idempotent(self):
        params = copy.deepcopy(PARAMS)
        params["delivery"].pop("secret")
        async with Client(self.server) as client:
            original = await request(client, "events/subscribe", PARAMS)
            self.principal = "owner-two"
            await request(client, "events/unsubscribe", params)
            self.assertIn(original["id"], self.manager.subscriptions)
            self.principal = "owner-one"
            for _ in range(2):
                result = await request(client, "events/unsubscribe", params)
                # The modern SDK adds standard resultType and serverInfo.
                self.assertEqual(set(result), {"_meta", "resultType"})
            self.assertEqual(self.manager.subscriptions, {})

    async def test_unauthenticated_methods_never_call_manager(self):
        unsub = copy.deepcopy(PARAMS)
        unsub["delivery"].pop("secret")
        async with Client(self.server) as client:
            for principal in (None, "", 1):
                self.principal = principal
                for method, params in (
                    ("events/list", {}),
                    ("events/subscribe", PARAMS),
                    ("events/unsubscribe", unsub),
                ):
                    with self.subTest(method=method, principal=principal):
                        with self.assertRaises(MCPError) as raised:
                            await request(client, method, params)
                        self.assertEqual(raised.exception.code, -32000)
                        self.assertEqual(
                            str(raised.exception), "Authentication required"
                        )
        self.assertEqual(self.manager.calls, [])

    async def test_principal_exceptions_are_sanitized(self):
        def denied():
            raise ValueError(SECRET)

        install_event_protocol(self.server, self.manager, denied)
        async with Client(self.server) as client:
            with self.assertRaises(MCPError) as raised:
                await request(client, "events/list", {})
            self.assertNotIn(SECRET, str(raised.exception))
            self.assertEqual(raised.exception.code, -32000)
        self.assertEqual(self.manager.calls, [])

    async def test_strict_subscribe_rejects_malformed_params_without_secrets(self):
        malformed = [
            None,
            {},
            {"delivery": PARAMS["delivery"]},
            {**PARAMS, "name": 1},
            {**PARAMS, "arguments": []},
            {**PARAMS, "owner": "forged-owner"},
            {**PARAMS, "cursor": 0},
            *[{**PARAMS, "ttlMs": value} for value in (True, False, 0, -1, 1.5, "100")],
            {**PARAMS, "ttl_ms": 100},
            *[
                {**PARAMS, "delivery": value}
                for value in (
                    None,
                    [],
                    {"mode": "webhook", "url": "https://receiver.example"},
                    {**PARAMS["delivery"], "mode": "poll"},
                    {**PARAMS["delivery"], "url": 42},
                    {**PARAMS["delivery"], "secret": 42},
                    {**PARAMS["delivery"], "extra": "value"},
                )
            ],
        ]
        logs = io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger().addHandler(handler)
        try:
            async with Client(self.server) as client:
                for params in malformed:
                    with self.subTest(params=params):
                        with self.assertRaises(MCPError) as raised:
                            await request(client, "events/subscribe", params)
                        self.assertEqual(raised.exception.code, -32602)
                        self.assertNotIn(SECRET, str(raised.exception.error))
            self.assertNotIn(SECRET, logs.getvalue())
        finally:
            logging.getLogger().removeHandler(handler)
        self.assertEqual(self.manager.calls, [])

    async def test_strict_list_and_unsubscribe_reject_extra_fields(self):
        unsub = copy.deepcopy(PARAMS)
        unsub["delivery"].pop("secret")
        async with Client(self.server) as client:
            for method, params in (
                ("events/list", {"cursor": 1}),
                ("events/list", {"owner": "forged"}),
                ("events/unsubscribe", PARAMS),
                ("events/unsubscribe", {**unsub, "ttlMs": 100}),
                ("events/unsubscribe", {**unsub, "cursor": None}),
            ):
                with self.subTest(method=method, params=params):
                    with self.assertRaises(MCPError) as raised:
                        await request(client, method, params)
                    self.assertEqual(raised.exception.code, -32602)
        self.assertEqual(self.manager.calls, [])

    async def test_event_errors_preserve_code_message_reason(self):
        async with Client(self.server) as client:
            for reason in ("timeout", None):
                self.manager.error = EventError(
                    -32015, "Callback verification failed", reason
                )
                with self.assertRaises(MCPError) as raised:
                    await request(client, "events/subscribe", PARAMS)
                error = raised.exception.error
                self.assertEqual(error.code, -32015)
                self.assertEqual(error.message, "Callback verification failed")
                self.assertEqual(error.data, {"reason": reason} if reason else None)

    async def test_unexpected_manager_error_never_exposes_exception(self):
        self.manager.error = RuntimeError("Credential: " + SECRET)
        logs = io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger().addHandler(handler)
        try:
            async with Client(self.server) as client:
                with self.assertRaises(MCPError) as raised:
                    await request(client, "events/subscribe", PARAMS)
                self.assertEqual(raised.exception.code, -32603)
                self.assertEqual(str(raised.exception), "Event request failed")
                self.assertNotIn(SECRET, str(raised.exception.error))
            self.assertNotIn(SECRET, logs.getvalue())
        finally:
            logging.getLogger().removeHandler(handler)

    async def test_fastmcp_middleware_only_observes_masked_secret(self):
        observed = []

        class Observer(Middleware):
            async def on_request(self, context, call_next):
                if context.method == "events/subscribe":
                    observed.append(context.message)
                return await call_next(context)

        self.server.add_middleware(Observer())
        async with Client(self.server) as client:
            await request(client, "events/subscribe", PARAMS)
        self.assertEqual(len(observed), 1)
        self.assertNotIn(SECRET, repr(observed))
        self.assertIsInstance(observed[0]["delivery"]["secret"], SecretStr)
        self.assertEqual(self.manager.calls[-1][2]["delivery"]["secret"], SECRET)

    async def test_installation_is_idempotent(self):
        count = len(self.server._mcp_server.middleware)
        install_event_protocol(self.server, self.manager, lambda: self.principal)
        self.assertEqual(len(self.server._mcp_server.middleware), count)
        async with Client(self.server) as client:
            await request(client, "events/subscribe", PARAMS)
        self.assertEqual(len(self.manager.calls), 1)

    async def test_simultaneous_connections_keep_authenticated_context(self):
        principal = ContextVar("event-test-owner")
        install_event_protocol(self.server, self.manager, principal.get)

        async def connect_as(owner):
            token = principal.set(owner)
            try:
                async with Client(self.server) as client:
                    return await request(client, "events/subscribe", PARAMS)
            finally:
                principal.reset(token)

        first, second = await asyncio.gather(connect_as("alice"), connect_as("bob"))
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual({call[1] for call in self.manager.calls}, {"alice", "bob"})


class RequestModelTests(unittest.TestCase):
    def test_model_repr_masks_secret_and_validation_message_hides_input(self):
        params = SubscribeEventParams.model_validate(PARAMS)
        self.assertNotIn(SECRET, repr(params))
        self.assertIsInstance(params.delivery.secret, SecretStr)
        with self.assertRaises(MCPError) as raised:
            SubscribeEventParams.model_validate({"delivery": PARAMS["delivery"]})
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)

    def test_result_dump_retains_explicit_null_extension_fields(self):
        result = EventResult.model_validate(
            {"cursor": None, "refreshBefore": None, "data": {"optional": None}}
        )
        self.assertEqual(
            result.model_dump(by_alias=True, exclude_none=True),
            {
                "cursor": None,
                "refreshBefore": None,
                "data": {"optional": None},
            },
        )


if __name__ == "__main__":
    unittest.main()
