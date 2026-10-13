# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic-only persistence, race, filesystem and secret-handling regressions."""

import base64
import importlib
import io
import itertools
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

DEPLOY = Path(__file__).resolve().parent / "deploy"
with patch.object(sys, "path", [str(DEPLOY), *sys.path]):
    keys = importlib.import_module("pomerium_keys")
    entrypoint = importlib.import_module("service_entrypoint")

ENABLED = {"POMERIUM_LOCAL_KEYS": "true"}
SYNTHETIC = {
    "SHARED_SECRET": base64.b64encode(b"synthetic-shared-test-only-value").decode(),
    "COOKIE_SECRET": base64.b64encode(b"synthetic-cookie-test-only-value").decode(),
}


class LocalKeyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / keys.DIRECTORY
        self.bundle = self.directory / keys.BUNDLE
        self.lock = self.directory / keys.LOCK
        for target, name, value in (
            (keys, "UID", os.getuid()),
            (keys, "GID", os.getgid()),
        ):
            context = patch.object(target, name, value)
            context.start()
            self.addCleanup(context.stop)
        groups = patch.object(keys.os, "getgroups", return_value=[])
        groups.start()
        self.addCleanup(groups.stop)
        random = patch.object(
            keys.secrets,
            "token_bytes",
            side_effect=itertools.cycle(
                base64.b64decode(SYNTHETIC[key]) for key in keys.KEYS
            ),
        )
        self.random = random.start()
        self.addCleanup(random.stop)
        names = patch.object(keys.secrets, "token_hex", return_value="synthetic-only")
        names.start()
        self.addCleanup(names.stop)

    def load(self, env=None):
        return keys.pomerium_environment(ENABLED if env is None else env, self.root)

    def assert_fails_without_generation(self, env=None, message=None):
        before = self.random.call_count
        with self.assertRaises(keys.LocalKeyError) as error:
            self.load(env)
        if message:
            self.assertIn(message, str(error.exception))
        self.assertEqual(self.random.call_count, before)

    def test_default_and_false_do_not_touch_storage_or_generate_credentials(self):
        for env in ({}, {"POMERIUM_LOCAL_KEYS": "false"}, SYNTHETIC):
            with self.subTest(env=list(env)), patch.object(keys.os, "open") as opened:
                self.assertEqual(self.load(env), env)
                opened.assert_not_called()
        self.random.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_only_explicit_true_enables_generation(self):
        for flag in ("", "1", "TRUE", "True", " true ", "yes"):
            with self.subTest(flag=flag):
                self.assert_fails_without_generation(
                    {"POMERIUM_LOCAL_KEYS": flag}, "must be true or false"
                )
        self.assertEqual(list(self.root.iterdir()), [])

    def test_generation_is_private_distinct_persistent_and_nonmutating(self):
        env = {**ENABLED, "OTHER": "x"}
        before = env.copy()
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            first = self.load(env)
            previous = self.bundle.stat()
            self.assertEqual(self.load(env), first)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(env, before)
        self.assertEqual(first, {**env, **SYNTHETIC})
        self.assertEqual(self.bundle.stat(), previous)
        self.assertEqual(self.random.call_count, 2)
        self.assertEqual(self.random.call_args_list[0].args, (32,))
        self.assertEqual(self.random.call_args_list[1].args, (32,))
        self.assertEqual(
            json.loads(self.bundle.read_text()), {"version": 1, **SYNTHETIC}
        )
        for path, mode in (
            (self.directory, 0o700),
            (self.bundle, 0o600),
            (self.lock, 0o600),
        ):
            info = path.stat()
            self.assertEqual(stat.S_IMODE(info.st_mode), mode)
            self.assertEqual((info.st_uid, info.st_gid), (os.getuid(), os.getgid()))
        self.assertEqual(
            {p.name for p in self.directory.iterdir()}, {keys.BUNDLE, keys.LOCK}
        )

    def test_exact_service_ids_and_empty_groups_required_before_storage(self):
        for target, value in (
            ("getresuid", (keys.UID, keys.UID, 0)),
            ("getresuid", (keys.UID + 1,) * 3),
            ("getresgid", (keys.GID, keys.GID, 0)),
            ("getgroups", [keys.GID]),
        ):
            with (
                self.subTest(target=target, value=value),
                patch.object(keys.os, target, return_value=value),
            ):
                self.assert_fails_without_generation(message="fully dropped")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_first_start_rejects_explicit_keys_without_overwriting_them(self):
        for name in keys.KEYS:
            env = {**ENABLED, name: SYNTHETIC[name]}
            self.assert_fails_without_generation(env, "Cannot initialize")
            self.assertEqual(env[name], SYNTHETIC[name])
            self.assertFalse(self.bundle.exists())

    def test_matching_runtime_keys_are_preserved_and_conflicts_fail(self):
        self.load()
        before = self.bundle.read_bytes()
        self.assertEqual(self.load({**ENABLED, **SYNTHETIC}), {**ENABLED, **SYNTHETIC})
        for name in keys.KEYS:
            for value in ("", "different", "non-ascii-ë"):
                with self.subTest(name=name, value=value):
                    self.assert_fails_without_generation(
                        {**ENABLED, name: value}, "conflicts"
                    )
        self.assertEqual(self.bundle.read_bytes(), before)

    def test_ambiguous_external_key_files_fail_before_touching_storage(self):
        for name in (
            "SHARED_SECRET_FILE",
            "COOKIE_SECRET_FILE",
            "SIGNING_KEY",
            "SIGNING_KEY_FILE",
            "IDP_CLIENT_SECRET",
            "IDP_CLIENT_SECRET_FILE",
        ):
            self.assert_fails_without_generation(
                {**ENABLED, name: "synthetic-explicit-value"}, "conflict"
            )
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_bundle_with_databroker_never_generates_replacements(self):
        for kind in ("directory", "file", "symlink"):
            state = self.root / "databroker"
            if kind == "directory":
                state.mkdir()
            elif kind == "file":
                state.write_text("existing-synthetic-state")
            else:
                state.symlink_to("/deliberately-missing-target")
            with self.subTest(kind=kind):
                self.assert_fails_without_generation(
                    message="existing databroker state"
                )
                self.assertFalse(self.bundle.exists())
            state.rmdir() if kind == "directory" else state.unlink()

    def test_persisted_keys_are_reused_alongside_databroker(self):
        first = self.load()
        state = self.root / "databroker"
        state.mkdir()
        (state / "CURRENT").write_text("synthetic-state")
        self.assertEqual(self.load(), first)
        self.assertEqual(self.random.call_count, 2)

    def test_invalid_bundle_formats_fail_unchanged_without_regeneration(self):
        self.load()
        valid = {"version": 1, **SYNTHETIC}
        invalid = [
            b"",
            b"{",
            b"\xff",
            b"[]",
            b"null",
            b"x" * 513,
            json.dumps({**valid, "version": 2}).encode(),
            json.dumps({**valid, "version": True}).encode(),
            json.dumps({**valid, "extra": "synthetic"}).encode(),
            json.dumps(
                {"version": 1, "SHARED_SECRET": SYNTHETIC["SHARED_SECRET"]}
            ).encode(),
            json.dumps({**valid, "COOKIE_SECRET": valid["SHARED_SECRET"]}).encode(),
            (
                '{"version":1,"version":1,"SHARED_SECRET":'
                + json.dumps(valid["SHARED_SECRET"])
                + ',"COOKIE_SECRET":'
                + json.dumps(valid["COOKIE_SECRET"])
                + "}"
            ).encode(),
        ]
        for value in (
            None,
            23,
            [],
            "!" * 44,
            "a" * 43,
            "a" * 44,
            base64.b64encode(b"x" * 31).decode(),
            base64.b64encode(b"x" * 33).decode(),
            SYNTHETIC["SHARED_SECRET"][:-2] + "F=",
        ):
            invalid.append(json.dumps({**valid, "SHARED_SECRET": value}).encode())
        for content in invalid:
            with self.subTest(content=content):
                self.bundle.write_bytes(content)
                self.assert_fails_without_generation(message="Invalid local key bundle")
                self.assertEqual(self.bundle.read_bytes(), content)

    def test_permissions_validated_on_every_restart(self):
        self.load()
        for path, expected in (
            (self.root, 0o700),
            (self.directory, 0o700),
            (self.bundle, 0o600),
            (self.lock, 0o600),
        ):
            for mode in (0o777, 0o755, 0o640, expected | stat.S_ISUID):
                with self.subTest(path=path.name, mode=mode):
                    path.chmod(mode)
                    self.assert_fails_without_generation()
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
                    path.chmod(expected)

    def test_wrong_owner_or_group_for_each_object_fails(self):
        self.load()
        original = keys.os.fstat
        for path in (self.root, self.directory, self.bundle, self.lock):
            inode = path.stat().st_ino
            for field in (4, 5):  # stat uid, gid

                def changed(fd):
                    result = original(fd)
                    if result.st_ino == inode:
                        fields = list(result)
                        fields[field] += 1
                        return os.stat_result(fields)
                    return result

                with (
                    self.subTest(path=path.name, field=field),
                    patch.object(keys.os, "fstat", side_effect=changed),
                ):
                    self.assert_fails_without_generation()

    def test_symlink_to_volume_directory_is_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(keys.LocalKeyError):
            keys.pomerium_environment(ENABLED, alias)
        self.random.assert_not_called()

    def test_key_directory_symlink_is_rejected_without_changing_target(self):
        target = self.root / "other"
        target.mkdir()
        self.directory.symlink_to(target, target_is_directory=True)
        self.assert_fails_without_generation()
        self.assertEqual(list(target.iterdir()), [])

    def test_bundle_and_lock_reject_symlink_hardlink_fifo_directory(self):
        self.load()
        for path in (self.bundle, self.lock):
            original = path.read_bytes()
            for kind in ("symlink", "hardlink", "fifo", "directory"):
                path.unlink()
                target = self.root / "untouched"
                target.write_bytes(original)
                target.chmod(0o600)
                if kind == "symlink":
                    path.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, path)
                elif kind == "fifo":
                    os.mkfifo(path, 0o600)
                else:
                    path.mkdir(mode=0o700)
                with self.subTest(path=path.name, kind=kind):
                    self.assert_fails_without_generation()
                    self.assertEqual(target.read_bytes(), original)
                path.rmdir() if kind == "directory" else path.unlink()
                target.unlink()
                path.write_bytes(original)
                path.chmod(0o600)

    def test_unexpected_files_or_nonempty_lock_fail_closed(self):
        self.load()
        pending = self.directory / ".pending-interrupted"
        pending.write_text("synthetic-interrupted-write")
        self.assert_fails_without_generation(message="Unexpected local key files")
        pending.unlink()
        self.lock.write_text("synthetic-corrupt-lock")
        self.assert_fails_without_generation(message="Invalid local key lock")

    def test_identical_random_keys_fail_closed(self):
        self.random.side_effect = lambda size: b"x" * size
        with self.assertRaises(keys.LocalKeyError):
            self.load()
        self.assertFalse(self.bundle.exists())

    def test_bundle_is_fsynced_then_atomically_renamed_then_directory_fsynced(self):
        events = []
        fsync, rename = keys.os.fsync, keys.os.rename

        def sync(fd):
            events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            return fsync(fd)

        def replace(*args, **kwargs):
            self.assertFalse(self.bundle.exists())
            events.append("rename")
            return rename(*args, **kwargs)

        with (
            patch.object(keys.os, "fsync", side_effect=sync),
            patch.object(keys.os, "rename", side_effect=replace),
        ):
            self.load()
        self.assertEqual(events, ["directory", "file", "rename", "directory"])

    def test_failed_write_never_publishes_bundle_and_cleans_temporary_file(self):
        fsync = keys.os.fsync

        def fail_file(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("synthetic-write-failure")
            return fsync(fd)

        with (
            patch.object(keys.os, "fsync", side_effect=fail_file),
            self.assertRaises(keys.LocalKeyError),
        ):
            self.load()
        self.assertEqual({path.name for path in self.directory.iterdir()}, {keys.LOCK})
        self.assertFalse(self.bundle.exists())

    def test_postrename_fsync_failure_does_not_regenerate_written_bundle(self):
        fsync = keys.os.fsync

        def fail_after_publish(fd):
            if self.bundle.exists():
                raise OSError("synthetic-directory-fsync-failure")
            return fsync(fd)

        with (
            patch.object(keys.os, "fsync", side_effect=fail_after_publish),
            self.assertRaises(keys.LocalKeyError),
        ):
            self.load()
        self.assertTrue(self.bundle.exists())
        self.assertEqual(self.load(), {**ENABLED, **SYNTHETIC})
        self.assertEqual(self.random.call_count, 2)

    def test_replaced_lock_is_detected_after_acquiring_lock(self):
        self.load()
        flock = keys.fcntl.flock

        def replace_lock(fd, operation):
            flock(fd, operation)
            self.lock.unlink()
            self.lock.touch(mode=0o600)

        with patch.object(keys.fcntl, "flock", side_effect=replace_lock):
            self.assert_fails_without_generation(message="Storage")

    def test_concurrent_processes_share_exactly_one_committed_bundle(self):
        script = f"""
import base64, json, os, sys
from pathlib import Path
sys.path.insert(0, {str(DEPLOY)!r})
import pomerium_keys as keys
keys.UID, keys.GID = os.getuid(), os.getgid()
keys.os.getgroups = lambda: []
root, worker = Path(sys.argv[1]), int(sys.argv[2])
calls = 0
def synthetic(size):
    global calls
    calls += 1
    return bytes([worker * 2 + calls]) * size
keys.secrets.token_bytes = synthetic
keys.secrets.token_hex = lambda size: "synthetic-worker-" + str(worker)
result = keys.pomerium_environment({ENABLED!r}, root)
assert result == keys.pomerium_environment({ENABLED!r}, root)
(root / ("result-" + str(worker))).write_text(json.dumps({{"calls": calls, **result}}))
"""
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.root), str(i)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for i in range(8)
        ]
        self.addCleanup(
            lambda: [process.kill() for process in processes if process.poll() is None]
        )
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertEqual(stdout + stderr, "")
        results = [
            json.loads((self.root / f"result-{i}").read_text()) for i in range(8)
        ]
        self.assertEqual(sum(result.pop("calls") for result in results), 2)
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(
            keys.decode_bundle(self.bundle.read_bytes()),
            {name: results[0][name] for name in keys.KEYS},
        )


