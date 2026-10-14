# SPDX-License-Identifier: GPL-3.0-or-later
"""Isolated real WeeChat acceptance; no network connection or live profile.

Usage: python tools/check_command_acceptance.py --weechat-root /extracted/package/root
"""

import argparse
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--weechat-root", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    root = pathlib.Path(args.weechat_root).resolve()
    repo = pathlib.Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    with tempfile.TemporaryDirectory(prefix="weechat-command-acceptance-") as directory:
        home = pathlib.Path(directory)
        log = open(home / "output.log", "w+")
        command = [
            str(root / "usr/bin/weechat-headless"),
            "-d",
            str(home / "profile"),
            "-p",
            "--stdout",
        ]
        # Load installed packaged plugins only; no servers configured or connected.
        for plugin in sorted(
            (root / "usr/lib/x86_64-linux-gnu/weechat/plugins").glob("*.so")
        ):
            command += ["-r", "/plugin load " + str(plugin)]
        command += [
            "-r",
            "/python load " + str(repo / "weechat_mcp.py"),
            "-r",
            "/set plugins.var.python.mcp.buffers core.weechat",
        ]
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=log,
            env=dict(
                os.environ, WEECHAT_MCP_FULL_CONTROL="1", WEECHAT_MCP_LOOPBACK_PORT="0"
            ),
        )
        try:
            portfile = home / "profile/mcp/loopback-port"
            deadline = time.monotonic() + 15
            while (
                not portfile.exists()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            if not portfile.exists():
                log.seek(0)
                raise RuntimeError(log.read())
            connection = socket.create_connection(
                ("127.0.0.1", int(portfile.read_text())), timeout=10
            )
            wire = connection.makefile("rwb")

            def request(operation, **kwargs):
                wire.write(
                    (
                        json.dumps(dict(id=1, operation=operation, **kwargs)) + "\n"
                    ).encode()
                )
                wire.flush()
                result = json.loads(wire.readline())
                while "event" in result:
                    result = json.loads(wire.readline())
                if "error" in result:
                    raise RuntimeError(result["error"])
                return result["result"]

            manifest = request("call", name="weechat_command_manifest", arguments={})
            tools = request("tools")
            generated = [t for t in tools if t["name"].startswith("weechat_cmd_")]
            assert len(generated) == manifest["coverage"]["total"]
            from jsonschema import Draft202012Validator

            for tool in generated:
                Draft202012Validator.check_schema(tool["inputSchema"])
            # Only read-only safe fixture commands. Local acceptance is the stopping point.
            accepted = []
            for plugin, name, raw in [
                ("core", "help", "help"),
                ("irc", "server", "list"),
            ]:
                item = next(
                    c
                    for c in manifest["commands"]
                    if (c["plugin"], c["name"]) == (plugin, name)
                )
                arguments = {
                    "buffer": "core.weechat",
                    "registry_revision": item["revision"],
                    "syntax": "raw",
                    "params": {"arguments": raw},
                }
                result = request("call", name=item["tool_name"], arguments=arguments)
                assert result["dispatched"]
                accepted.append(result)
            from client import Client

            client = Client(int(portfile.read_text()))
            try:
                discovered = client.tools()["tools"]
                assert len(discovered) == len(tools)
                item = next(
                    c
                    for c in manifest["commands"]
                    if c["plugin"] == "irc" and c["name"] == "server"
                )
                variant = next(
                    v
                    for v in item["variants"]
                    if v["syntax"].startswith("list|listfull")
                )
                tool = next(t for t in discovered if t["name"] == item["tool_name"])
                for syntax, params in [
                    (variant["id"], {"token_1": "list"}),
                    ("raw", {"arguments": "list"}),
                ]:
                    arguments = {
                        "buffer": "core.weechat",
                        "registry_revision": item["revision"],
                        "syntax": syntax,
                        "params": params,
                    }
                    branch = next(
                        b
                        for b in tool["inputSchema"]["oneOf"]
                        if b["properties"]["syntax"]["const"] == syntax
                    )
                    # Model hosts that expose an alternative on its own and only
                    # forward declared fields, rather than the root's siblings.
                    forwarded = {
                        k: v for k, v in arguments.items() if k in branch["properties"]
                    }
                    assert forwarded == arguments
                    Draft202012Validator(branch).validate(forwarded)
                    result = client.tool(item["tool_name"], forwarded)
                    assert not result.get("isError"), result
                    assert json.loads(result["content"][0]["text"])["dispatched"]
                    accepted.append(
                        {
                            "transport": "MCP stdio",
                            "command": "irc/server",
                            "syntax": syntax,
                            "dispatched": True,
                        }
                    )
            finally:
                client.close()
            snapshot = request("history", uri="weechat://buffer/core.weechat")
            messages = [entry["message"] for entry in snapshot["lines"]]
            assert any("No server" in message for message in messages), messages
            assert any("help" in message.lower() for message in messages), messages
            accepted.append({"fixture_output_verified": True, "messages": messages})
            pathlib.Path(args.report).write_text(
                json.dumps({"manifest": manifest, "accepted": accepted}, indent=2)
                + "\n"
            )
            print(json.dumps({"coverage": manifest["coverage"], "accepted": accepted}))
            wire.close()
            connection.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
            log.close()


if __name__ == "__main__":
    main()
