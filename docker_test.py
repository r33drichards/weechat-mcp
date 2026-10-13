# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in Docker/HTTP smoke test against real WeeChat and loopback-only IRC.

Docker (Linux host networking):
    uv run python docker_test.py --miniircd /path/to/miniircd

Same entrypoint/HTTP checks without Docker (does NOT validate the image):
    uv run python docker_test.py --host --miniircd /path/to/miniircd \
        --weechat-root /path/to/extracted/debian/root

No public IRC service, persistent profile, or fixed credential is used.
"""

import argparse
import asyncio
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import anyio
import httpx2
import mcp_types as types
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.subscriptions import ResourceUpdated

from runner import Runner

PROTOCOL = "2026-07-28"
BUFFER = "irc.docker.#wchat"
REPO = Path(__file__).resolve().parent


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--miniircd", required=True, type=Path)
    parser.add_argument(
        "--host", action="store_true", help="Run entrypoint locally; skip Docker"
    )
    parser.add_argument(
        "--weechat-root",
        type=Path,
        help="Host mode: extracted Debian package root (otherwise use PATH)",
    )
    parser.add_argument(
        "--loopback-ipc",
        action="store_true",
        help="Host mode: use ephemeral loopback IPC when Unix sockets are unavailable",
    )
    parser.add_argument("--image", default="weechat-mcp:test")
    parser.add_argument(
        "--skip-build", action="store_true", help="Test an already-built Docker image"
    )
    args = parser.parse_args()
    args.miniircd = args.miniircd.resolve()
    if not args.miniircd.is_file():
        parser.error("--miniircd must identify the official miniircd script")
    if args.weechat_root and not args.host:
        parser.error("--weechat-root requires --host")
    if args.loopback_ipc and not args.host:
        parser.error("--loopback-ipc requires --host; Docker must test Unix IPC")
    if args.host and args.skip_build:
        parser.error("--skip-build is only meaningful in Docker mode")
    return args


def allocate_ports():
    # Hold both sockets at once so the OS cannot return the same port twice.
    with socket.socket() as irc, socket.socket() as http:
        irc.bind(("127.0.0.1", 0))
        http.bind(("127.0.0.1", 0))
        return irc.getsockname()[1], http.getsockname()[1]


def host_environment(env, root, work):
    if root is None:
        if not shutil.which("weechat-headless", path=env.get("PATH")):
            raise RuntimeError("Install WeeChat or supply --weechat-root for --host")
        return env
    root = root.resolve()
    binary = root / "usr/bin/weechat-headless"
    plugins = list(root.glob("usr/lib/*/weechat/plugins/irc.so"))
    if not binary.is_file() or len(plugins) != 1:
        raise RuntimeError("Extracted WeeChat root is missing its binary or IRC plugin")
    plugin_dir = plugins[0].parent
    if not (plugin_dir / "python.so").is_file():
        raise RuntimeError("Extracted WeeChat root is missing its Python plugin")
    # Keep the production entrypoint unchanged. Only substitute the extracted
    # binary/plugin locations that normally live under /usr in the image.
    wrapper_dir = work / "bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "weechat-headless"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"binary = {str(binary)!r}\n"
        f"plugins = {str(plugin_dir)!r}\n"
        "args = [('/plugin load ' + plugins + '/' + arg.split()[-1] + '.so')\n"
        "        if arg in ('/plugin load irc', '/plugin load python') else arg\n"
        "        for arg in sys.argv[1:]]\n"
        "os.execv(binary, [binary, *args])\n"
    )
    wrapper.chmod(0o700)
    return dict(env, PATH=str(wrapper_dir) + os.pathsep + env.get("PATH", ""))


class Processes:
    def __init__(self, work):
        self.work = work
        self.children = []
        self.files = []
        self.container = None

    def launch(self, command, name, env=None):
        logfile = (self.work / (name + ".log")).open("w")
        self.files.append(logfile)
        child = subprocess.Popen(
            command,
            cwd=REPO,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=logfile,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.children.append((name, child))
        return child

    def check_alive(self):
        for name, child in self.children:
            if child.poll() is not None:
                raise RuntimeError(f"{name} exited early (status {child.returncode})")

    def logs(self, token):
        for path in sorted(self.work.glob("*.log")):
            content = path.read_text(errors="replace")[-20000:].replace(
                token, "[redacted]"
            )
            print(f"\n--- {path.name} ---\n{content}", file=sys.stderr)

    def close(self):
        if self.container:
            # Removing the named test container also handles a failed docker-run
            # client, and never touches a user's unrelated containers.
            try:
                subprocess.run(
                    ["docker", "rm", "-f", self.container],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"Container cleanup failed: {exc}", file=sys.stderr)
        for _, child in reversed(self.children):
            # Also kill descendants if the entrypoint itself failed early.
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
        for logfile in self.files:
            logfile.close()


async def wait_port(port, processes):
    with anyio.fail_after(25):
        while True:
            processes.check_alive()
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await anyio.sleep(0.1)
                continue
            writer.close()
            await writer.wait_closed()
            return


class IRCObserver:
    """An independent raw IRC client, not the MCP server's connection."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    async def send(self, message):
        self.writer.write((message + "\r\n").encode())
        await self.writer.drain()

    async def until(self, text):
        lines = []
        with anyio.fail_after(10):
            while True:
                raw = await self.reader.readline()
                if not raw:
                    raise ConnectionError("Independent IRC observer disconnected")
                line = raw.decode(errors="replace").rstrip("\r\n")
                if line.startswith("PING "):
                    await self.send("PONG " + line[5:])
                    continue
                lines.append(line)
                if text in line:
                    return lines

    @classmethod
    async def connect(cls, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        observer = cls(reader, writer)
        try:
            await observer.send("NICK dockerobserver")
            await observer.send("USER dockerobserver 0 * :Local Docker smoke test")
            await observer.until(" 001 ")
            await observer.send("JOIN #wchat")
            await observer.until(" 366 ")
            return observer
        except BaseException:
            await observer.close()
            raise

    async def wait_for_weechat(self):
        with anyio.fail_after(20):
            while True:
                await self.send("NAMES #wchat")
                lines = await self.until(" 366 ")
                names = [
                    nick.lstrip("@+")
                    for line in lines
                    if " 353 " in line
                    for nick in line.rsplit(" :", 1)[-1].split()
                ]
                if "dockertest" in names:
                    return
                await anyio.sleep(0.1)

    async def close(self):
        self.writer.close()
        await self.writer.wait_closed()


async def exercise(irc_port, http_port, token, processes):
    await wait_port(irc_port, processes)
    await wait_port(http_port, processes)
    url = f"http://127.0.0.1:{http_port}/mcp"
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL,
    }
    async with httpx2.AsyncClient(timeout=10, trust_env=False) as unauthenticated:
        for authorization in (None, "Bearer invalid-test-token"):
            request_headers = dict(headers)
            if authorization:
                request_headers["Authorization"] = authorization
            response = await unauthenticated.post(
                url,
                headers=request_headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            assert response.status_code in (401, 403), (
                f"Unauthorized request was not denied: {response.status_code}"
            )
    print("HTTP_MISSING_AND_WRONG_BEARER_DENIED_OK", flush=True)

    observer = await IRCObserver.connect(irc_port)
    runner = None
    try:
        runner = Runner(processes.work / "host-turn.sqlite", "dockertest", max_turns=1)
        await observer.wait_for_weechat()
        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + token},
            timeout=httpx2.Timeout(20, read=30),
            trust_env=False,
        ) as http:
            transport = streamable_http_client(url, http_client=http)
            async with Client(
                transport, mode=PROTOCOL, cache=None, read_timeout_seconds=15
            ) as sdk:
                assert sdk.session.protocol_version == PROTOCOL
                tools = await sdk.list_tools()
                assert any(tool.name == "weechat_msg" for tool in tools.tools)
                resources = await sdk.list_resources()
                matches = [item for item in resources.resources if item.name == BUFFER]
                assert len(matches) == 1, resources
                uri = str(matches[0].uri)
                print("HTTP_MODERN_MCP_TOOLS_AND_RESOURCES_OK", flush=True)

                with anyio.fail_after(15):
                    async with sdk.listen(resource_subscriptions=[uri]) as events:
                        assert uri in (events.honored.resource_subscriptions or [])
                        print("HTTP_SUBSCRIPTIONS_LISTEN_ACKNOWLEDGED_OK", flush=True)
                        inbound = "dockertest: observer-to-http-" + secrets.token_hex(8)
                        await observer.send("PRIVMSG #wchat :" + inbound)
                        async for event in events:
                            if isinstance(event, ResourceUpdated) and event.uri == uri:
                                snapshot = await sdk.read_resource(uri)
                                content = snapshot.contents[0]
                                assert isinstance(content, types.TextResourceContents)
                                history = json.loads(content.text)
                                if inbound in json.dumps(history):
                                    break
                        else:
                            raise AssertionError(
                                "Listen stream ended before the IRC update"
                            )
                        print(
                            "HTTP_REAL_IRC_NOTIFICATION_AND_RESOURCE_READ_OK",
                            flush=True,
                        )
                        assert runner.ingest(history) == 1
                        turn = runner.start_turn()
                        assert turn and turn["message"]["message"] == inbound
                        assert runner.start_turn() is None, "Only one host turn may run"

                        outbound = "http-to-observer-" + secrets.token_hex(8)
                        result = await sdk.call_tool(
                            "weechat_msg", {"buffer": BUFFER, "arguments": outbound}
                        )
                        assert not result.model_dump(by_alias=True).get("isError"), (
                            result
                        )
                        lines = await observer.until(outbound)
                        assert any(
                            line.startswith(":dockertest!")
                            and " PRIVMSG #wchat :" + outbound in line
                            for line in lines
                        ), lines
                        print(
                            "HTTP_MCP_SEND_RECEIVED_BY_INDEPENDENT_IRC_CLIENT_OK",
                            flush=True,
                        )
                        runner.finish_turn(turn)
                        assert runner.ingest(history) == 0, (
                            "Replay enqueued a duplicate"
                        )
                        assert runner.counts() == {"done": 1}
                        print("HTTP_MENTION_TRIGGER_HOST_TURN_AND_REPLY_OK", flush=True)
                # Closing the acknowledged subscription must leave the HTTP
                # connection usable for a new read, rather than wedging it.
                await sdk.read_resource(uri)
                print("HTTP_LISTEN_CLOSE_AND_SUBSEQUENT_READ_OK", flush=True)
    finally:
        if runner is not None:
            runner.close()
        await observer.close()
    processes.check_alive()


