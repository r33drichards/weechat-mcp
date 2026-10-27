# SPDX-License-Identifier: GPL-3.0-or-later
"""Run an isolated WeeChat and its stdio MCP bridge; stdout is protocol-only."""

import contextlib
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

UID = GID = 10001
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
MANAGED_BINARY = "/opt/weechat-managed/bin/weechat-headless"
MANAGED_PLUGIN_DIR = "/opt/weechat-managed/lib/weechat/plugins"


def managed_settings(env):
    """Validate managed MCP configuration without changing the trusted profile."""
    value = env.get("WEECHAT_MCP_MANAGED_JS", "0")
    if value not in ("0", "1"):
        raise ValueError("WEECHAT_MCP_MANAGED_JS must be 0 or 1")
    if value == "0":
        return None
    # This is the agent MCP boundary, not a global restriction on trusted native
    # plugins or scripts. Full MCP control could otherwise bypass managed policy.
    if env.get("WEECHAT_MCP_FULL_CONTROL") != "0":
        raise ValueError("Managed JavaScript requires WEECHAT_MCP_FULL_CONTROL=0")
    paths = {}
    for name in ("ROOT", "POLICY", "DATA"):
        key = "WEECHAT_MANAGED_" + name
        path = Path(env.get(key, ""))
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError(key + " must be an absolute path")
        paths[name.lower()] = path
    root = paths["root"]
    # Managed source may be edited by MCP, but must never become trusted Python/
    # JavaScript autoload source or an operator-owned policy/data replacement.
    trusted = [
        Path("/app"),
        Path("/usr"),
        Path("/nix"),
        Path("/etc"),
        Path("/opt/weechat-managed"),
    ]
    persistent = home(env)
    if persistent is not None:
        trusted.append(persistent)
    if any(root.is_relative_to(path) or path.is_relative_to(root) for path in trusted):
        raise ValueError(
            "WEECHAT_MANAGED_ROOT must be separate from trusted code/profile directories"
        )
    if any(paths[key].is_relative_to(root) for key in ("policy", "data")):
        raise ValueError("Managed policy and data must be outside WEECHAT_MANAGED_ROOT")
    return paths


def initialize_managed_root(path):
    """Prepare one private source directory, never chown policy or its parents."""
    if path.resolve() != path:
        raise ValueError("WEECHAT_MANAGED_ROOT must not contain symbolic links")
    path.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path, DIRECTORY_FLAGS)
    try:
        info = os.fstat(fd)
        allowed = (0, UID) if os.getuid() == 0 else (os.getuid(),)
        if info.st_uid not in allowed or info.st_mode & 0o022:
            raise ValueError("Unsafe WEECHAT_MANAGED_ROOT ownership or permissions")
        if os.getuid() == 0:
            os.fchown(fd, UID, GID)
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def weechat_command(profile, commands, managed=False):
    """Opt in to the fork binary without altering trusted startup semantics."""
    binary = MANAGED_BINARY if managed else "weechat-headless"
    command = [binary, "-d", str(profile), "-p", "--stdout"]
    for item in commands:
        command += ["-r", item]
    return command


def configuration(env, first_boot=True):
    """Validate all command interpolation, including WeeChat expression syntax.

    With a persistent WEECHAT_HOME, environment-derived settings (server, nick,
    channels, exposed buffers) are only applied on the first boot; afterwards the
    saved configuration is authoritative so runtime `/set` edits survive restarts.
    """
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
    setup = [
        "/set plugins.var.python.mcp.buffers "
        + ",".join("irc.docker." + c for c in channels),
        f"/server add docker {host}/{port} -{'tls' if tls == 'true' else 'notls'}",
        "/set irc.server.docker.tls_verify on",
        f"/set irc.server.docker.nicks {nick}",
        "/set irc.server.docker.autojoin " + ",".join(channels),
    ]
    plugins = (
        [
            "/plugin load " + MANAGED_PLUGIN_DIR + "/" + name + ".so"
            for name in ("irc", "python", "javascript")
        ]
        if env.get("WEECHAT_MCP_MANAGED_JS") == "1"
        else ["/plugin load irc", "/plugin load python"]
    )
    return [
        *plugins,
        "/python load " + str(Path(__file__).with_name("weechat_mcp.py")),
        *(setup if first_boot else []),
        "/connect docker",
    ]


