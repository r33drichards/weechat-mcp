# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline integration of the WeeChat capture, bridge and event wire adapter."""

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastmcp import Client
from fastmcp.server.auth import AccessToken
from mcp import types
from mcp.server.subscriptions import InMemorySubscriptionBus

import weechat_mcp as plugin
from bridge import (
    SharedSecretVerifier,
    create_server,
    event_settings,
    run_with_event_workers,
)
from mcp_events import EventManager
from test_mcp import Fake
from test_mcp_events import Sender


class CaptureFake(Fake):
    def __init__(self):
        super().__init__()
        self.present = True
        self.buffer_type = "channel"

    def buffer_get_string(self, ptr, name):
        return {
            "full_name": "irc.railway.#wchat",
            "localvar_type": self.buffer_type,
            "localvar_nick": "dot",
        }.get(name, "")

    def string_remove_color(self, text, _):
        return text

    def infolist_get(self, kind, ptr, args):
        result = super().infolist_get(kind, ptr, args)
        if kind == "buffer" and not self.present:
            result["rows"] = []
        return result


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.fake = CaptureFake()
        self.api = plugin.WeeChatAPI(self.fake)
        self.protocol = plugin.BridgeProtocol(self.api)

    def test_revocation_distinct_from_temporary_missing_buffer(self):
        uri = self.api.uri("irc.railway.#wchat")
        self.fake.present = False
        missing = self.protocol.handle(dict(id=1, operation="event_history", uri=uri))
        self.assertEqual(missing["error_code"], "channel_unavailable")
        self.fake.allowed = ""
        revoked = self.protocol.handle(dict(id=1, operation="event_history", uri=uri))
        self.assertNotIn("error_code", revoked)
        self.assertIn("error", revoked)

    def test_only_channel_scope_can_be_subscribed(self):
        self.fake.buffer_type = "private"
        result = self.protocol.handle(
            dict(
                id=1, operation="event_history", uri=self.api.uri("irc.railway.#wchat")
            )
        )
        self.assertIn("error", result)

    def test_print_capture_marks_incoming_status_and_own_messages(self):
        with patch.object(plugin, "api", self.api), patch.object(plugin, "clients", {}):
            for tags, nick in [
                ("irc_privmsg,nick_alice", "alice"),
                ("irc_join,nick_alice", "alice"),
                ("irc_privmsg,nick_DOT", "DOT"),
                ("irc_privmsg,nick_old,notify_self", "old"),
            ]:
                self.fake.add_line("hello", prefix=nick, tags_array=tags.split(","))
                plugin.printed("", "p", "1700000000", tags, 1, 0, nick, "hello")
        lines = self.api.event_history(self.api.uri("irc.railway.#wchat"))["lines"]
        self.assertEqual(
            [line["is_message"] for line in lines], [True, False, True, True]
        )
        self.assertEqual(
            [line["is_self"] for line in lines], [False, False, True, True]
        )
        self.assertFalse(self.fake.calls)


class SettingsTests(unittest.TestCase):
    def test_feature_off_by_default(self):
        self.assertIsNone(event_settings({}, None, "http"))

    def test_stdio_and_unbound_private_mode_fail_closed(self):
        for transport in ("stdio", "http"):
            with self.assertRaises(ValueError):
                event_settings({"MCP_EVENTS_DB": "/runtime/events.db"}, None, transport)

    def test_private_mode_requires_explicit_operator_owner_binding(self):
        _, owner, principal = event_settings(
            {"MCP_EVENTS_DB": "/runtime/events.db", "MCP_EVENTS_OWNER": "single-owner"},
            None,
            "http",
        )
        self.assertEqual(owner, "railway-private:single-owner")
        self.assertEqual(principal(), owner)

    def test_bearer_principal_requires_current_verified_token(self):
        verifier = SharedSecretVerifier("dummy-test-bearer-only-" * 2)
        _, owner, principal = event_settings(
            {"MCP_EVENTS_DB": "/runtime/events.db"}, verifier, "http"
        )
        with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
            with self.assertRaises(ValueError):
                principal()
        for token, good in [
            ("dummy-test-bearer-only-" * 2, True),
            ("wrong-dummy-token", False),
        ]:
            with patch(
                "fastmcp.server.dependencies.get_access_token",
                return_value=AccessToken(token=token, client_id="owner", scopes=[]),
            ):
                _, owner, principal = event_settings(
                    {"MCP_EVENTS_DB": "/runtime/events.db"}, verifier, "http"
                )
                if good:
                    self.assertEqual(principal(), owner)
                else:
                    with self.assertRaises(ValueError):
                        principal()


class LocalBackend:
    def __init__(self):
        self.api = plugin.WeeChatAPI(CaptureFake())
        self.protocol = plugin.BridgeProtocol(self.api)
        self.events = InMemorySubscriptionBus()
        self.on_buffer_updated = lambda uri: None

    async def request(self, operation, **params):
        result = self.protocol.handle(dict(id=1, operation=operation, **params))
        if result.get("error_code") == "channel_unavailable":
            raise ConnectionError(result["error"])
        if "error" in result:
            raise ValueError(result["error"])
        return result["result"]


class IntegratedProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_plugin_reload_cannot_replay_pre_opt_in_lines(self):
        backend, sender = LocalBackend(), Sender()
        backend.api.w.add_line("before opt-in")
        with tempfile.TemporaryDirectory() as directory:
            manager = EventManager(
                str(Path(directory) / "events.db"),
                backend,
                lambda owner: owner == "owner",
                sender=sender,
            )
            params = {
                "name": "message.created",
                "arguments": {"channel_id": "irc.railway.#wchat", "trigger": "message"},
                "delivery": {
                    "mode": "webhook",
                    "url": "https://receiver.example/callback/test",
                    "secret": "whsec_"
                    + base64.b64encode(b"fake-signing-key-" * 2).decode(),
                },
            }
            try:
                await manager.subscribe("owner", params)
                public_epoch = backend.api.buffer_data("irc.railway.#wchat")["epoch"]
                backend.api.w.add_line("queued before reload")
                await manager.collect()
                backend.api = plugin.WeeChatAPI(backend.api.w)
                backend.protocol = plugin.BridgeProtocol(backend.api)
                self.assertNotEqual(
                    public_epoch, backend.api.buffer_data("irc.railway.#wchat")["epoch"]
                )
                backend.api.w.add_line("arrived after reload")
                await manager.collect()
                await manager.deliver_once()
                await manager.deliver_once()
                await manager.deliver_once()
                self.assertEqual(
                    [json.loads(item[1])["data"]["text"] for item in sender.deliveries],
                    ["queued before reload", "arrived after reload"],
                )
                self.assertFalse(
                    (await manager.subscribe("owner", params))["truncated"]
                )
            finally:
                await manager.close()

    async def test_changed_native_buffer_identity_does_not_backfill_unknown_history(
        self,
    ):
        backend, sender = LocalBackend(), Sender()
        backend.api.w.add_line("before opt-in")
        with tempfile.TemporaryDirectory() as directory:
            manager = EventManager(
                str(Path(directory) / "events.db"),
                backend,
                lambda owner: True,
                sender=sender,
            )
            params = {
                "name": "message.created",
                "arguments": {"channel_id": "irc.railway.#wchat", "trigger": "message"},
                "delivery": {
                    "mode": "webhook",
                    "url": "https://receiver.example/callback/test",
                    "secret": "whsec_"
                    + base64.b64encode(b"fake-signing-key-" * 2).decode(),
                },
            }
            try:
                await manager.subscribe("owner", params)
                backend.api.w.nodes["p"]["id"] += 1
                backend.api.w.add_line("uncertain new-lifetime history")
                await manager.collect()
                await manager.deliver_once()
                self.assertFalse(sender.deliveries)
                self.assertTrue((await manager.subscribe("owner", params))["truncated"])
                backend.api.w.add_line("after new baseline")
                await manager.collect()
                await manager.deliver_once()
                self.assertEqual(
                    json.loads(sender.deliveries[0][1])["data"]["text"],
                    "after new baseline",
                )
            finally:
                await manager.close()

    async def test_background_failure_stops_server_without_exception_disclosure(self):
        stopped = asyncio.Event()

        async def serve():
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def fail():
            raise RuntimeError("fake-sensitive-sentinel")

        task = asyncio.create_task(fail())
        intake = asyncio.create_task(asyncio.Event().wait())
        manager = SimpleNamespace(task=task, intake_task=intake)
        try:
            with self.assertRaisesRegex(RuntimeError, "worker stopped") as caught:
                await run_with_event_workers(serve(), manager)
            self.assertNotIn("fake-sensitive-sentinel", str(caught.exception))
            self.assertTrue(stopped.is_set())
        finally:
            intake.cancel()
            await asyncio.gather(task, intake, return_exceptions=True)

    async def test_wire_subscription_capture_delivery_refresh_and_stop(self):
        backend, sender = LocalBackend(), Sender()
        with tempfile.TemporaryDirectory() as directory:
            manager = EventManager(
                str(Path(directory) / "events.db"),
                backend,
                lambda owner: owner == "test-owner",
                sender=sender,
            )
            server = create_server(
                backend, event_manager=manager, event_principal=lambda: "test-owner"
            )
            async with Client(server) as client:
                # Use generic result model with extra allowed for extension response.
                from event_protocol import EventResult

                async def event_request(method, params):
                    result = await client.session.send_request(
                        types.Request[dict, str](method=method, params=params),
                        EventResult,
                    )
                    return result.model_dump(by_alias=True)

                catalog = await event_request("events/list", {})
                self.assertEqual(catalog["events"][0]["name"], "message.created")
                self.assertFalse(sender.requests)
                args = {
                    "name": "message.created",
                    "arguments": {
                        "channel_id": "irc.railway.#wchat",
                        "trigger": "mention",
                        "nick": "dot",
                    },
                    "delivery": {
                        "mode": "webhook",
                        "url": "https://receiver.example/callback/chat",
                        "secret": "whsec_"
                        + base64.b64encode(b"offline-dummy-key-" * 2).decode(),
                    },
                }
                subscribed = await event_request("events/subscribe", args)
                self.assertTrue(subscribed["id"].startswith("sub_"))
                with (
                    patch.object(plugin, "api", backend.api),
                    patch.object(plugin, "clients", {}),
                ):
                    backend.api.w.add_line(
                        "dot: hello", tags_array=["irc_privmsg", "nick_alice"]
                    )
                    plugin.printed(
                        "",
                        "p",
                        "1700000000",
                        "irc_privmsg,nick_alice",
                        1,
                        1,
                        "alice",
                        "dot: hello",
                    )
                backend.on_buffer_updated(backend.api.uri("irc.railway.#wchat"))
                await manager.collect()
                await manager.deliver_once()
                self.assertEqual(
                    json.loads(sender.deliveries[0][1])["data"]["text"], "dot: hello"
                )
                args["delivery"].pop("secret")
                await event_request("events/unsubscribe", args)
                await manager.deliver_once()
                self.assertEqual(len(sender.deliveries), 1)
                self.assertFalse(backend.api.w.calls)
            await manager.close()


if __name__ == "__main__":
    unittest.main()
