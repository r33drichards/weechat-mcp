# SPDX-License-Identifier: GPL-3.0-or-later
"""Synchronous experiment host facade over the official modern MCP client."""

import json
import sys
from collections import deque
from pathlib import Path

import anyio
from anyio.from_thread import start_blocking_portal
from mcp import Client as SDKClient
from mcp import StdioServerParameters
from mcp.shared.subscriptions import ResourceUpdated


class Client:
    def __init__(self, port):
        self.notifications = deque()
        self.subscriptions = {}
        self.portal_context = start_blocking_portal()
        self.portal = self.portal_context.__enter__()
        try:
            self.worker, self.sdk = self.portal.start_task(self.run, port)
            self.version = self.sdk.session.protocol_version
        except BaseException:
            self.portal_context.__exit__(*sys.exc_info())
            raise

    async def run(self, port, *, task_status=anyio.TASK_STATUS_IGNORED):
        self.stop = anyio.Event()
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                str(Path(__file__).with_name("bridge.py")),
                "--loopback-port",
                str(port),
            ],
        )
        async with SDKClient(
            params, mode="2026-07-28", cache=None, read_timeout_seconds=10
        ) as sdk:
            task_status.started(sdk)
            await self.stop.wait()

    async def watch(self, uri, *, task_status=anyio.TASK_STATUS_IGNORED):
        async with self.sdk.listen(resource_subscriptions=[uri]) as stream:
            if uri not in (stream.honored.resource_subscriptions or []):
                raise RuntimeError("Server did not acknowledge the requested resource")
            task_status.started()
            async for event in stream:
                self.notifications.append(event)

    def listen(self, uri):
        if uri not in self.subscriptions:
            task, _ = self.portal.start_task(self.watch, uri)
            self.subscriptions[uri] = task
        return self.snapshot(uri)

    def unlisten(self, uri):
        task = self.subscriptions.pop(uri, None)
        if task is not None:
            task.cancel()

    def snapshot(self, uri):
        result = self.portal.call(self.sdk.read_resource, uri)
        return json.loads(result.contents[0].text)

    def updates(self, uri):
        task = self.subscriptions.get(uri)
        if task is not None and task.done():
            task.result()  # Surface a lost stream instead of silently polling forever.
            raise ConnectionError("Resource subscription ended")
        matching = False
        while True:
            try:
                event = self.notifications.popleft()
            except IndexError:
                break
            matching |= isinstance(event, ResourceUpdated) and event.uri == uri
        return self.snapshot(uri) if matching else None

    def tools(self):
        return self.portal.call(self.sdk.list_tools).model_dump(by_alias=True)

    def resources(self):
        return self.portal.call(self.sdk.list_resources).model_dump(by_alias=True)

    def tool(self, name, arguments):
        result = self.portal.call(self.sdk.call_tool, name, arguments)
        return result.model_dump(by_alias=True)

    def send(self, buffer, message):
        result = self.tool("weechat_msg", {"buffer": buffer, "arguments": message})
        if result.get("isError"):
            raise RuntimeError(result)
        return result

    def close(self):
        for uri in list(self.subscriptions):
            self.unlisten(uri)
        self.portal.call(self.stop.set)
        self.worker.result(timeout=5)
        self.portal_context.__exit__(None, None, None)
