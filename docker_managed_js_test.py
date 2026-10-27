# SPDX-License-Identifier: GPL-3.0-or-later
"""Build/load the actual Nix Docker image, then run an offline mixed-trust smoke.

    uv run python docker_managed_js_test.py
    # Or load a previously Nix-built image:
    nix build .#managed-js-image -o result-image
    docker load --input result-image
    uv run python docker_managed_js_test.py --skip-build
    # Compatibility route only (Nix closure inside the existing apt image):
    uv run python docker_managed_js_test.py --dockerfile

Disposable containers use --network none. The actual Nix-built WeeChat/Python/
JavaScript plugins exercise independent embedded policy and mixed-trust behavior;
no public IRC or HTTP endpoint is contacted. NixOS VM tests cover network policy.
"""

import argparse
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
NIX_IMAGE = "weechat-mcp:managed-js-nix"

PROBE = (
    r"""
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
import weechat

marker = Path(os.environ["SMOKE_MARKER"])
messages = []

def smoke_print(data, buffer, date, tags, displayed, highlight, prefix, message):
    messages.append(message)
    return weechat.WEECHAT_RC_OK

def finish(result):
    result["messages"] = messages
    marker.write_text(json.dumps(result))
    weechat.command("", "/quit")
    return weechat.WEECHAT_RC_OK

def smoke_done(data, remaining):
    return finish({"ok": True, "trusted_exec": True, "managed_isolation": True})

def operation(action, source):
    values = {"name": "smoke", "source": source,
        "digest": hashlib.sha256(source.encode()).hexdigest()}
    deadline = time.monotonic() + 3
    while True:
        result = weechat.info_get_hashtable("javascript_managed_" + action, values)
        # This one native error explicitly means no candidate/isolate was made;
        # never retry uncertain activation or any other policy/runtime failure.
        if result.get("error") != "candidate_rate_limited" or time.monotonic() >= deadline:
            return result
        time.sleep(0.1)

weechat.register("managed_image_probe", "weechat-mcp", "1", "GPL3", "offline image probe", "", "")
try:
    assert weechat.hook_print("", "", "", 1, "smoke_print", "")
    weechat.command("", "/javascript load " + os.environ["SMOKE_TRUSTED"])
    assert Path(os.environ["SMOKE_SOCKET"]).is_socket(), "managed MCP did not expose IPC"
    # Trusted Python is intentionally unchanged: no process-global noexec filter.
    subprocess.run(["/bin/true"], check=True)
    try:
        with open(os.environ["WEECHAT_MANAGED_DATA"], "a"):
            raise AssertionError("operator policy data was writable by service")
    except PermissionError:
        pass
    source = """
    + repr("""
        if (typeof require !== "undefined" || typeof process !== "undefined") throw Error("node globals");
        for (const key of ["command", "hook_process", "config_set", "register", "readFile", "setPolicy"]) {
            if (typeof weechat[key] !== "undefined") throw Error("legacy surface");
        }
        weechat.onMessage(function (event) { weechat.log(event.text); });
    """)
    + r"""
    valid = operation("validate", source)
    assert valid.get("ok") == "1", valid
    before = weechat.info_get_hashtable("javascript_managed_status", {})
    assert json.loads(before["plugins"]) == [], before
    loaded = operation("activate", source)
    assert loaded.get("ok") == "1", loaded
    current = weechat.info_get_hashtable("javascript_managed_status", {})
    assert len(json.loads(current["plugins"])) == 1, current
    for invalid in ("while (true) {}", "weechat.command('', '/quit')", "weechat.log('FORBIDDEN_STAGING_EFFECT')"):
        rejected = operation("activate", invalid)
        assert rejected.get("ok") != "1", rejected
        assert weechat.info_get_hashtable("javascript_managed_status", {}) == current
    # Managed-writable files cannot be promoted through the trusted JS loader.
    forbidden = Path(os.environ["WEECHAT_MANAGED_ROOT"]) / "promote.js"
    forbidden.write_text("weechat.register('promoted','test','1','GPL3','','',''); weechat.print('', 'FORBIDDEN_TRUST_PROMOTION');")
    weechat.command("", "/javascript load " + str(forbidden))
    for buffer, text in (("irc.docker.#wchat", "MANAGED_EVENT_OK"), ("irc.docker.#outside", "FORBIDDEN_EVENT")):
        queued = weechat.info_get_hashtable("javascript_managed_message", {"buffer": buffer, "nick": "fixture", "text": text})
        assert queued.get("ok") == "1", queued
    assert weechat.hook_timer(500, 0, 1, "smoke_done", "")
except Exception as error:
    finish({"error": repr(error)})
"""
)

HARNESS = r"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, "/app")
from container_entrypoint import MANAGED_BINARY, MANAGED_PLUGIN_DIR, weechat_command

