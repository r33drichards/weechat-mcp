# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual modern WeeChat mixed-trust VM checks, using the existing private IPC."""

import argparse
import json
import socket
import time
from pathlib import Path


def eventually(predicate, description, seconds=30):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (OSError, ValueError, KeyError) as error:
            last = error
        time.sleep(0.1)
    raise AssertionError(f"Timed out: {description}; last error: {last}")


def exchange(path, request):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        connection.sendall(json.dumps(request).encode() + b"\n")
        incoming = b""
        while True:
            chunk = connection.recv(1024 * 1024)
            if not chunk:
                raise ValueError("Socket closed before response")
            incoming += chunk
            while b"\n" in incoming:
                line, incoming = incoming.split(b"\n", 1)
                response = json.loads(line)
                # Native resource events can interleave with a call response.
                if "id" not in request or response.get("id") == request["id"]:
                    return response


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--http-state", required=True, type=Path)
    args = parser.parse_args()
    ipc = args.home / "mcp" / "bridge.sock"

    def control(op, **kwargs):
        return exchange(args.control, {"op": op, **kwargs})

    def call(operation, arguments=None, error=False):
        request = {
            "id": 1,
            "operation": "call",
            "name": "weechat_managed_script_" + operation,
            "arguments": arguments or {},
        }
        deadline = time.monotonic() + 5
        while True:
            response = exchange(ipc, request)
            # This explicit pre-admission result means no candidate was run.
            # Never retry a success, another error, or an uncertain response.
            if "candidate_rate_limited" not in str(response.get("error", "")):
                break
            if time.monotonic() >= deadline:
                raise AssertionError("candidate admission stayed rate-limited")
            time.sleep(0.3)  # Native minimum is 250 ms; keep the same snapshot.
        if error:
            assert "error" in response, response
            return response
        assert "result" in response, response
        return response["result"]

    def wait_message(text, after=0, seconds=30):
        try:
            return eventually(
                lambda: any(
                    item == {"target": "#test", "text": text}
                    for item in control("status")["messages"][after:]
                ),
                f"native IRC reply {text!r}",
                seconds,
            )
        except AssertionError as error:
            # These bounded records contain only this isolated test's synthetic
            # data. Preserve exact success/error assertions while making a DNS,
            # transport, callback, or generation failure distinguishable in CI.
            details = {
                "received": control("status")["messages"][after:][-12:],
                "http_hits": json.loads(args.http_state.read_text())[-12:],
                "managed": call("status")["active"],
            }
            raise AssertionError(f"{error}; diagnostics: {details!r}") from error

    def send(text, expected):
        index = len(control("status")["messages"])
        control("send", text=text)
        wait_message(expected, index)
        time.sleep(0.3)

    def write(source, previous="", name="example"):
        result = call(
            "write", {"name": name, "source": source, "expected_digest": previous}
        )
        assert result["activated"] is False, result
        return result["digest"]

    def activate(digest, name="example"):
        arguments = {"name": name, "digest": digest}
        validated = call("validate", arguments)
        assert validated["activated"] is False, validated
        activated = call("activate", arguments)
        assert activated["activated"] is True, activated
        return activated

    def no_forbidden_messages():
        messages = control("status")["messages"]
        assert not any(item["text"].startswith("forbidden-") for item in messages), (
            messages
        )

    eventually(lambda: control("status")["joined"], "actual WeeChat IRC join")
    send("trusted-ping", "trusted:pong")
    send("trusted-process", "trusted:process:trusted-process-ok")
    initial = call("status")
    assert initial["active"] == [], initial
    # Read-only status proves a real callback without logging, IRC or HTTP effects.
    probe = write("setTimeout(()=>{},100);weechat.onMessage(()=>{});")
    call("validate", {"name": "example", "digest": probe})
    time.sleep(0.3)
    assert call("status")["active"] == []
    before_probe = list(control("status")["messages"])
    call("activate", {"name": "example", "digest": probe})
    eventually(
        lambda: call("status")["active"][0]["timers"]["callbacks_completed"] == 1,
        "effect-free timer callback reported through status",
    )
    probe_status = call("status")["active"][0]
    assert probe_status["timers"] == {
        "live": 0,
        "callbacks_started": 1,
        "callbacks_completed": 1,
        "callbacks_failed": 0,
    }, probe_status
    # Rejected candidates retain the exact generation and completed counters.
    rejected = write("setTimeout(()=>{},100);throw Error('reject');", probe)
    call("validate", {"name": "example", "digest": rejected}, error=True)
    call("activate", {"name": "example", "digest": rejected}, error=True)
    assert call("status")["active"] == [probe_status]

    probe = write(
        "const t=setTimeout(()=>{throw Error('canceled');},100);"
        "clearTimeout(t);weechat.onMessage(()=>{});",
        rejected,
    )
    activate(probe)
    time.sleep(0.3)
    canceled_status = call("status")["active"][0]
    empty_counts = {
        "live": 0,
        "callbacks_started": 0,
        "callbacks_completed": 0,
        "callbacks_failed": 0,
    }
    assert canceled_status["generation"] != probe_status["generation"]
    assert canceled_status["timers"] == empty_counts, canceled_status

    probe = write(
        "let n=0;const t=setInterval(()=>{if(++n===2)clearInterval(t);},100);"
        "weechat.onMessage(()=>{});",
        probe,
    )
    activate(probe)
    eventually(
        lambda: call("status")["active"][0]["timers"]["callbacks_completed"] == 2,
        "effect-free self-canceled interval completed twice",
    )
    interval_status = call("status")["active"][0]
    assert interval_status["timers"] == {
        "live": 0,
        "callbacks_started": 2,
        "callbacks_completed": 2,
        "callbacks_failed": 0,
    }, interval_status
    time.sleep(0.3)
    assert call("status")["active"] == [interval_status]

    probe = write(
        "setTimeout(()=>{throw Error('callback');},100);weechat.onMessage(()=>{});",
        probe,
    )
    activate(probe)
    eventually(
        lambda: call("status")["active"][0]["timers"]["callbacks_failed"] == 1,
        "thrown callback counted as failed, not completed",
    )
    failure_status = call("status")["active"][0]
    assert (
        failure_status["active"] is True
    )  # Ordinary exceptions do not retire the generation.
    assert failure_status["timers"] == {
        "live": 0,
        "callbacks_started": 1,
        "callbacks_completed": 0,
        "callbacks_failed": 1,
    }, failure_status

    # Replace a live timer before its deadline; old hooks cannot increment the new generation.
    probe = write("setTimeout(()=>{},2000);weechat.onMessage(()=>{});", probe)
    activate(probe)
    stale_status = call("status")["active"][0]
    assert stale_status["timers"] == {**empty_counts, "live": 1}, stale_status
    probe = write("weechat.onMessage(()=>{});", probe)
    activate(probe)
    probe_status = call("status")["active"][0]
    assert probe_status["generation"] != stale_status["generation"]
    time.sleep(2.1)
    assert call("status")["active"] == [probe_status]
    assert probe_status["timers"] == empty_counts, probe_status
    assert control("status")["messages"] == before_probe
    source = args.source.read_text()
    digest = write(source, probe)
    read = call("read", {"name": "example"})
    assert read["source"] == source and read["digest"] == digest, read
    assert call("status")["active"] == [probe_status]  # Writing never activates code.
    activate(digest)
    assert call("status")["active"][0]["timers"]["callbacks_started"] == 0
    send("ping", "v1:pong")
    send("deny", "denied:7")
    send("slash", "/exec /bin/true")
    send("slash-join", "/JOIN #not-joined")
    send("double-slash", "//literal")
    assert "#not-joined" not in control("status")["channels"]
    send("literal", "literal:${process.env.HOME};weechat.command('/quit')")

    # Actual native timer delivery is serialized through the managed dispatcher.
    # Allow the global send budget to recover before counting asynchronous replies.
    time.sleep(1.1)
    send("timer-once", "timer:once")
    time.sleep(0.6)
    assert (
        sum(item["text"] == "timer:once" for item in control("status")["messages"]) == 1
    )
    index = len(control("status")["messages"])
    control("send", text="timer-interval")
    wait_message("timer:interval:1", index)
    wait_message("timer:interval:2", index)
    time.sleep(1.1)
    assert [
        item["text"]
        for item in control("status")["messages"][index:]
        if item["text"].startswith("timer:interval:")
    ] == ["timer:interval:1", "timer:interval:2"]
    send("timer-cancel", "timer:canceled")
    time.sleep(0.7)
    send("timer-policy", "timer:policy-checked")
    no_forbidden_messages()

    # Validation may create timer declarations but must never arm their hooks.
    staged_timer = write(
        'setTimeout(() => weechat.send("irc.managed.#test", "timer:startup"), 100);\n'
        + source,
        digest,
    )
    index = len(control("status")["messages"])
    call("validate", {"name": "example", "digest": staged_timer})
    time.sleep(0.6)
    assert not any(
        item["text"] == "timer:startup"
        for item in control("status")["messages"][index:]
    )
    call("activate", {"name": "example", "digest": staged_timer})
    wait_message("timer:startup", index)
    time.sleep(0.6)
    assert (
        sum(
            item["text"] == "timer:startup"
            for item in control("status")["messages"][index:]
        )
        == 1
    )
    digest = write(source, staged_timer)
    activate(digest)

    # Named management never grants filesystem/policy-administration authority.
    for name in ("../policy", "/etc/passwd", "example.js", "..", "policy/data"):
        call("read", {"name": name}, error=True)
    call(
        "write",
        {"name": "example", "source": source, "expected_digest": "0" * 64},
        error=True,
    )
    control("send", text="scope")
    control("send", text="outside-trigger", target="#outside")
    time.sleep(0.5)
    no_forbidden_messages()

    send("private-http", "http-error:invalid_http_request:not_sent")
    send("policy-http", "http-error:policy_denied:not_sent")
    send("dotpath", "http-error:invalid_http_request:not_sent")
    send("encoded-dotpath", "http-error:invalid_http_request:not_sent")
    assert not any(
        item["path"] == "/policy-denied"
        for item in json.loads(args.http_state.read_text())
    )

    write(
        'weechat.command("", "/command irc /msg -server managed #test forbidden-promotion");',
        name="escape",
    )
    send("trusted-promote", "trusted:promote-attempted")
    no_forbidden_messages()

    for method in ("GET", "POST", "PUT", "DELETE"):
        body = "payload" if method != "GET" else ""
        send("http-" + method.lower(), f"http:200:{method}:{body}")
    send("redirect", "http:302:")
    send("large", "http-error:transport_failed:failed")
    hits = json.loads(args.http_state.read_text())
    assert not any(item["trap"] for item in hits), hits
    assert not any("/admin" in item["path"] for item in hits), hits
    assert all(item["host"] == "fixture.test:18080" for item in hits), hits
    assert [item["method"] for item in hits if item["path"] == "/echo"] == [
        "GET",
        "POST",
        "PUT",
        "DELETE",
    ], hits
    send("tls", "http:200:GET:")
    send("tls-wrong-host", "http-error:transport_failed:failed")
    tls_hits = [
        item
        for item in json.loads(args.http_state.read_text())
        if item["path"] == "/tls"
    ]
    assert len(tls_hits) == 1 and tls_hits[0]["host"] == "fixture.test:18443", tls_hits

    index = len(control("status")["messages"])
    control("send", text="slow")
    eventually(
        lambda: any(
            item["path"] == "/slow" for item in json.loads(args.http_state.read_text())
        ),
        "native slow HTTP started",
    )
    control("send", text="trusted-ping")
    wait_message("trusted:pong", index, seconds=2)
    assert not any(
        item["text"] == "http:200:slow"
        for item in control("status")["messages"][index:]
    )
    wait_message("http:200:slow", index)

    # A failed candidate and stale digest never replace the last active source.
    invalid = write("weechat.onMessage( {", digest)
    call("validate", {"name": "example", "digest": invalid}, error=True)
    call("activate", {"name": "example", "digest": digest}, error=True)
    send("ping", "v1:pong")
    staged = write(
        'weechat.send("irc.managed.#test", "forbidden-staging");\n' + source, invalid
    )
    call("validate", {"name": "example", "digest": staged}, error=True)
    send("ping", "v1:pong")
    replacement = write(source.replace("v1:pong", "v2:pong"), staged)
    activate(replacement)
    send("ping", "v2:pong")

    # Pending effects from an old generation cannot acquire a new one's authority.
    index = len(control("status")["messages"])
    before = sum(
        item["path"] == "/slow" for item in json.loads(args.http_state.read_text())
    )
    send("timer-stale", "timer:armed")
    control("send", text="slow")
    eventually(
        lambda: (
            sum(
                item["path"] == "/slow"
                for item in json.loads(args.http_state.read_text())
            )
            > before
        ),
        "old generation HTTP started",
    )
    replacement = write(source.replace("v1:pong", "v3:pong"), replacement)
    activate(replacement)
    send("ping", "v3:pong")
    time.sleep(3.5)
    assert not any(
        item["text"] == "http:200:slow"
        for item in control("status")["messages"][index:]
    )

    old_connections = control("status")["connections"]
    control("disconnect")
    eventually(
        lambda: (
            (state := control("status"))["connections"] > old_connections
            and state["joined"]
        ),
        "native IRC reconnect",
    )
    send("ping", "v3:pong")
    send("trusted-process", "trusted:process:trusted-process-ok")

    # Runaway managed execution/microtasks retire only that managed generation.
    for trigger in ("loop", "microtask-loop", "timer-loop", "timer-microtask-loop"):
        control("send", text=trigger)
        eventually(
            lambda: any(
                item["name"] == "example" and not item["active"]
                for item in call("status")["active"]
            ),
            f"managed generation retired after {trigger}",
        )
        if trigger.startswith("timer-"):
            retired = next(
                item for item in call("status")["active"] if item["name"] == "example"
            )
            assert retired["timers"]["callbacks_failed"] >= 1, retired
        send("trusted-ping", "trusted:pong")
        send("trusted-process", "trusted:process:trusted-process-ok")
        replacement = write(source.replace("v1:pong", "recovered:pong"), replacement)
        activate(replacement)
        send("ping", "recovered:pong")
    no_forbidden_messages()
    print(
        "PASS: actual mixed-trust V8/IRC/native timers/HTTP/TLS, named hot reload, independent policy, trusted compatibility"
    )


if __name__ == "__main__":
    main()