class LocalKeyEntrypointTests(unittest.TestCase):
    def test_only_pomerium_child_receives_generated_keys(self):
        env = {
            **ENABLED,
            "MCP_AUTH_TOKEN": "synthetic-backend-token",
            "GITHUB_CLIENT_SECRET": "synthetic-github-token",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(sys, "argv", ["service_entrypoint.py", "pomerium"]),
            patch.object(entrypoint.os, "umask"),
            patch.object(entrypoint, "render"),
            patch.object(
                entrypoint, "pomerium_environment", return_value={**env, **SYNTHETIC}
            ) as prepare,
            patch.object(entrypoint, "supervise", return_value=0) as supervisor,
        ):
            self.assertEqual(entrypoint.main(), 0)
            self.assertEqual(dict(os.environ), env)
        prepare.assert_called_once()
        ((_, child),) = supervisor.call_args.args[0]
        self.assertEqual(child, {**ENABLED, **SYNTHETIC})

    def test_auth_service_never_initializes_local_keys(self):
        with (
            patch.dict(os.environ, ENABLED, clear=True),
            patch.object(sys, "argv", ["service_entrypoint.py", "auth"]),
            patch.object(entrypoint.os, "umask"),
            patch.object(entrypoint, "render"),
            patch.object(entrypoint, "pomerium_environment") as prepare,
            patch.object(entrypoint.subprocess, "run"),
            patch.object(entrypoint, "supervise", return_value=0),
        ):
            self.assertEqual(entrypoint.main(), 0)
        prepare.assert_not_called()

    def test_local_key_failure_stops_before_pomerium_child_starts(self):
        with (
            patch.object(sys, "argv", ["service_entrypoint.py", "pomerium"]),
            patch.object(entrypoint.os, "umask"),
            patch.object(entrypoint, "render"),
            patch.object(
                entrypoint,
                "pomerium_environment",
                side_effect=keys.LocalKeyError("Invalid local key bundle."),
            ),
            patch.object(entrypoint, "supervise") as supervisor,
            self.assertRaises(keys.LocalKeyError),
        ):
            entrypoint.main()
        supervisor.assert_not_called()

    def test_startup_conflict_names_key_without_logging_value_or_traceback(self):
        script = f"""
import os, runpy, sys
sys.path.insert(0, {str(DEPLOY)!r})
import pomerium_keys, render_config
render_config.render = lambda *args: None
pomerium_keys.pomerium_environment = lambda env: (_ for _ in ()).throw(pomerium_keys.LocalKeyError("Runtime SHARED_SECRET conflicts with the persisted local key."))
os.environ['SHARED_SECRET'] = {SYNTHETIC["SHARED_SECRET"]!r}
sys.argv = ['service_entrypoint.py', 'pomerium']
runpy.run_path({str(DEPLOY / "service_entrypoint.py")!r}, run_name='__main__')
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("SHARED_SECRET conflicts", result.stderr)
        self.assertNotIn(SYNTHETIC["SHARED_SECRET"], result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_new_module_is_in_both_runtime_images_and_build_context(self):
        self.assertIn(
            "!deploy/pomerium_keys.py", (DEPLOY.parent / ".dockerignore").read_text()
        )
        for service in ("pomerium", "auth"):
            self.assertIn(
                "deploy/pomerium_keys.py", (DEPLOY / service / "Dockerfile").read_text()
            )


if __name__ == "__main__":
    unittest.main()
