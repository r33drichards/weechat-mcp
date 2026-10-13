# SPDX-License-Identifier: GPL-3.0-or-later
"""FastMCP stdio server backed by private, event-loop-safe WeeChat IPC."""

import asyncio
import json
import sys
from urllib.parse import quote

import mcp_types as types
from fastmcp import FastMCP
from fastmcp.resources import Resource
from fastmcp.server.providers import Provider
from fastmcp.tools import Tool, ToolResult
from jsonschema import validate
from mcp.server.subscriptions import (
    InMemorySubscriptionBus,
    ListenHandler,
    ResourceUpdated,
)
from pydantic import PrivateAttr


class Backend:
    """One bounded local connection per MCP host; no automatic command retries."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.pending = {}
        self.sequence = 0
        self.events = InMemorySubscriptionBus()
        self.on_disconnect = lambda: None
        self.task = asyncio.create_task(self.receive())

    async def receive(self):
        try:
            while line := await self.reader.readline():
                message = json.loads(line)
                if message.get("event") == "updated":
                    await self.events.publish(ResourceUpdated(uri=message["uri"]))
                else:
                    future = self.pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(ValueError(message["error"]))
                        else:
                            future.set_result(message["result"])
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("WeeChat bridge disconnected"))
            self.pending.clear()
            self.on_disconnect()

    async def request(self, operation, **params):
        if self.task.done():
            raise ConnectionError("WeeChat bridge disconnected")
        self.sequence += 1
        ident = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            self.writer.write(
                (
                    json.dumps(dict(id=ident, operation=operation, **params)) + "\n"
                ).encode()
            )
            await self.writer.drain()
            return await asyncio.wait_for(future, 10)
        finally:
            self.pending.pop(ident, None)

    async def close(self):
        self.writer.close()
        await self.writer.wait_closed()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


class WeeChatTool(Tool):
    _backend: Backend = PrivateAttr()

    async def run(self, arguments):
        validate(arguments, self.parameters)
        value = await self._backend.request("call", name=self.name, arguments=arguments)
        return ToolResult(
            content=[types.TextContent(type="text", text=json.dumps(value))]
        )


class BufferResource(Resource):
    _backend: Backend = PrivateAttr()

    async def read(self):
        return json.dumps(await self._backend.request("history", uri=str(self.uri)))


class WeeChatProvider(Provider):
    """Discover live scoped commands/buffers on every lookup, including revocation."""

    def __init__(self, backend):
        super().__init__()
        self.backend = backend

    async def _list_tools(self):
        result = []
        for info in await self.backend.request("tools"):
            tool = WeeChatTool(
                name=info["name"],
                description=info["description"],
                parameters=info["inputSchema"],
            )
            tool._backend = self.backend
            result.append(tool)
        return result

    async def _get_tool(self, name, version=None):
        return next(
            (tool for tool in await self._list_tools() if tool.name == name), None
        )

    async def _list_resources(self):
        result = []
        for name in await self.backend.request("buffers"):
            resource = BufferResource(
                name=name,
                uri="weechat://buffer/" + quote(name, safe=""),
                mime_type="application/json",
            )
            resource._backend = self.backend
            result.append(resource)
        return result

    async def _get_resource(self, uri, version=None):
        return next(
            (
                resource
                for resource in await self._list_resources()
                if str(resource.uri) == uri
            ),
            None,
        )


def create_server(backend):
    server = FastMCP(
        "weechat-mcp",
        version="0.1.0",
        providers=[WeeChatProvider(backend)],
        strict_input_validation=True,
    )

    handler = ListenHandler(backend.events)
    backend.on_disconnect = handler.close

    async def listen(ctx, params):
        # Validate scope before the SDK acknowledges the stream. SDK handles
        # correlation, filtering, buffering, acknowledgement and cancellation.
        for uri in params.notifications.resource_subscriptions or ():
            await backend.request("history", uri=uri)
        # This provider emits resource updates only, not list-change events.
        honored = params.model_copy(
            update={
                "notifications": types.SubscriptionFilter(
                    resource_subscriptions=params.notifications.resource_subscriptions
                )
            }
        )
        return await handler(ctx, honored)

    # FastMCP exposes components at a high level; subscriptions/listen is an
    # SDK extension seam. Keep this single pinned private access isolated.
    server._mcp_server.add_request_handler(
        "subscriptions/listen", types.SubscriptionsListenRequestParams, listen
    )
    return server


async def main():
    tcp = len(sys.argv) == 3 and sys.argv[1] == "--loopback-port"
    if not tcp and len(sys.argv) != 2:
        raise SystemExit(
            "Usage: bridge.py /path/to/bridge.sock OR --loopback-port PORT"
        )
    if tcp:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", int(sys.argv[2]), limit=1024 * 1024
        )
    else:
        reader, writer = await asyncio.open_unix_connection(
            sys.argv[1], limit=1024 * 1024
        )
    backend = Backend(reader, writer)
    try:
        await create_server(backend).run_stdio_async(
            show_banner=False, log_level="ERROR"
        )
    finally:
        await backend.close()


if __name__ == "__main__":
    asyncio.run(main())
