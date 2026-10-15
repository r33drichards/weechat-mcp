# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

import weechat_mcp as m
from fake_native import NativeFake


class Fake(NativeFake):
    WEECHAT_RC_OK = 0

    def __init__(self):
        super().__init__()
        self.calls = []
        self.allowed = "irc.railway.#wchat"

    def config_get_plugin(self, name):
        return self.allowed

    def infolist_get(self, kind, ptr, args):
        rows = (
            [
                {"full_name": "irc.railway.#wchat", "pointer": "p"},
                {"full_name": "irc.railway.#private", "pointer": "secret"},
            ]
            if kind == "buffer"
            else [
                {"command": c, "description": c}
                for c in [
                    "msg",
                    "say",
                    "me",
                    "topic",
                    "names",
                    "who",
                    "exec",
                    "set",
                    "python",
                ]
            ]
        )
        return {"rows": rows, "i": -1}

    def infolist_next(self, il):
        il["i"] += 1
        return il["i"] < len(il["rows"])

    def infolist_string(self, il, key):
        return il["rows"][il["i"]].get(key, "")

    infolist_pointer = infolist_string

    def infolist_free(self, il):
        pass

    def buffer_get_string(self, ptr, name):
        return {
            "full_name": "irc.railway.#wchat",
            "localvar_type": "channel",
            "localvar_nick": "dot",
            "localvar_channel": "#wchat",
        }.get(name, "")

    def command(self, ptr, text):
        self.calls.append((ptr, text))
        return 0


