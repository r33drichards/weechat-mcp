#!/usr/bin/env python3
"""Render private runtime config and supervise the auth service's two processes."""

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from render_config import render

# Each process receives only the secrets it uses. Never log the environment.
SECRETS = {
    "MCP_AUTH_TOKEN",
    "SHARED_SECRET",
    "COOKIE_SECRET",
    "SIGNING_KEY",
    "IDP_CLIENT_SECRET",
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
    "POMERIUM_CLIENT_SECRET",
}


def child_environment(allowed):
    return {
        key: value
        for key, value in os.environ.items()
        if key not in SECRETS or key in allowed
    }


def stop_all(children):
    # Signal every service before waiting so neither is left serving alone.
    for child in children:
        if child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + 10
    for child in children:
        try:
            child.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def supervise(commands):
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    previous = {
        sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    children = []
    try:
        for command, env in commands:
            if stopping:
                return 0
            children.append(
                subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL)
            )
        while not stopping:
            for child in children:
                if child.poll() is not None:
                    print(
                        "A required service exited; stopping the container.",
                        file=sys.stderr,
                    )
                    return 1  # Even an unexpected zero exit loses a required service.
            time.sleep(0.05)
        return 0
    finally:
        stop_all(children)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("auth", "pomerium"):
        raise ValueError("Expected service: auth or pomerium")
    service = sys.argv[1]
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="weechat-auth-config-") as directory:
        root = Path(directory)
        render(service, os.environ, root)
        if service == "pomerium":
            return supervise(
                [
                    (
                        ["pomerium", "--config", str(root / "pomerium.yaml")],
                        child_environment(
                            {
                                "SHARED_SECRET",
                                "COOKIE_SECRET",
                                "SIGNING_KEY",
                                "IDP_CLIENT_SECRET",
                            }
                        ),
                    )
                ]
            )
        # Validate before either child starts. No config dump, which can leak secrets.
        router_env = child_environment(set())
        subprocess.run(
            ["nginx", "-e", "stderr", "-t", "-c", str(root / "router.conf")],
            env=router_env,
            check=True,
        )
        return supervise(
            [
                (
                    ["dex", "serve", str(root / "dex.yaml")],
                    child_environment(
                        {
                            "GITHUB_CLIENT_ID",
                            "GITHUB_CLIENT_SECRET",
                            "POMERIUM_CLIENT_SECRET",
                        }
                    ),
                ),
                (
                    [
                        "nginx",
                        "-e",
                        "stderr",
                        "-c",
                        str(root / "router.conf"),
                        "-g",
                        "daemon off;",
                    ],
                    router_env,
                ),
            ]
        )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError):
        # Upstream parsing errors can contain credentials: don't serialize exceptions.
        print(
            "Service startup failed; check required inputs and private configuration.",
            file=sys.stderr,
        )
        sys.exit(1)
