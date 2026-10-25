#!/usr/bin/env python3
"""Initialize the mounted volume, then permanently drop privileges before startup."""

import os
import secrets
import stat
import sys
from pathlib import Path

UID = GID = 10001
DATA = Path("/data")
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def initialize_volume(path=DATA):
    # Railway mounts a root-owned volume over the image's /data. Modify only this
    # directory, via its descriptor; never follow links or recursively chown data.
    fd = os.open(path, DIRECTORY_FLAGS)
    try:
        info = os.fstat(fd)
        if (
            info.st_uid not in (0, UID)
            or info.st_gid not in (0, GID)
            or info.st_mode & 0o022
        ):
            raise ValueError("Unsafe volume ownership or permissions")
        os.fchmod(fd, 0o700)
        os.fchown(fd, UID, GID)
    finally:
        os.close(fd)


def drop_privileges():
    os.setgroups([])
    os.setresgid(GID, GID, GID)
    os.setresuid(UID, UID, UID)
    if (
        os.getresuid() != (UID, UID, UID)
        or os.getresgid() != (GID, GID, GID)
        or os.getgroups()
    ):
        raise ValueError("Privilege drop failed")


def validate_state(fd, directory):
    info = os.fstat(fd)
    if (
        info.st_uid != UID
        or info.st_gid != GID
        or info.st_mode & 0o022
        or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        or (not directory and info.st_nlink != 1)
    ):
        raise ValueError("Unsafe persistent state")
    if directory:
        for name in os.listdir(fd):
            # O_NONBLOCK prevents a malicious FIFO from blocking this check.
            child = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
            )
            try:
                validate_state(child, stat.S_ISDIR(os.fstat(child).st_mode))
            finally:
                os.close(child)


def check_storage(service, path=DATA):
    fd = os.open(path, DIRECTORY_FLAGS)
    try:
        info = os.fstat(fd)
        if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (UID, GID, 0o700):
            raise ValueError("Volume is not private service-owned storage")
        names = (
            ("databroker", "databroker.refresh-authority")
            if service == "pomerium"
            else ("dex.db", "dex.db-wal", "dex.db-shm", "dex.db-journal")
        )
        for name in names:
            try:
                child = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
                )
            except FileNotFoundError:
                continue
            try:
                validate_state(child, directory=service == "pomerium")
            finally:
                os.close(child)
        probe = ".weechat-write-test-" + secrets.token_hex(12)
        output = os.open(
            probe,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=fd,
        )
        try:
            os.write(output, b"storage readiness\n")
            os.fsync(output)
        finally:
            os.close(output)
            os.unlink(probe, dir_fd=fd)
    finally:
        os.close(fd)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("auth", "pomerium"):
        raise ValueError("Expected service: auth or pomerium")
    if os.geteuid() != 0:
        raise ValueError("Volume initialization requires root")
    os.umask(0o077)
    initialize_volume()
    drop_privileges()
    check_storage(sys.argv[1])
    # Configuration rendering and all service processes run after the drop. This
    # exec also keeps the non-root supervisor as PID 1 for normal signal handling.
    os.execv(
        sys.executable,
        [
            sys.executable,
            str(Path(__file__).with_name("service_entrypoint.py")),
            sys.argv[1],
        ],
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError):
        print(
            "Volume initialization failed; check private storage ownership and permissions.",
            file=sys.stderr,
        )
        sys.exit(1)
