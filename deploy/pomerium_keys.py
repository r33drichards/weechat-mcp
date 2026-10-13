#!/usr/bin/env python3
"""Opt-in, single-volume Pomerium keys; never print key material."""

import base64
import binascii
import fcntl
import hmac
import json
import os
import secrets
import stat
from pathlib import Path

UID = GID = 10001
DIRECTORY = "pomerium-keys"
BUNDLE = "keys.json"
LOCK = ".lock"
KEYS = ("SHARED_SECRET", "COOKIE_SECRET")
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK
MAX_BUNDLE_BYTES = 512


class LocalKeyError(ValueError):
    """Only fixed, credential-free messages may be passed to this exception."""


def validate_object(fd, directory=False):
    info = os.fstat(fd)
    expected_mode = 0o700 if directory else 0o600
    if (
        info.st_uid != UID
        or info.st_gid != GID
        or stat.S_IMODE(info.st_mode) != expected_mode
        or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        or (not directory and info.st_nlink != 1)
    ):
        raise LocalKeyError("Storage must be private, service-owned and link-free.")
    return info


def validate_entry(parent, name, fd, directory=False):
    info = validate_object(fd, directory)
    entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (info.st_dev, info.st_ino) != (entry.st_dev, entry.st_ino):
        raise LocalKeyError("Storage changed during startup.")


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LocalKeyError("Invalid local key bundle; restore a verified backup.")
        result[key] = value
    return result


def decode_bundle(content):
    try:
        bundle = json.loads(content, object_pairs_hook=unique_fields)
        if (
            not isinstance(bundle, dict)
            or set(bundle) != {"version", *KEYS}
            or type(bundle["version"]) is not int
            or bundle["version"] != 1
        ):
            raise ValueError
        for key in KEYS:
            value = bundle[key]
            if not isinstance(value, str) or len(value) != 44:
                raise ValueError
            raw = base64.b64decode(value, validate=True)
            if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != value:
                raise ValueError
        if hmac.compare_digest(bundle[KEYS[0]], bundle[KEYS[1]]):
            raise ValueError
    except (ValueError, UnicodeError, binascii.Error):
        raise LocalKeyError(
            "Invalid local key bundle; restore a verified backup."
        ) from None
    return {key: bundle[key] for key in KEYS}


def read_bundle(directory):
    fd = os.open(BUNDLE, os.O_RDONLY | FILE_FLAGS, dir_fd=directory)
    try:
        info = validate_object(fd)
        if not 0 < info.st_size <= MAX_BUNDLE_BYTES:
            raise LocalKeyError("Invalid local key bundle; restore a verified backup.")
        content = os.read(fd, MAX_BUNDLE_BYTES + 1)
        if len(content) != info.st_size:
            raise LocalKeyError("Local key bundle changed during startup.")
        bundle = decode_bundle(content)
        validate_entry(directory, BUNDLE, fd)
        return bundle
    finally:
        os.close(fd)


def create_bundle(directory):
    # Separate CSPRNG calls. A repeated output fails rather than sharing keys.
    bundle = {key: base64.b64encode(secrets.token_bytes(32)).decode() for key in KEYS}
    payload = (json.dumps({"version": 1, **bundle}, sort_keys=True) + "\n").encode()
    decode_bundle(payload)
    name = ".pending-" + secrets.token_hex(16)
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FILE_FLAGS, 0o600, dir_fd=directory
    )
    try:
        validate_object(fd)
        # fdopen owns a duplicate so that descriptor validation remains possible.
        with os.fdopen(os.dup(fd), "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        validate_entry(directory, name, fd)
        os.rename(name, BUNDLE, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(fd)
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass


def load_keys(env, path):
    data = os.open(path, DIRECTORY_FLAGS)
    try:
        validate_object(data, directory=True)
        try:
            os.mkdir(DIRECTORY, 0o700, dir_fd=data)
        except FileExistsError:
            pass
        directory = os.open(DIRECTORY, DIRECTORY_FLAGS, dir_fd=data)
        try:
            validate_entry(data, DIRECTORY, directory, directory=True)
            os.fsync(data)
            lock = os.open(
                LOCK, os.O_RDWR | os.O_CREAT | FILE_FLAGS, 0o600, dir_fd=directory
            )
            try:
                validate_object(lock)
                # Never unlink the lock: every process must lock the same inode.
                fcntl.flock(lock, fcntl.LOCK_EX)
                validate_entry(data, DIRECTORY, directory, directory=True)
                validate_entry(directory, LOCK, lock)
                if os.fstat(lock).st_size != 0:
                    raise LocalKeyError("Invalid local key lock file.")
                entries = set(os.listdir(directory))
                if entries - {LOCK, BUNDLE}:
                    raise LocalKeyError(
                        "Unexpected local key files; restore a verified backup."
                    )
                if BUNDLE not in entries:
                    try:
                        os.stat("databroker", dir_fd=data, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise LocalKeyError(
                            "Local keys are missing beside existing databroker state; "
                            "restore the matching key bundle from a verified backup."
                        )
                    if any(key in env for key in KEYS):
                        raise LocalKeyError(
                            "Cannot initialize local keys with SHARED_SECRET or "
                            "COOKIE_SECRET set; use external keys with local mode off."
                        )
                    create_bundle(directory)
                bundle = read_bundle(directory)
                for key in KEYS:
                    if key in env and not hmac.compare_digest(
                        env[key].encode(), bundle[key].encode()
                    ):
                        raise LocalKeyError(
                            "Runtime "
                            + key
                            + " conflicts with the persisted local key."
                        )
                validate_entry(data, DIRECTORY, directory, directory=True)
                validate_entry(directory, LOCK, lock)
                return bundle
            finally:
                os.close(lock)
        finally:
            os.close(directory)
    finally:
        os.close(data)


def pomerium_environment(env, path=Path("/data")):
    """Return a private child environment, leaving the supervisor's env unchanged."""
    result = dict(env)
    enabled = env.get("POMERIUM_LOCAL_KEYS", "false")
    if enabled == "false":
        return result
    if enabled != "true":
        raise LocalKeyError("POMERIUM_LOCAL_KEYS must be true or false.")
    if (
        os.getresuid() != (UID, UID, UID)
        or os.getresgid() != (GID, GID, GID)
        or os.getgroups()
    ):
        raise LocalKeyError("Local keys require the fully dropped service identity.")
    for name in (
        "SHARED_SECRET_FILE",
        "COOKIE_SECRET_FILE",
        "SIGNING_KEY",
        "SIGNING_KEY_FILE",
        "IDP_CLIENT_SECRET",
        "IDP_CLIENT_SECRET_FILE",
    ):
        if name in env:
            raise LocalKeyError(name + " conflicts with local key mode.")
    try:
        result.update(load_keys(env, path))
    except OSError:
        raise LocalKeyError("Local key storage is unavailable or unsafe.") from None
    return result