def verify_host_shutdown(process, work, http_port):
    """Signal only the supervisor; verify cleanup and reaping where visible."""
    children_file = Path(f"/proc/{process.pid}/task/{process.pid}/children")
    child_pids = None
    if children_file.exists():
        child_pids = [int(pid) for pid in children_file.read_text().split()]
        assert len(child_pids) == 2, (
            f"Expected WeeChat and bridge children: {child_pids}"
        )
    profiles = [path for path in work.glob("weechat-mcp-*") if path.is_dir()]
    assert len(profiles) == 1, "Could not identify the temporary WeeChat profile"
    ports = [http_port]
    loopback_port = profiles[0] / "mcp" / "loopback-port"
    if loopback_port.exists():
        ports.append(int(loopback_port.read_text()))
    process.terminate()
    assert process.wait(timeout=15) == 128 + signal.SIGTERM
    if child_pids is not None:
        assert all(not Path(f"/proc/{pid}").exists() for pid in child_pids), (
            "Supervisor did not reap all children"
        )
        print("HOST_SIGTERM_CHILD_REAP_OK", flush=True)
    else:
        print("HOST_CHILD_PID_REAP_CHECK_NOT_RUN: /proc unavailable", flush=True)
    for port in ports:
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=1)
        except ConnectionRefusedError:
            continue
        connection.close()
        raise AssertionError(f"Listener still open after supervisor exit: {port}")
    assert all(not profile.exists() for profile in profiles), (
        "Temporary WeeChat profile was not removed"
    )
    print("HOST_SIGTERM_CLOSES_LISTENERS_AND_REMOVES_PROFILE_OK", flush=True)


