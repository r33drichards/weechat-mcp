# SPDX-License-Identifier: GPL-3.0-or-later
"""Real native buffer/IPC test. Disposable WeeChat; no IRC/network connection."""

import argparse
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path


def wait_for(predicate, timeout=10):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Isolated native buffer test timed out")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weechat-root", required=True)
    args = parser.parse_args()
    root = Path(args.weechat_root).resolve()
    repo = Path(__file__).resolve().parent
    name = "python.native_fixture"
    with tempfile.TemporaryDirectory(prefix="native-buffer-live-") as temporary:
        work = Path(temporary)
        home = work / "home"
        control = work / "control.json"
        response = control.with_suffix(".result")
        endpoint = home / "mcp" / "loopback-port"
        env = dict(
            os.environ,
            WEECHAT_MCP_LOOPBACK_PORT="0",
            NATIVE_TEST_CONTROL=str(control),
            NATIVE_TEST_REPO=str(repo),
        )
        command = [
            str(root / "usr/bin/weechat-headless"),
            "-d",
            str(home),
            "-p",
            "-a",
            "--stdout",
        ]
        for action in (
            "/plugin load "
            + str(root / "usr/lib/x86_64-linux-gnu/weechat/plugins/python.so"),
            # Crucially, these 650 lines are printed BEFORE loading mcp.
            "/python load " + str(repo / "fixtures/native_buffers.py"),
            "/python load " + str(repo / "weechat_mcp.py"),
        ):
            command += ["-r", action]

        def call(**request):
            with socket.create_connection(
                ("127.0.0.1", int(endpoint.read_text())), timeout=5
            ) as connection:
                connection.sendall(json.dumps({"id": 1, **request}).encode() + b"\n")
                result = json.loads(connection.makefile("rb").readline())
            if "error" in result:
                raise ValueError(result["error"])
            return result["result"]

        def read(**kwargs):
            return call(
                operation="call",
                name="weechat_buffers",
                arguments={"buffer": name, **kwargs},
            )

        counter = 0

        def fixture(action):
            nonlocal counter
            counter += 1
            control.write_text(json.dumps({"id": counter, "action": action}))
            result = wait_for(
                lambda: (
                    json.loads(response.read_text())
                    if response.exists()
                    and json.loads(response.read_text())["id"] == counter
                    else None
                )
            )
            assert result.get("ok"), result

        def reject_cursor(cursor):
            try:
                read(cursor=cursor)
            except ValueError:
                return
            raise AssertionError("Stale cursor unexpectedly accepted")

        log = work / "weechat.log"
        with log.open("w") as output:
            process = subprocess.Popen(
                command, env=env, stdout=output, stderr=subprocess.STDOUT
            )
            try:
                wait_for(endpoint.exists)
                tools = call(operation="tools")
                assert not any(
                    tool["name"] == "weechat_read_recent_messages" for tool in tools
                )
                page = read(limit=500)
                assert page["available"] == 650, page
                assert page["lines"][0]["message"] == "old-150"
                assert page["lines"][-1]["message"] == "old-649"
                older = read(limit=500, cursor=page["next_cursor"])
                assert [line["message"] for line in older["lines"]] == [
                    "old-" + str(i) for i in range(150)
                ]
                assert older["next_cursor"] is None
                print("NATIVE_PRE_PLUGIN_650_LINES_AND_PAGINATION_OK", flush=True)

                fixture("append")
                fixture("filter")
                recent = read(limit=1)["lines"][0]
                assert recent["message"] == "new-backdated" and recent["date"] == 1
                assert recent["displayed"] is False and "hidden" in recent["tags"]
                assert "date_printed" in recent
                print("NATIVE_ORDER_FILTER_FLAGS_AND_METADATA_OK", flush=True)

                cursor = read(limit=1)["next_cursor"]
                data_epoch = read()["data_epoch"]
                previous_endpoint = endpoint.stat().st_mtime_ns
                fixture("reload")
                wait_for(lambda: endpoint.stat().st_mtime_ns != previous_endpoint)
                reject_cursor(cursor)
                assert read()["available"] == 651
                reloaded_epoch = read()["data_epoch"]
                if data_epoch.startswith("native-buffer:"):
                    assert reloaded_epoch == data_epoch
                    print("NATIVE_DATA_IDENTITY_SURVIVES_PLUGIN_RELOAD_OK", flush=True)
                else:
                    # WeeChat 3.8–4.2 has no stable native buffer ID.
                    assert reloaded_epoch != data_epoch
                    print(
                        "NATIVE_LEGACY_IDENTITY_CHANGE_REQUIRES_RECONCILIATION_OK",
                        flush=True,
                    )
                print(
                    "NATIVE_PLUGIN_RELOAD_PRESERVES_LINES_EXPIRES_CURSOR_OK", flush=True
                )

                cursor = read(limit=1)["next_cursor"]
                fixture("clear")
                assert read()["lines"] == []
                reject_cursor(cursor)
                fixture("prune")
                pruned = read(limit=500)
                assert pruned["available"] == 100, pruned
                assert pruned["lines"][0]["message"] == "prune-50"
                assert pruned["lines"][-1]["message"] == "prune-149"
                print("NATIVE_CLEAR_AND_WEECHAT_RETENTION_LIMIT_OK", flush=True)
            except Exception:
                print(log.read_text()[-6000:])
                raise
            finally:
                process.terminate()
                process.wait(timeout=5)

        # A process restart does not make the MCP adapter a persistent log store.
        endpoint.unlink(missing_ok=True)
        env["NATIVE_TEST_SEED"] = "0"
        with log.open("w") as output:
            process = subprocess.Popen(
                command, env=env, stdout=output, stderr=subprocess.STDOUT
            )
            try:
                wait_for(endpoint.exists)
                restarted = read()
                assert restarted["lines"] == []
                if data_epoch.startswith("native-buffer:"):
                    assert (
                        restarted["data_epoch"].split(":")[:3]
                        != data_epoch.split(":")[:3]
                    )
                else:
                    assert restarted["data_epoch"] != data_epoch
                print("NATIVE_PROCESS_RESTART_NEW_DATA_IDENTITY_OK", flush=True)
                print("NATIVE_PROCESS_RESTART_HAS_NO_ADAPTER_HISTORY_OK", flush=True)
            finally:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
