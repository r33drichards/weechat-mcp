# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in real WeeChat + local IRC smoke test; never connects off localhost."""

import argparse
import json
import os
import pathlib
import socket
import subprocess
import tempfile
import time

from mcp.shared.subscriptions import ResourceUpdated

from client import Client
from runner import Runner

parser = argparse.ArgumentParser()
parser.add_argument(
    "--weechat-root", required=True, help="Extracted official Debian package root"
)
parser.add_argument(
    "--miniircd", required=True, help="Path to official miniircd script"
)
args = parser.parse_args()
root = str(pathlib.Path(args.weechat_root).resolve())
repo = str(pathlib.Path(__file__).resolve().parent)
processes = []
logs = []
extra_clients = []
runners = []


def launch(cmd, path, env=None):
    f = open(path, "w")
    logs.append(f)
    p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, env=env)
    processes.append(p)
    return p


try:
    with tempfile.TemporaryDirectory(prefix="weechat-live-") as work:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        ircport = probe.getsockname()[1]
        probe.close()
        launch(
            [
                "python3",
                args.miniircd,
                "--listen",
                "127.0.0.1",
                "--ports",
                str(ircport),
                "--verbose",
            ],
            work + "/irc.log",
        )
        time.sleep(0.3)
        home = work + "/profile"
        env = dict(os.environ, WEECHAT_MCP_LOOPBACK_PORT="0")
        cmd = [root + "/usr/bin/weechat-headless", "-d", home, "-p", "--stdout"]
        commands = [
            "/plugin load " + root + "/usr/lib/x86_64-linux-gnu/weechat/plugins/irc.so",
            "/plugin load "
            + root
            + "/usr/lib/x86_64-linux-gnu/weechat/plugins/python.so",
            "/python load " + repo + "/weechat_mcp.py",
            f"/server add railway 127.0.0.1/{ircport} -notls",
            "/set irc.server.railway.nicks dotmcp",
            "/set irc.server.railway.autojoin #wchat",
            "/connect railway",
        ]
        for c in commands:
            cmd += ["-r", c]
        launch(cmd, work + "/weechat.log", env)
        endpoint = pathlib.Path(home + "/mcp/loopback-port")
        for _ in range(100):
            if endpoint.exists():
                break
            time.sleep(0.1)
        assert endpoint.exists(), pathlib.Path(work + "/weechat.log").read_text()
        m = Client(int(endpoint.read_text()))
        extra_clients.append(m)
        assert m.version == "2026-07-28", m.version
        print("REAL_WEECHAT_MODERN_MCP_2026_07_28_OK", flush=True)
        assert any(t["name"] == "weechat_msg" for t in m.tools()["tools"])
        resources = []
        for _ in range(100):
            resources = m.resources()["resources"]
            if resources:
                break
            time.sleep(0.1)
        assert resources, (
            "No #wchat buffer: " + pathlib.Path(work + "/weechat.log").read_text()
        )
        uri = resources[0]["uri"]
        m.listen(uri)
        observer = socket.create_connection(("127.0.0.1", ircport))
        observer.settimeout(5)
        observer.sendall(
            b"NICK dotobserver\r\nUSER dotobserver 0 * :MCP loopback test\r\n"
        )
        buf = b""
        while b" 001 " not in buf:
            buf += observer.recv(65536)
        observer.sendall(b"JOIN #wchat\r\n")
        buf = b""
        while b" 366 " not in buf:
            buf += observer.recv(65536)
        observer.sendall(b"PRIVMSG #wchat :observer-to-mcp-loopback-test\r\n")
        time.sleep(0.2)
        data = m.snapshot(uri)
        assert "observer-to-mcp-loopback-test" in json.dumps(data), data
        assert any(isinstance(x, ResourceUpdated) for x in m.notifications), (
            m.notifications
        )
        print("REAL_IRC_RECEIVE_AND_MCP_SUBSCRIPTION_OK", flush=True)
        out = m.tool(
            "weechat_msg",
            {
                "buffer": "irc.railway.#wchat",
                "arguments": "mcp-to-observer-loopback-test",
            },
        )
        assert not out.get("isError"), out
        buf = b""
        while b"mcp-to-observer-loopback-test" not in buf:
            buf += observer.recv(65536)
        assert b":dotmcp!" in buf, buf
        print("REAL_MCP_SEND_RECEIVED_BY_SECOND_IRC_CLIENT_OK", flush=True)
        # A second real WeeChat profile owns a distinct IRC identity and MCP connection.
        home2 = work + "/profile2"
        cmd2 = list(cmd)
        cmd2[cmd2.index(home)] = home2
        cmd2 = [
            x.replace(
                "/set irc.server.railway.nicks dotmcp",
                "/set irc.server.railway.nicks dotmcp2",
            )
            for x in cmd2
        ]
        launch(cmd2, work + "/weechat2.log", env)
        endpoint2 = pathlib.Path(home2 + "/mcp/loopback-port")
        for _ in range(100):
            if endpoint2.exists():
                break
            time.sleep(0.1)
        assert endpoint2.exists(), "Second WeeChat plugin did not start"
        m2 = Client(int(endpoint2.read_text()))
        extra_clients.append(m2)
        for _ in range(100):
            resources2 = m2.resources()["resources"]
            if resources2:
                break
            time.sleep(0.1)
        assert resources2, "Second WeeChat did not join"
        uri2 = resources2[0]["uri"]
        m2.listen(uri2)
        sent = m.tool(
            "weechat_msg",
            {
                "buffer": "irc.railway.#wchat",
                "arguments": "agent-one-to-agent-two-test",
            },
        )
        assert not sent.get("isError"), sent
        for _ in range(50):
            received = m2.snapshot(uri2)
            if "agent-one-to-agent-two-test" in json.dumps(received):
                break
            time.sleep(0.1)
        assert "agent-one-to-agent-two-test" in json.dumps(received)
        sent = m2.tool(
            "weechat_msg",
            {
                "buffer": "irc.railway.#wchat",
                "arguments": "agent-two-reply-to-agent-one-test",
            },
        )
        assert not sent.get("isError"), sent
        for _ in range(50):
            received = m.snapshot(uri)
            if "agent-two-reply-to-agent-one-test" in json.dumps(received):
                break
            time.sleep(0.1)
        assert "agent-two-reply-to-agent-one-test" in json.dumps(received)
        assert "dotmcp2" in json.dumps(received)
        assert any(isinstance(x, ResourceUpdated) for x in m2.notifications)
        print("TWO_DISTINCT_WEECHAT_MCP_IDENTITIES_ROUNDTRIP_OK", flush=True)
        # Separate MCP connections to one WeeChat profile have independent watches.
        m.unlisten(uri)
        m.notifications.clear()
        observer.sendall(b"PRIVMSG #wchat :after-unsubscribe-test\r\n")
        time.sleep(0.2)
        m.snapshot(uri)
        assert not any(isinstance(x, ResourceUpdated) for x in m.notifications)
        m.listen(uri)
        print("RESOURCE_LISTEN_CANCELLATION_OK", flush=True)
        # Deterministic host wakeup experiment: no external model calls or model spend.
        ca = Client(int(endpoint.read_text()))
        cb = Client(int(endpoint2.read_text()))
        extra_clients.extend([ca, cb])
        ra = Runner(work + "/agent-a.sqlite", "dotmcp", max_turns=2)
        rb = Runner(work + "/agent-b.sqlite", "dotmcp2", max_turns=1)
        runners.extend([ra, rb])
        ra.ingest(ca.listen(uri))
        rb.ingest(cb.listen(uri2))
        evidence = []

        def intake():
            for client, runner, resource in [(ca, ra, uri), (cb, rb, uri2)]:
                snapshot = client.updates(resource)
                if snapshot is not None:
                    runner.ingest(snapshot)

        def until(predicate):
            end = time.monotonic() + 8
            while time.monotonic() < end:
                intake()
                if predicate():
                    return
                time.sleep(0.03)
            raise AssertionError(
                "Wakeup condition timed out: " + str((ra.counts(), rb.counts()))
            )

        observer.sendall(
            b"PRIVMSG #wchat :dotmcp: start the local wakeup experiment\r\n"
        )
        until(lambda: ra.counts().get("pending") == 1)
        ta = ra.start_turn()
        assert ta
        question = "dotmcp2: does the local mention-trigger wakeup work?"
        ca.send("irc.railway.#wchat", question)
        ra.finish_turn(ta)
        evidence.append(
            {"speaker": "dotmcp", "text": question, "trigger": ta["message"]["message"]}
        )
        until(lambda: rb.counts().get("pending") == 1)
        tb = rb.start_turn()
        assert tb
        # Disconnect B's MCP transport while its turn is busy. IRC/WeeChat stays joined.
        cb.close()
        extra_clients.remove(cb)
        observer.sendall(b"PRIVMSG #wchat :dotmcp2: queue this while you are busy\r\n")
        time.sleep(0.15)
        rb.close()
        runners.remove(rb)
        rb = Runner(work + "/agent-b.sqlite", "dotmcp2", max_turns=1)
        runners.append(rb)
        cb = Client(int(endpoint2.read_text()))
        extra_clients.append(cb)
        catchup = cb.listen(uri2)
        added = rb.ingest(catchup)
        assert added == 1, (added, catchup)
        assert rb.counts() == {"pending": 1, "running": 1}, rb.counts()
        assert rb.start_turn() is None
        # Replayed/duplicate update notifications reread the same snapshot, never enqueue twice.
        for _ in range(2):
            cb.notifications.append(ResourceUpdated(uri=uri2))
            rb.ingest(cb.updates(uri2))
        assert rb.counts() == {"pending": 1, "running": 1}, rb.counts()
        answer = "dotmcp: yes, the local mention-trigger wakeup works."
        cb.send("irc.railway.#wchat", answer)
        rb.finish_turn(tb)
        evidence.append(
            {"speaker": "dotmcp2", "text": answer, "trigger": tb["message"]["message"]}
        )
        until(lambda: ra.counts().get("pending") == 1)
        done = ra.start_turn()
        assert done
        ra.finish_turn(done)
        assert rb.start_turn() is None, "Turn cap must stop queued messages"
        assert ra.counts() == {"done": 2}, ra.counts()
        assert rb.counts() == {"done": 1, "pending": 1}, rb.counts()
        print(
            "AUTOMATIC_MENTION_WAKEUP_BUSY_QUEUE_RECONNECT_DUPLICATES_CAP_OK",
            flush=True,
        )
        print(
            "WAKEUP_EVIDENCE "
            + json.dumps(
                {
                    "protocol": ca.version,
                    "runtime": "deterministic host, no LLM",
                    "exchange": evidence,
                    "agent_a": ra.counts(),
                    "agent_b": rb.counts(),
                    "reconnect_caught_up": added,
                }
            ),
            flush=True,
        )
        observer.sendall(b"QUIT :test complete\r\n")
        observer.close()
        print("ALL_LIVE_CHECKS_PASS", flush=True)
finally:
    for c in extra_clients:
        c.close()
    for r in runners:
        r.close()
    for p in reversed(processes):
        if p.poll() is None:
            p.terminate()
    for p in processes:
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    for f in logs:
        f.close()
