# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

import weechat_mcp as m


class Fake:
    WEECHAT_RC_OK = 0

    def __init__(self):
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
        return "#wchat"

    def command(self, ptr, text):
        self.calls.append((ptr, text))
        return 0


class Tests(unittest.TestCase):
    def setUp(self):
        self.w = Fake()
        self.api = m.WeeChatAPI(self.w)
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

    def test_ring_bound(self):
        self.api.lines["irc.railway.#wchat"].extend(range(250))
        self.assertEqual(len(self.api.history("irc.railway.#wchat")["lines"]), 200)

    def test_recent_messages_limits_and_metadata(self):
        buffer = "irc.railway.#wchat"
        self.api.lines[buffer].extend({"sequence": i} for i in range(1, 251))
        self.api.evicted[buffer] = 50
        result = self.api.call("weechat_read_recent_messages", {"buffer": buffer})
        self.assertEqual(
            [x["sequence"] for x in result["lines"]], list(range(201, 251))
        )
        self.assertEqual(result["epoch"], self.api.epoch)
        self.assertEqual(result["evicted_through"], 50)
        self.assertEqual(result["retained"], 200)
        self.assertEqual(result["available"], 200)
        self.assertEqual(result["returned"], 50)
        self.assertTrue(result["truncated"])
        self.assertIn("older lines not imported", result["since"])
        for limit in (1, 200):
            result = self.api.call(
                "weechat_read_recent_messages", {"buffer": buffer, "limit": limit}
            )
            self.assertEqual(len(result["lines"]), limit)
            self.assertEqual(result["truncated"], limit < 200)
        self.assertFalse(self.w.calls)

    def test_recent_messages_scope_revocation_and_validation(self):
        tool = "weechat_read_recent_messages"
        buffer = "irc.railway.#wchat"
        result = self.api.call(tool, {"buffer": buffer})
        self.assertEqual(result["lines"], [])
        self.assertEqual(result["returned"], 0)
        self.assertFalse(result["truncated"])
        for bad in (0, -1, 201, True, False, 1.5, "50", None):
            with self.assertRaises(ValueError):
                self.api.call(tool, {"buffer": buffer, "limit": bad})
        for args in ({}, {"buffer": 42}, {"buffer": buffer, "extra": 1}):
            with self.assertRaises(ValueError):
                self.api.call(tool, args)
        for name in ("irc.railway.#private", "unknown", "irc.railway.%23wchat"):
            with self.assertRaises(ValueError):
                self.api.call(tool, {"buffer": name})
        self.api.lines[buffer].append({"sequence": 1, "message": "retained"})
        self.w.allowed = ""
        with self.assertRaises(ValueError):
            self.api.call(tool, {"buffer": buffer})
        self.assertFalse(self.w.calls)


class FakeBackend:
    def __init__(self):
        self.api = m.WeeChatAPI(Fake())
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
        self.assertIn("weechat_read_recent_messages", names)
        self.assertNotIn("weechat_exec", names)

    async def test_recent_messages_tool(self):
        import json

        buffer = "irc.railway.#wchat"
        self.backend.api.lines[buffer].extend(
            {"sequence": i, "message": str(i)} for i in range(1, 4)
        )
        result = await self.client.call_tool(
            "weechat_read_recent_messages", {"buffer": buffer, "limit": 2}
        )
        self.assertFalse(result.is_error)
        from mcp_types import TextContent

        content = result.content[0]
        assert isinstance(content, TextContent)
        history = json.loads(content.text)
        self.assertEqual([x["sequence"] for x in history["lines"]], [2, 3])
        for args in (
            {"buffer": "irc.railway.#private"},
            {"buffer": buffer, "limit": 201},
            {"buffer": buffer, "limit": True},
            {"buffer": buffer, "extra": 1},
        ):
            result = await self.client.call_tool(
                "weechat_read_recent_messages", args, raise_on_error=False
            )
            self.assertTrue(result.is_error)
        self.backend.api.w.allowed = ""
        result = await self.client.call_tool(
            "weechat_read_recent_messages", {"buffer": buffer}, raise_on_error=False
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
        self.assertEqual(json.loads(result[0].text)["retained"], 200)

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