class Tests(unittest.TestCase):
    def setUp(self):
        self.w = Fake()
        self.api = m.WeeChatAPI(self.w, full_control=False)
        self.p = m.BridgeProtocol(self.api)

    def test_scope(self):
        self.assertEqual(list(self.api.buffers()), ["irc.railway.#wchat"])
        with self.assertRaises(ValueError):
            self.api.call(
                "weechat_msg", {"buffer": "irc.railway.#private", "arguments": "hi"}
            )

    def test_tools_are_discovered_but_shell_not_exposed(self):
        names = [t["name"] for t in self.api.tools()]
        self.assertIn("weechat_msg", names)
        self.assertNotIn("weechat_exec", names)
        self.assertTrue(
            any(
                c["command"] == "exec" and not c["exposed"]
                for c in self.api.call("weechat_command_catalog", {})
            )
        )
        with self.assertRaises(ValueError):
            self.api.call(
                "weechat_exec", {"buffer": "irc.railway.#wchat", "arguments": "id"}
            )

    def test_send_target_is_fixed(self):
        result = self.api.call(
            "weechat_msg", {"buffer": "irc.railway.#wchat", "arguments": "hello"}
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(self.w.calls, [("p", "/msg * hello")])

    def test_no_injection(self):
        for text in ["hello\n/exec id", "hello\rJOIN #private", "hi\0bye", "x" * 351]:
            with self.assertRaises(ValueError):
                self.api.call(
                    "weechat_say", {"buffer": "irc.railway.#wchat", "arguments": text}
                )
        self.assertFalse(self.w.calls)

    def test_topic_cannot_override_channel(self):
        self.api.call(
            "weechat_topic",
            {"buffer": "irc.railway.#wchat", "arguments": "#private hello"},
        )
        self.assertEqual(self.w.calls[0][1], "/topic #wchat #private hello")

    def test_names_target_fixed(self):
        self.api.call("weechat_names", {"buffer": "irc.railway.#wchat"})
        self.assertEqual(self.w.calls[0][1], "/names #wchat")
        with self.assertRaises(ValueError):
            self.api.call(
                "weechat_names",
                {"buffer": "irc.railway.#wchat", "arguments": "#private"},
            )

    def test_removed_history_tool(self):
        self.assertNotIn(
            "weechat_read_recent_messages", [tool["name"] for tool in self.api.tools()]
        )
        with self.assertRaises(ValueError):
            self.api.call(
                "weechat_read_recent_messages", {"buffer": "irc.railway.#wchat"}
            )
        self.assertFalse(hasattr(self.api, "lines"))

    def test_native_buffers_validation_and_revocation(self):
        tool = "weechat_buffers"
        buffer = "irc.railway.#wchat"
        for bad in (0, -1, 501, True, False, 1.5, "50", None):
            with self.assertRaises(ValueError):
                self.api.call(tool, {"buffer": buffer, "limit": bad})
        for args in (
            {"limit": 2},
            {"buffer": 42},
            {"buffer": buffer, "extra": 1},
            {"buffer": buffer, "cursor": None},
        ):
            with self.assertRaises(ValueError):
                self.api.call(tool, args)
        for name in ("irc.railway.#private", "unknown", "irc.railway.%23wchat"):
            with self.assertRaises(ValueError):
                self.api.call(tool, {"buffer": name})
        self.w.add_line("first")
        self.w.add_line("second")
        snapshot = self.api.call(tool, {"buffer": buffer, "limit": 1})
        self.w.allowed = ""
        with self.assertRaises(ValueError):
            self.api.call(tool, {"buffer": buffer, "cursor": snapshot["next_cursor"]})
        self.assertFalse(self.w.calls)


class FakeBackend:
    def __init__(self):
        self.api = m.WeeChatAPI(Fake(), full_control=False)
        self.protocol = m.BridgeProtocol(self.api)
        from mcp.server.subscriptions import InMemorySubscriptionBus

        self.events = InMemorySubscriptionBus()

    async def request(self, operation, **params):
        response = self.protocol.handle(dict(id=1, operation=operation, **params))
        if "error" in response:
            raise ValueError(response["error"])
        return response["result"]


class FastMCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from fastmcp import Client

        from bridge import create_server

        self.backend = FakeBackend()
        self.client = Client(create_server(self.backend))
        await self.client.__aenter__()

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)

    async def test_tool_discovery(self):
        names = [t.name for t in await self.client.list_tools()]
        self.assertIn("weechat_msg", names)
        self.assertNotIn("weechat_read_recent_messages", names)
        self.assertNotIn("weechat_exec", names)

    async def test_native_buffer_tool(self):
        import json

        from mcp_types import TextContent

        buffer = "irc.railway.#wchat"
        for i in range(3):
            self.backend.api.w.add_line(str(i))
        result = await self.client.call_tool(
            "weechat_buffers", {"buffer": buffer, "limit": 2}
        )
        self.assertFalse(result.is_error)
        content = result.content[0]
        assert isinstance(content, TextContent)
        snapshot = json.loads(content.text)
        self.assertEqual([x["id"] for x in snapshot["lines"]], [1, 2])
        result = await self.client.call_tool(
            "weechat_buffers", {"buffer": buffer, "cursor": snapshot["next_cursor"]}
        )
        content = result.content[0]
        assert isinstance(content, TextContent)
        self.assertEqual([x["id"] for x in json.loads(content.text)["lines"]], [0])
        for args in (
            {"buffer": "irc.railway.#private"},
            {"buffer": buffer, "limit": 501},
            {"buffer": buffer, "limit": True},
            {"buffer": buffer, "extra": 1},
        ):
            result = await self.client.call_tool(
                "weechat_buffers", args, raise_on_error=False
            )
            self.assertTrue(result.is_error)
        self.assertFalse(self.backend.api.w.calls)

    async def test_resource_read(self):
        import json

        resources = await self.client.list_resources()
        self.assertEqual(len(resources), 1)
        result = await self.client.read_resource(str(resources[0].uri))
        from mcp_types import TextResourceContents

        assert isinstance(result[0], TextResourceContents)
        self.assertEqual(json.loads(result[0].text)["source"], "weechat_hdata")

    async def test_send(self):
        result = await self.client.call_tool(
            "weechat_msg", {"buffer": "irc.railway.#wchat", "arguments": "hello"}
        )
        self.assertFalse(result.is_error)
        self.assertEqual(self.backend.api.w.calls, [("p", "/msg * hello")])

    async def test_unknown_tool_error(self):
        result = await self.client.call_tool("weechat_exec", {}, raise_on_error=False)
        self.assertTrue(result.is_error)

    async def test_invalid_type(self):
        result = await self.client.call_tool(
            "weechat_msg", {"buffer": 42}, raise_on_error=False
        )
        self.assertTrue(result.is_error)
        self.assertFalse(self.backend.api.w.calls)

    async def test_extra_argument(self):
        result = await self.client.call_tool(
            "weechat_buffers", {"unexpected": True}, raise_on_error=False
        )
        self.assertTrue(result.is_error)

    async def test_scoped_send(self):
        result = await self.client.call_tool(
            "weechat_msg",
            {"buffer": "irc.railway.#private", "arguments": "hi"},
            raise_on_error=False,
        )
        self.assertTrue(result.is_error)
        self.assertFalse(self.backend.api.w.calls)

    async def test_control_characters(self):
        result = await self.client.call_tool(
            "weechat_msg",
            {"buffer": "irc.railway.#wchat", "arguments": "\x01VERSION\x01"},
            raise_on_error=False,
        )
        self.assertTrue(result.is_error)
        self.assertFalse(self.backend.api.w.calls)

    async def test_resource_scope(self):
        with self.assertRaises(Exception):
            await self.client.read_resource(
                self.backend.api.uri("irc.railway.#private")
            )

    async def test_modern_listen_ack_and_event(self):
        import asyncio

        from mcp.client.subscriptions import listen
        from mcp.shared.subscriptions import ResourceUpdated

        uri = self.backend.api.uri("irc.railway.#wchat")
        async with listen(self.client.session, resource_subscriptions=[uri]) as stream:
            self.assertEqual(stream.honored.resource_subscriptions, [uri])
            await self.backend.events.publish(ResourceUpdated(uri=uri))
            event = await asyncio.wait_for(anext(stream), 2)
            self.assertEqual(event, ResourceUpdated(uri=uri))

    async def test_modern_listen_rejects_private_buffer(self):
        from mcp.client.subscriptions import listen
        from mcp.shared.exceptions import MCPError

        uri = self.backend.api.uri("irc.railway.#private")
        with self.assertRaises(MCPError):
            async with listen(self.client.session, resource_subscriptions=[uri]):
                self.fail("Private buffer was acknowledged")

    async def test_modern_listen_filters_other_resources(self):
        import asyncio

        from mcp.client.subscriptions import listen
        from mcp.shared.subscriptions import ResourceUpdated

        uri = self.backend.api.uri("irc.railway.#wchat")
        async with listen(self.client.session, resource_subscriptions=[uri]) as stream:
            await self.backend.events.publish(
                ResourceUpdated(uri="weechat://buffer/other")
            )
            await self.backend.events.publish(ResourceUpdated(uri=uri))
            event = await asyncio.wait_for(anext(stream), 2)
            assert isinstance(event, ResourceUpdated)
            self.assertEqual(event.uri, uri)

    async def test_listen_ack_does_not_promise_list_events(self):
        from mcp.client.subscriptions import listen

        async with listen(self.client.session, tools_list_changed=True) as stream:
            self.assertFalse(stream.honored.tools_list_changed)

    async def test_cancel_and_relisten(self):
        import asyncio

        from mcp.client.subscriptions import listen
        from mcp.shared.subscriptions import ResourceUpdated

        uri = self.backend.api.uri("irc.railway.#wchat")
        async with listen(self.client.session, resource_subscriptions=[uri]):
            pass
        async with listen(self.client.session, resource_subscriptions=[uri]) as stream:
            await self.backend.events.publish(ResourceUpdated(uri=uri))
            self.assertEqual(
                await asyncio.wait_for(anext(stream), 2), ResourceUpdated(uri=uri)
            )


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_fails_pending_request_and_closes_streams(self):
        import asyncio

        from bridge import Backend

        class Writer:
            def write(self, data):
                pass

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        reader = asyncio.StreamReader()
        backend = Backend(reader, Writer())
        closed = []
        backend.on_disconnect = lambda: closed.append(True)
        pending = asyncio.create_task(backend.request("buffers"))
        await asyncio.sleep(0)
        reader.feed_eof()
        with self.assertRaises(ConnectionError):
            await pending
        self.assertEqual(closed, [True])
        with self.assertRaises(ConnectionError):
            await backend.request("buffers")
        await backend.close()


if __name__ == "__main__":
    unittest.main()