def home(env):
    """Return the persistent WeeChat home from WEECHAT_HOME, or None for a temp dir."""
    value = env.get("WEECHAT_HOME", "")
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError("WEECHAT_HOME must be an absolute path")
    return path


def drop_privileges():
    """Permanently become the service user when started as root (no-op otherwise)."""
    if os.getuid() != 0:
        return
    os.setgroups([])
    os.setresgid(GID, GID, GID)
    os.setresuid(UID, UID, UID)
    if (
        os.getresuid() != (UID, UID, UID)
        or os.getresgid() != (GID, GID, GID)
        or os.getgroups()
    ):
        raise ValueError("Privilege drop failed")


def initialize_home(path, drop=drop_privileges):
    """Make the persistent home private to the service user, dropping root if held.

    Railway mounts volumes root-owned, so the image starts as root when a volume
    is used. Only the home directory itself is chowned (through its descriptor,
    never following links); existing contents are left as they are.
    """
    if os.getuid() == 0:
        path.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(path, DIRECTORY_FLAGS)
        try:
            info = os.fstat(fd)
            if (
                info.st_uid not in (0, UID)
                or info.st_gid not in (0, GID)
                or info.st_mode & 0o022
            ):
                raise ValueError("Unsafe WEECHAT_HOME ownership or permissions")
            os.fchmod(fd, 0o700)
            os.fchown(fd, UID, GID)
        finally:
            os.close(fd)
        drop()
    else:
        path.mkdir(mode=0o700, exist_ok=True)
    info = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid not in (os.getuid(), UID)
        or info.st_mode & 0o022
    ):
        raise ValueError("WEECHAT_HOME is not a private directory owned by this user")
    # A stale IPC endpoint from a previous run would make the bridge connect
    # before WeeChat's plugin is listening.
    for name in ("bridge.sock", "loopback-port"):
        with contextlib.suppress(FileNotFoundError):
            (path / "mcp" / name).unlink()
    return not (path / "weechat.conf").exists()


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run():
    managed = managed_settings(os.environ)
    configuration(os.environ)  # Validate before touching storage or privileges.
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport not in ("stdio", "http"):
        raise ValueError("MCP_TRANSPORT must be stdio or http")
    if transport == "http":
        from bridge import event_settings, http_settings

        auth, _ = http_settings(
            os.environ
        )  # Fail before connecting IRC if HTTP is unsafe.
        event_settings(os.environ, auth, transport)
    os.umask(0o077)
    if managed is not None:
        if not Path(MANAGED_BINARY).is_file():
            raise ValueError(
                "Managed JavaScript requires the opt-in managed-js image target"
            )
        initialize_managed_root(managed["root"])
    persistent = home(os.environ)
    if persistent is not None:
        first_boot = initialize_home(persistent)
        profile_dir = contextlib.nullcontext(str(persistent))
    else:
        drop_privileges()
        first_boot = True
        profile_dir = tempfile.TemporaryDirectory(prefix="weechat-mcp-")
    commands = configuration(os.environ, first_boot=first_boot)
    stopping = 0

    def on_signal(signum, frame):
        nonlocal stopping
        stopping = signum

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_signal)
    children = []
    with profile_dir as profile:
        try:
            env = dict(os.environ, HOME=str(profile))
            loopback = env.get("WEECHAT_MCP_LOOPBACK_PORT")
            if loopback not in (None, "0"):
                raise ValueError(
                    "WEECHAT_MCP_LOOPBACK_PORT only supports explicit test value 0"
                )
            cmd = weechat_command(profile, commands, managed=managed is not None)
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