assert os.getuid() == 0
with tempfile.TemporaryDirectory(prefix="managed-js-image-") as directory:
    work = Path(directory)
    work.chmod(0o755)
    root = work / "managed"
    profile = work / "profile"
    failed_profile = work / "failed-profile"
    for path in (root, profile, failed_profile):
        path.mkdir(mode=0o700)
        os.chown(path, 10001, 10001)
    data = work / "data.json"
    data.write_text(json.dumps({"managed": {"buffers": ["irc.docker.#wchat"], "http_methods": [], "http_hosts": [], "log": True}}))
    data.chmod(0o444)
    probe = work / "probe.py"
    probe.write_text(PROBE)
    probe.chmod(0o444)
    trusted = work / "trusted.js"
    trusted.write_text("if (weechat.register('trusted_image_probe','test','1','GPL3','','','')) { weechat.print('', 'TRUSTED_JS_OK'); }")
    trusted.chmod(0o444)
    marker = profile / "result.json"
    env = dict(os.environ,
        HOME=str(profile), WEECHAT_MCP_MANAGED_JS="1", WEECHAT_MCP_FULL_CONTROL="0",
        WEECHAT_MANAGED_ROOT=str(root), WEECHAT_MANAGED_DATA=str(data),
        SMOKE_SOCKET=str(profile / "mcp" / "bridge.sock"), SMOKE_MARKER=str(marker), SMOKE_TRUSTED=str(trusted))
    commands = ["/plugin load " + MANAGED_PLUGIN_DIR + "/" + name + ".so" for name in ("irc", "python", "javascript")]
    commands += ["/python load /app/weechat_mcp.py", "/python load " + str(probe)]
    # Keep the root harness for temporary-directory cleanup; only WeeChat and its
    # trusted subprocess probe run as the actual unprivileged service identity.
    result = subprocess.run(weechat_command(profile, commands, managed=True),
        env=env, user=10001, group=10001, extra_groups=[],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=20)
    assert result.returncode == 0, result.stdout
    assert marker.exists(), result.stdout
    status = json.loads(marker.read_text())
    assert status.get("ok"), (status, result.stdout)
    messages = "\n".join(status.pop("messages"))
    assert "TRUSTED_JS_OK" in messages, (messages, result.stdout)
    assert "MANAGED_EVENT_OK" in messages, (messages, result.stdout)
    for forbidden in ("FORBIDDEN_STAGING_EFFECT", "FORBIDDEN_TRUST_PROMOTION", "FORBIDDEN_EVENT"):
        assert forbidden not in messages, messages
    print(json.dumps(status, sort_keys=True))
    # Missing operator policy must stop the actual plugin before MCP readiness.
    env.update(HOME=str(failed_profile), WEECHAT_HOME=str(failed_profile),
        WEECHAT_MANAGED_DATA=str(work / "missing.json"),
        IRC_HOST="127.0.0.1", IRC_PORT="1", IRC_TLS="false", MCP_TRANSPORT="stdio")
    failed = subprocess.run(["/app/.venv/bin/python", "/app/container_entrypoint.py"],
        env=env, user=10001, group=10001, extra_groups=[],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=30)
    assert failed.returncode == 1, (failed.returncode, failed.stdout)
    assert any(message in failed.stdout for message in (
        "Timed out waiting for WeeChat's plugin socket",
        "WeeChat exited before its plugin became ready")), failed.stdout
    assert not (failed_profile / "mcp" / "bridge.sock").exists(), failed.stdout
    print("Missing policy blocks MCP readiness; supervisor fails and cleans up.")
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=NIX_IMAGE)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument(
        "--dockerfile",
        action="store_true",
        help="Test the compatibility Dockerfile target instead of the actual Nix-built image",
    )
    args = parser.parse_args()
    if not args.skip_build and args.dockerfile:
        subprocess.run(
            ["docker", "build", "--target", "managed-js", "-t", args.image, str(REPO)],
            check=True,
            timeout=3300,
        )
    elif not args.skip_build:
        with tempfile.TemporaryDirectory(prefix="weechat-managed-image-") as directory:
            artifact = Path(directory) / "image"
            subprocess.run(
                [
                    "nix",
                    "--extra-experimental-features",
                    "nix-command flakes",
                    "build",
                    "--no-update-lock-file",
                    ".#managed-js-image",
                    "--out-link",
                    str(artifact),
                ],
                cwd=REPO,
                check=True,
                timeout=3300,
            )
            subprocess.run(
                ["docker", "load", "--input", str(artifact)], check=True, timeout=120
            )
        if args.image != NIX_IMAGE:
            subprocess.run(
                ["docker", "tag", NIX_IMAGE, args.image], check=True, timeout=15
            )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--entrypoint",
            "/app/.venv/bin/python",
            args.image,
            "-",
        ],
        input="PROBE = " + repr(PROBE) + "\n" + HARNESS,
        text=True,
        check=True,
        timeout=60,
    )
    print("Managed-js image smoke passed; trusted plugins remain unrestricted.")


if __name__ == "__main__":
    main()
