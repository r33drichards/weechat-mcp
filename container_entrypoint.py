# SPDX-License-Identifier: GPL-3.0-or-later
"""Run an isolated WeeChat and its stdio MCP bridge; stdout is protocol-only."""

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def configuration(env):
    """Validate all command interpolation, including WeeChat expression syntax."""
    host = env.get("IRC_HOST", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("IRC_HOST must be a DNS hostname or IPv4 address")
    port = int(env.get("IRC_PORT", "6697"))
    if not 1 <= port <= 65535:
        raise ValueError("IRC_PORT must be between 1 and 65535")
    tls = env.get("IRC_TLS", "true").lower()
    if tls not in ("true", "false"):
        raise ValueError("IRC_TLS must be true or false")
    nick = env.get("IRC_NICK", "weechat-mcp")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,29}", nick):
        raise ValueError(
            "IRC_NICK must be 1-30 letters, digits, underscores or hyphens"
        )
    channels = env.get("IRC_CHANNELS", "#wchat").split(",")
    if not all(re.fullmatch(r"#[A-Za-z0-9_.-]{1,100}", c) for c in channels):
        raise ValueError("IRC_CHANNELS must be comma-separated #channel names")
    if len(channels) > 20 or len(channels) != len(set(channels)):
        raise ValueError("IRC_CHANNELS must contain 1-20 distinct channels")
    return [
        "/plugin load irc",
        "/plugin load python",
        "/python load " + str(Path(__file__).with_name("weechat_mcp.py")),
        "/set plugins.var.python.mcp.buffers "
        + ",".join("irc.docker." + c for c in channels),
        f"/server add docker {host}/{port} -{'tls' if tls == 'true' else 'notls'}",
        "/set irc.server.docker.tls_verify on",
        f"/set irc.server.docker.nicks {nick}",
        "/set irc.server.docker.autojoin " + ",".join(channels),
        "/connect docker",
    ]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run():
    commands = configuration(os.environ)
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport not in ("stdio", "http"):
        raise ValueError("MCP_TRANSPORT must be stdio or http")
    if transport == "http":
        from bridge import http_settings

        http_settings(os.environ)  # Fail before connecting IRC if HTTP is unsafe.
    os.umask(0o077)
    stopping = 0

    def on_signal(signum, frame):
        nonlocal stopping
        stopping = signum

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_signal)
    children = []
    with tempfile.TemporaryDirectory(prefix="weechat-mcp-") as profile:
        try:
            env = dict(os.environ)
            loopback = env.get("WEECHAT_MCP_LOOPBACK_PORT")
            if loopback not in (None, "0"):
                raise ValueError(
                    "WEECHAT_MCP_LOOPBACK_PORT only supports explicit test value 0"
                )
            cmd = ["weechat-headless", "-d", profile, "-p", "--stdout"]
            for command in commands:
                cmd += ["-r", command]
            weechat = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=sys.stderr,
                stderr=sys.stderr,
                env={
                    key: value for key, value in env.items() if key != "MCP_AUTH_TOKEN"
                },
            )
            children.append(weechat)
            endpoint = (
                Path(profile) / "mcp" / ("loopback-port" if loopback else "bridge.sock")
            )
            deadline = time.monotonic() + 20
            while not endpoint.exists():
                if stopping:
                    return 128 + stopping
                if weechat.poll() is not None:
                    raise RuntimeError("WeeChat exited before its plugin became ready")
                if time.monotonic() >= deadline:
                    raise RuntimeError("Timed out waiting for WeeChat's plugin socket")
                time.sleep(0.05)
            connection_args = (
                ["--loopback-port", endpoint.read_text().strip()]
                if loopback
                else [str(endpoint)]
            )
            bridge = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("bridge.py")),
                    *connection_args,
                ],
                env=env,
            )
            children.append(bridge)
            while not stopping:
                if weechat.poll() is not None:
                    raise RuntimeError("WeeChat exited; stopping MCP")
                if bridge.poll() is not None:
                    return bridge.returncode if bridge.returncode >= 0 else 1
                time.sleep(0.05)
            return 128 + stopping
        finally:
            for child in reversed(children):
                stop(child)


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"weechat-mcp: {exc}", file=sys.stderr)
        sys.exit(1)