def main():
    args = arguments()
    if not args.host:
        if sys.platform != "linux":
            raise RuntimeError("Docker test requires Linux host networking; use --host")
        if not shutil.which("docker"):
            raise RuntimeError(
                "Docker is unavailable; Docker test was not run (try --host)"
            )
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=30)
        if not args.skip_build:
            subprocess.run(
                ["docker", "build", "-t", args.image, "."], cwd=REPO, check=True
            )
    irc_port, http_port = allocate_ports()
    token = secrets.token_urlsafe(32)
    settings = {
        "MCP_TRANSPORT": "http",
        # Even in host-network Docker mode, keep this test listener private.
        "MCP_HOST": "127.0.0.1",
        "PORT": str(http_port),
        "MCP_AUTH_TOKEN": token,
        "IRC_HOST": "127.0.0.1",
        "IRC_PORT": str(irc_port),
        "IRC_TLS": "false",
        "IRC_NICK": "dockertest",
        "IRC_CHANNELS": "#wchat",
    }
    with tempfile.TemporaryDirectory(prefix="weechat-docker-test-") as directory:
        work = Path(directory)
        processes = Processes(work)
        try:
            processes.launch(
                [
                    sys.executable,
                    str(args.miniircd),
                    "--listen",
                    "127.0.0.1",
                    "--ports",
                    str(irc_port),
                    "--verbose",
                ],
                "miniircd",
            )
            anyio.run(wait_port, irc_port, processes)
            env = dict(os.environ, **settings)
            env.pop("WEECHAT_MCP_LOOPBACK_PORT", None)
            if args.host:
                env = host_environment(env, args.weechat_root, work)
                env["TMPDIR"] = str(work)
                if args.loopback_ipc:
                    env["WEECHAT_MCP_LOOPBACK_PORT"] = "0"
                    print(
                        "HOST_IPC: ephemeral loopback (Unix socket IPC NOT tested)",
                        flush=True,
                    )
                command = [sys.executable, str(REPO / "container_entrypoint.py")]
                print("HOST_MODE: Docker image build/run NOT performed", flush=True)
            else:
                processes.container = "weechat-mcp-test-" + secrets.token_hex(6)
                command = [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "host",
                    "--name",
                    processes.container,
                ]
                for key in settings:
                    command += ["-e", key]
                command += [args.image]
            service = processes.launch(command, "weechat-mcp", env)
            anyio.run(exercise, irc_port, http_port, token, processes)
            if args.host:
                verify_host_shutdown(service, work, http_port)
        except BaseException:
            processes.logs(token)
            raise
        finally:
            processes.close()
    label = (
        "HOST_ENTRYPOINT_HTTP_SMOKE_OK" if args.host else "DOCKER_IMAGE_HTTP_SMOKE_OK"
    )
    print(label, flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        sys.exit(1)
