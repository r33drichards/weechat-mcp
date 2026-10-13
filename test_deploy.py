# SPDX-License-Identifier: GPL-3.0-or-later
"""Private deployment rendering and required-service supervision regressions."""

import importlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

DEPLOY = Path(__file__).resolve().parent / "deploy"
with patch.object(sys, "path", [str(DEPLOY), *sys.path]):
    renderer = importlib.import_module("render_config")
    entrypoint = importlib.import_module("service_entrypoint")
    volume = importlib.import_module("volume_entrypoint")

POMERIUM_ENV = {
    "AUTH_ORIGIN": "https://auth.example.test",
    "MCP_ORIGIN": "https://mcp.example.test",
    "BACKEND_ORIGIN": "http://bridge.railway.internal:8000",
    "OWNER_EMAIL": "owner@example.test",
    "MCP_AUTH_TOKEN": "synthetic-backend-token-for-tests-only-12345",
}
AUTH_ENV = {
    "AUTH_ORIGIN": POMERIUM_ENV["AUTH_ORIGIN"],
    "POMERIUM_INTERNAL_ORIGIN": "http://pomerium.railway.internal:8080",
    "PORT": "4321",
}
ALL_ENV = {**POMERIUM_ENV, **AUTH_ENV}
SYNTHETIC_SECRETS = {
    name: "synthetic-" + name.lower().replace("_", "-") + "-tests-only-12345"
    for name in entrypoint.SECRETS
}


class RendererTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "rendered"
        dns = patch.object(
            renderer, "resolver_addresses", return_value="10.0.0.2 [fd00::53]"
        )
        dns.start()
        self.addCleanup(dns.stop)

    def render(self, service="all", env=None):
        renderer.render(service, ALL_ENV if env is None else env, self.output)
        return {path.name: path.read_text() for path in self.output.iterdir()}

    def test_two_public_origins_and_dex_shares_auth_origin(self):
        files = self.render()
        pomerium, dex, router = (
            files["pomerium.yaml"],
            files["dex.yaml"],
            files["router.conf"],
        )
        self.assertIn('authenticate_service_url: "https://auth.example.test"', pomerium)
        self.assertIn('idp_provider_url: "https://auth.example.test/dex"', pomerium)
        self.assertIn('from: "https://mcp.example.test"', pomerium)
        self.assertIn('issuer: "https://auth.example.test/dex"', dex)
        self.assertIn('"https://auth.example.test/oauth2/callback"', dex)
        self.assertIn('redirectURI: "https://auth.example.test/dex/callback"', dex)
        self.assertIn('proxy_set_header Host "auth.example.test";', router)
        self.assertNotIn("dex.example.test", "".join(files.values()))
        for text in files.values():
            self.assertNotIn("@@", text)

    def test_dex_and_health_listeners_are_loopback_only(self):
        files = self.render()
        self.assertIn("web:\n  http: 127.0.0.1:5556\n", files["dex.yaml"])
        self.assertIn("telemetry:\n  http: 127.0.0.1:5558\n", files["dex.yaml"])
        self.assertIn("health_check_addr: 127.0.0.1:28080\n", files["pomerium.yaml"])
        self.assertNotIn("0.0.0.0:555", files["dex.yaml"])
        self.assertNotIn("127.0.0.1:5558", files["router.conf"])
        self.assertNotIn("28080", files["router.conf"])

    def test_only_mcp_route_is_present_in_pomerium(self):
        pomerium = self.render()["pomerium.yaml"]
        routes = pomerium.split("\nroutes:\n", 1)[1]
        self.assertEqual(routes.count("\n    from:"), 1)
        self.assertIn("regex: '^/mcp(/.*)?$'", routes)
        self.assertNotIn("/dex", routes)
        self.assertNotIn("allow_public_unauthenticated_access", routes)
        self.assertIn('is: "owner@example.test"', routes)
        self.assertIn(
            "Authorization: " + json.dumps("Bearer " + POMERIUM_ENV["MCP_AUTH_TOKEN"]),
            routes,
        )

    def test_auth_does_not_require_or_render_backend_inputs(self):
        files = self.render("auth", AUTH_ENV)
        self.assertEqual(set(files), {"dex.yaml", "router.conf"})
        rendered = "".join(files.values())
        self.assertNotIn(POMERIUM_ENV["MCP_AUTH_TOKEN"], rendered)
        self.assertNotIn(POMERIUM_ENV["OWNER_EMAIL"], rendered)
        self.assertNotIn(POMERIUM_ENV["BACKEND_ORIGIN"], rendered)

    def test_pomerium_does_not_require_auth_router_inputs_or_dns(self):
        with patch.object(renderer, "resolver_addresses") as resolvers:
            files = self.render("pomerium", POMERIUM_ENV)
        resolvers.assert_not_called()
        self.assertEqual(set(files), {"pomerium.yaml"})

    def test_required_inputs_are_service_specific(self):
        for service, base, keys in (
            ("auth", AUTH_ENV, ("AUTH_ORIGIN", "POMERIUM_INTERNAL_ORIGIN")),
            ("pomerium", POMERIUM_ENV, tuple(POMERIUM_ENV)),
        ):
            for key in keys:
                env = dict(base)
                del env[key]
                with self.subTest(service=service, key=key):
                    with self.assertRaisesRegex(ValueError, key):
                        renderer.configuration(service, env)

    def test_equal_public_origins_are_rejected(self):
        for mcp in (
            ALL_ENV["AUTH_ORIGIN"],
            ALL_ENV["AUTH_ORIGIN"].upper(),
            ALL_ENV["AUTH_ORIGIN"] + "/",
            ALL_ENV["AUTH_ORIGIN"] + ":443",
        ):
            with self.subTest(mcp=mcp), self.assertRaises(ValueError):
                renderer.configuration("all", {**ALL_ENV, "MCP_ORIGIN": mcp})

    def test_origin_rejects_unsafe_nginx_interpolation(self):
        invalid = (
            "http://public.example.test",
            "https://user:password@example.test",
            "https://example.test/path",
            "https://example.test?query=value",
            "https://example.test#fragment",
            "https://example.test:0",
            "https://example.test:65536",
            "https://example.test:notaport",
            "https://example.test;include",
            'https://example.test"',
            "https://example.test${evil}",
            "https://example.test\\evil",
            "https://example.\ntest",
            "https://example.\rtest",
            "https://example.\ttest",
            "https://example.test\x00",
            "https://exámple.test",
            "https://",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                renderer.origin("AUTH_ORIGIN", {"AUTH_ORIGIN": value}, public=True)

    def test_private_upstream_rejects_interpolation_and_path_injection(self):
        for value in (
            "http://pomerium.internal:8080;",
            "http://pomerium.internal:8080/path",
            "http://pomerium.\ninternal:8080",
            "http://$upstream:8080",
            "file:///etc/passwd",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                renderer.configuration(
                    "auth", {**AUTH_ENV, "POMERIUM_INTERNAL_ORIGIN": value}
                )

    def test_origin_accepts_https_and_private_ipv6_with_ports(self):
        for value, public in (
            ("https://auth.example.test:8443/", True),
            ("http://[fd00::1]:8080", False),
            ("https://private.example.test", False),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    renderer.origin("ORIGIN", {"ORIGIN": value}, public),
                    value.rstrip("/"),
                )

    def test_router_port_validation_and_default(self):
        for value in (
            "0",
            "80",
            "1023",
            "65536",
            "-1",
            "8080;",
            "8080\n",
            "abc",
            "４３２１",
            "٤٣٢١",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                renderer.configuration("auth", {**AUTH_ENV, "PORT": value})
        default = {key: value for key, value in AUTH_ENV.items() if key != "PORT"}
        self.assertEqual(renderer.configuration("auth", default)["PORT"], "8080")
        router = self.render("auth", AUTH_ENV)["router.conf"]
        self.assertIn("listen 4321;", router)
        self.assertIn("listen [::]:4321;", router)
        self.assertIn("resolver 10.0.0.2 [fd00::53] valid=10s", router)

    def test_invalid_email_and_backend_token_rejected(self):
        for key, value in (
            ("OWNER_EMAIL", "missing-at-sign"),
            ("OWNER_EMAIL", "owner@ example.test"),
            ("MCP_AUTH_TOKEN", "short"),
            ("MCP_AUTH_TOKEN", "x" * 32 + ";"),
            ("MCP_AUTH_TOKEN", "é" * 32),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                renderer.configuration("pomerium", {**POMERIUM_ENV, key: value})

    def test_json_quoting_keeps_owner_email_a_single_yaml_scalar(self):
        owner = 'owner"@example.test'
        pomerium = self.render("pomerium", {**POMERIUM_ENV, "OWNER_EMAIL": owner})[
            "pomerium.yaml"
        ]
        self.assertIn("is: " + json.dumps(owner), pomerium)

    def test_private_directory_and_file_permissions_even_when_preexisting(self):
        self.output.mkdir(mode=0o755)
        destination = self.output / "pomerium.yaml"
        destination.write_text("old configuration")
        destination.chmod(0o644)
        self.render()
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        for path in self.output.iterdir():
            with self.subTest(path=path.name):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_atomic_replace_does_not_follow_destination_symlink(self):
        self.output.mkdir()
        target = self.root / "do-not-overwrite"
        target.write_text("unrelated private content")
        destination = self.output / "pomerium.yaml"
        destination.symlink_to(target)
        self.render("pomerium", POMERIUM_ENV)
        self.assertEqual(target.read_text(), "unrelated private content")
        self.assertFalse(destination.is_symlink())
        self.assertIn("routes:", destination.read_text())

    def test_output_directory_symlink_is_rejected(self):
        target = self.root / "target"
        target.mkdir(mode=0o755)
        self.output.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.render()
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_symlinked_parent_directory_is_rejected(self):
        target = self.root / "target"
        target.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(target, target_is_directory=True)
        self.output = alias / "rendered"
        with self.assertRaises(ValueError):
            self.render()
        self.assertEqual(list(target.iterdir()), [])

    def test_failed_atomic_replace_preserves_previous_file_and_cleans_temp(self):
        self.output.mkdir()
        destination = self.output / "pomerium.yaml"
        destination.write_text("previous complete configuration")
        with patch.object(renderer.os, "replace", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                self.render("pomerium", POMERIUM_ENV)
        self.assertEqual(destination.read_text(), "previous complete configuration")
        self.assertEqual(list(self.output.iterdir()), [destination])

    def test_invalid_configuration_creates_no_output(self):
        with self.assertRaises(ValueError):
            self.render("all", {**ALL_ENV, "MCP_AUTH_TOKEN": "short"})
        self.assertFalse(self.output.exists())

    def test_cli_success_and_validation_failure_never_print_secrets(self):
        env = {**os.environ, **ALL_ENV, **SYNTHETIC_SECRETS}
        for invalid in (False, True):
            if invalid:
                env["MCP_AUTH_TOKEN"] = "synthetic-invalid-secret!"
            result = subprocess.run(
                [
                    sys.executable,
                    str(DEPLOY / "render_config.py"),
                    "--output",
                    str(self.output),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            with self.subTest(invalid=invalid):
                self.assertEqual(result.returncode, int(invalid), result.stderr)
                output = result.stdout + result.stderr
                for secret in (*SYNTHETIC_SECRETS.values(), env["MCP_AUTH_TOKEN"]):
                    self.assertNotIn(secret, output)
                self.assertNotIn("Authorization:", output)
                self.assertNotIn("issuer:", output)


class VolumeEntrypointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Filesystem tests use this unprivileged test user's actual ownership.
        for name, value in (("UID", os.getuid()), ("GID", os.getgid())):
            patched = patch.object(volume, name, value)
            patched.start()
            self.addCleanup(patched.stop)

    def test_only_volume_directory_is_changed(self):
        unrelated = self.root / "unrelated"
        unrelated.mkdir(mode=0o755)
        marker = unrelated / "keep"
        marker.write_text("unchanged")
        previous = (unrelated.stat(), marker.stat())
        with patch.object(volume.os, "fchown") as chown:
            volume.initialize_volume(self.root)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        chown.assert_called_once()
        self.assertEqual(chown.call_args.args[1:], (os.getuid(), os.getgid()))
        self.assertEqual((unrelated.stat(), marker.stat()), previous)
        self.assertEqual(marker.read_text(), "unchanged")

    def test_root_owned_empty_volume_is_supported(self):
        info = Mock(st_uid=0, st_gid=0, st_mode=stat.S_IFDIR | 0o755)
        with (
            patch.object(volume.os, "fstat", return_value=info),
            patch.object(volume.os, "fchmod") as chmod,
            patch.object(volume.os, "fchown") as chown,
        ):
            volume.initialize_volume(self.root)
        chmod.assert_called_once()
        self.assertEqual(chmod.call_args.args[1], 0o700)
        chown.assert_called_once()

    def test_unsafe_volume_ownership_or_permissions_are_rejected_unchanged(self):
        for uid, gid, mode in ((12345, 0, 0o755), (0, 12345, 0o755), (0, 0, 0o777)):
            info = Mock(st_uid=uid, st_gid=gid, st_mode=stat.S_IFDIR | mode)
            with (
                self.subTest(uid=uid, gid=gid, mode=mode),
                patch.object(volume.os, "fstat", return_value=info),
                patch.object(volume.os, "fchmod") as chmod,
                patch.object(volume.os, "fchown") as chown,
                self.assertRaises(ValueError),
            ):
                volume.initialize_volume(self.root)
            chmod.assert_not_called()
            chown.assert_not_called()

    def test_volume_symlink_and_non_directory_are_rejected(self):
        target = self.root / "target"
        target.mkdir(mode=0o755)
        alias = self.root / "alias"
        alias.symlink_to(target, target_is_directory=True)
        regular = self.root / "file"
        regular.touch()
        for path in (alias, regular):
            with self.subTest(path=path), self.assertRaises(OSError):
                volume.initialize_volume(path)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_privilege_drop_clears_groups_and_real_effective_saved_ids_in_order(self):
        calls = Mock()
        with (
            patch.object(volume.os, "setgroups", calls.groups),
            patch.object(volume.os, "setresgid", calls.gid),
            patch.object(volume.os, "setresuid", calls.uid),
            patch.object(volume.os, "getresuid", return_value=(volume.UID,) * 3),
            patch.object(volume.os, "getresgid", return_value=(volume.GID,) * 3),
            patch.object(volume.os, "getgroups", return_value=[]),
        ):
            volume.drop_privileges()
        self.assertEqual(
            calls.mock_calls,
            [
                call.groups([]),
                call.gid(*((volume.GID,) * 3)),
                call.uid(*((volume.UID,) * 3)),
            ],
        )

    def test_privilege_drop_verifies_saved_ids_and_groups(self):
        for uids, gids, groups in (
            ((volume.UID, volume.UID, 0), (volume.GID,) * 3, []),
            ((volume.UID,) * 3, (volume.GID, volume.GID, 0), []),
            ((volume.UID,) * 3, (volume.GID,) * 3, [0]),
        ):
            with (
                self.subTest(uids=uids, gids=gids, groups=groups),
                patch.object(volume.os, "setgroups"),
                patch.object(volume.os, "setresgid"),
                patch.object(volume.os, "setresuid"),
                patch.object(volume.os, "getresuid", return_value=uids),
                patch.object(volume.os, "getresgid", return_value=gids),
                patch.object(volume.os, "getgroups", return_value=groups),
                self.assertRaises(ValueError),
            ):
                volume.drop_privileges()

    def test_storage_preserves_existing_state_and_unrelated_files(self):
        (self.root / "dex.db").write_text("existing database")
        (self.root / "unrelated").symlink_to("/does/not/exist")
        volume.check_storage("auth", self.root)
        self.assertEqual((self.root / "dex.db").read_text(), "existing database")
        self.assertEqual(
            sorted(p.name for p in self.root.iterdir()), ["dex.db", "unrelated"]
        )
        (self.root / "databroker").mkdir()
        (self.root / "databroker" / "CURRENT").write_text("existing database")
        volume.check_storage("pomerium", self.root)
        self.assertEqual(
            (self.root / "databroker" / "CURRENT").read_text(), "existing database"
        )

    def test_storage_symlinks_hardlinks_and_fifo_are_rejected(self):
        target = self.root / "other"
        target.write_text("untouched")
        state = self.root / "dex.db"
        for kind in ("symlink", "hardlink", "fifo", "directory"):
            if kind == "symlink":
                state.symlink_to(target)
            elif kind == "hardlink":
                os.link(target, state)
            elif kind == "fifo":
                os.mkfifo(state)
            else:
                state.mkdir()
            with self.subTest(kind=kind), self.assertRaises((ValueError, OSError)):
                volume.check_storage("auth", self.root)
            state.rmdir() if kind == "directory" else state.unlink()
        self.assertEqual(target.read_text(), "untouched")

    def test_nested_pomerium_state_symlink_is_rejected(self):
        state = self.root / "databroker"
        state.mkdir()
        (state / "CURRENT").symlink_to("/does/not/exist")
        with self.assertRaises(OSError):
            volume.check_storage("pomerium", self.root)

    def test_storage_unsafe_owner_or_permissions_are_rejected(self):
        (self.root / "dex.db").write_text("existing database")
        (self.root / "dex.db").chmod(0o666)
        with self.assertRaises(ValueError):
            volume.check_storage("auth", self.root)
        (self.root / "dex.db").chmod(0o600)
        with patch.object(volume, "UID", 12345), self.assertRaises(ValueError):
            volume.check_storage("auth", self.root)

    def test_storage_write_failure_is_fatal_and_probe_is_removed(self):
        with (
            patch.object(volume.os, "write", side_effect=OSError("read-only storage")),
            self.assertRaises(OSError),
        ):
            volume.check_storage("auth", self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_bootstrap_order_and_fail_closed_before_exec(self):
        for failure in (None, "initialize", "drop", "check"):
            events = Mock()
            if failure:
                getattr(events, failure).side_effect = OSError("synthetic failure")
            with (
                self.subTest(failure=failure),
                patch.object(sys, "argv", ["volume_entrypoint.py", "auth"]),
                patch.object(volume.os, "geteuid", return_value=0),
                patch.object(volume.os, "umask"),
                patch.object(volume, "initialize_volume", events.initialize),
                patch.object(volume, "drop_privileges", events.drop),
                patch.object(volume, "check_storage", events.check),
                patch.object(volume.os, "execv", events.execute),
            ):
                if failure:
                    with self.assertRaises(OSError):
                        volume.main()
                    events.execute.assert_not_called()
                else:
                    volume.main()
                    self.assertEqual(
                        [item[0] for item in events.mock_calls],
                        ["initialize", "drop", "check", "execute"],
                    )
                    self.assertEqual(events.execute.call_args.args[1][-1], "auth")

    def test_docker_images_use_separate_initializer(self):
        for service in ("auth", "pomerium"):
            dockerfile = (DEPLOY / service / "Dockerfile").read_text()
            self.assertIn(
                '"-I", "/app/volume_entrypoint.py", "' + service + '"', dockerfile
            )
            self.assertNotIn("chown", dockerfile)
            self.assertIn("deploy/volume_entrypoint.py", dockerfile)
        self.assertIn(
            "!deploy/volume_entrypoint.py",
            (DEPLOY.parent / ".dockerignore").read_text(),
        )


class ResolverTests(unittest.TestCase):
    def test_only_valid_ip_nameservers_are_interpolated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resolv.conf"
            path.write_text(
                "# local configuration\nsearch internal\nnameserver 10.0.0.2\n"
                "nameserver fd00::53 # IPv6\n"
            )
            self.assertEqual(renderer.resolver_addresses(path), "10.0.0.2 [fd00::53]")
            for text in ("search internal\n", "nameserver 10.0.0.2;include\n"):
                path.write_text(text)
                with self.subTest(text=text), self.assertRaises(ValueError):
                    renderer.resolver_addresses(path)


class EntrypointTests(unittest.TestCase):
    def test_root_identity_fails_before_render_or_services(self):
        for ids in ((0, 0, 0), (10001, 10001, 0)):
            with (
                self.subTest(ids=ids),
                patch.object(sys, "argv", ["service_entrypoint.py", "auth"]),
                patch.object(entrypoint.os, "getresuid", return_value=ids),
                patch.object(entrypoint, "render") as render,
                patch.object(entrypoint, "supervise") as supervise,
                self.assertRaises(ValueError),
            ):
                entrypoint.main()
            render.assert_not_called()
            supervise.assert_not_called()

    def test_child_environment_removes_unneeded_secrets(self):
        env = {**SYNTHETIC_SECRETS, "PATH": "/synthetic/bin", "PORT": "8080"}
        allowed = {"GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "POMERIUM_CLIENT_SECRET"}
        with patch.dict(os.environ, env, clear=True):
            child = entrypoint.child_environment(allowed)
        self.assertEqual(child, {key: env[key] for key in (*allowed, "PATH", "PORT")})

    def test_auth_main_checks_nginx_and_scopes_each_child_environment(self):
        events = []
        with (
            patch.dict(os.environ, {**AUTH_ENV, **SYNTHETIC_SECRETS}, clear=True),
            patch.object(sys, "argv", ["service_entrypoint.py", "auth"]),
            patch.object(entrypoint.os, "umask") as umask,
            patch.object(entrypoint, "render") as render,
            patch.object(entrypoint.subprocess, "run") as check,
            patch.object(entrypoint, "supervise") as supervise,
        ):
            check.side_effect = lambda *args, **kwargs: events.append("check")
            supervise.side_effect = lambda commands: events.append("supervise") or 0
            self.assertEqual(entrypoint.main(), 0)
        umask.assert_called_once_with(0o077)
        self.assertEqual(render.call_args.args[0], "auth")
        self.assertEqual(events, ["check", "supervise"])
        commands = supervise.call_args.args[0]
        self.assertEqual([command[0][0] for command in commands], ["dex", "nginx"])
        dex_command, dex_env = commands[0]
        router_command, router_env = commands[1]
        self.assertEqual(dex_command[1], "serve")
        self.assertEqual(router_command[-2:], ["-g", "daemon off;"])
        self.assertEqual(
            set(dex_env) & entrypoint.SECRETS,
            {"GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "POMERIUM_CLIENT_SECRET"},
        )
        self.assertFalse(set(router_env) & entrypoint.SECRETS)
        self.assertEqual(check.call_args.kwargs, {"env": router_env, "check": True})
        self.assertEqual(check.call_args.args[0][0], "nginx")
        self.assertIn("-t", check.call_args.args[0])
        self.assertNotIn("-T", check.call_args.args[0])

    def test_pomerium_main_only_passes_pomerium_secrets(self):
        with (
            patch.dict(os.environ, {**POMERIUM_ENV, **SYNTHETIC_SECRETS}, clear=True),
            patch.object(sys, "argv", ["service_entrypoint.py", "pomerium"]),
            patch.object(entrypoint.os, "umask"),
            patch.object(entrypoint, "render") as render,
            patch.object(entrypoint.subprocess, "run") as check,
            patch.object(entrypoint, "supervise", return_value=0) as supervise,
        ):
            self.assertEqual(entrypoint.main(), 0)
        self.assertEqual(render.call_args.args[0], "pomerium")
        check.assert_not_called()
        ((command, env),) = supervise.call_args.args[0]
        self.assertEqual(command[:2], ["pomerium", "--config"])
        self.assertEqual(
            set(env) & entrypoint.SECRETS,
            {"SHARED_SECRET", "COOKIE_SECRET", "SIGNING_KEY", "IDP_CLIENT_SECRET"},
        )

    def test_failed_nginx_validation_does_not_spawn_services(self):
        with (
            patch.object(sys, "argv", ["service_entrypoint.py", "auth"]),
            patch.object(entrypoint.os, "umask"),
            patch.object(entrypoint, "render"),
            patch.object(
                entrypoint.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(1, "nginx"),
            ),
            patch.object(entrypoint, "supervise") as supervise,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                entrypoint.main()
        supervise.assert_not_called()

    def test_startup_exception_does_not_print_sensitive_exception_text(self):
        secret = "synthetic-sensitive-exception-tests-only"
        script = (
            "import runpy, sys; "
            f"sys.path.insert(0, {str(DEPLOY)!r}); "
            "import render_config; "
            "defective = lambda *args: (_ for _ in ()).throw(ValueError("
            f"{secret!r})); "
            "render_config.render = defective; "
            "sys.argv = ['service_entrypoint.py', 'auth']; "
            f"runpy.run_path({str(DEPLOY / 'service_entrypoint.py')!r}, run_name='__main__')"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Service startup failed", result.stderr)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_stop_all_mocked_timeout_is_bounded_and_reaps_killed_child(self):
        # Deliberately mock the ten-second deadline instead of slowing the suite.
        children = [Mock(), Mock()]
        for child in children:
            child.poll.return_value = None
        children[0].wait.side_effect = [subprocess.TimeoutExpired("stuck", 9.8), 0]
        sequence = Mock()
        sequence.attach_mock(children[0], "first")
        sequence.attach_mock(children[1], "second")
        with patch.object(entrypoint.time, "monotonic", side_effect=[10, 10.2, 20.5]):
            entrypoint.stop_all(children)
        calls = sequence.mock_calls
        self.assertLess(
            calls.index(call.first.terminate()),
            calls.index(call.first.wait(timeout=9.8)),
        )
        self.assertLess(
            calls.index(call.second.terminate()),
            calls.index(call.first.wait(timeout=9.8)),
        )
        children[0].kill.assert_called_once_with()
        self.assertEqual(children[0].wait.call_args_list, [call(timeout=9.8), call()])
        children[1].wait.assert_called_once_with(timeout=0.01)
        children[1].kill.assert_not_called()

    def test_stop_all_reaps_already_exited_child_without_signalling(self):
        child = Mock()
        child.poll.return_value = 7
        entrypoint.stop_all([child])
        child.terminate.assert_not_called()
        child.kill.assert_not_called()
        child.wait.assert_called_once()


CHILD_SCRIPT = """
import os
import signal
import sys
import time
from pathlib import Path

root, name = Path(sys.argv[1]), sys.argv[2]
def terminate(signum, frame):
    (root / ('stopped.' + name)).write_text(str(signum))
    sys.exit(0)
signal.signal(signal.SIGTERM, terminate)
(root / ('ready.' + name)).write_text(str(os.getpid()))
while True:
    failure = root / ('exit.' + name)
    if failure.exists():
        sys.exit(int(failure.read_text()))
    time.sleep(0.01)
"""


@unittest.skipUnless(os.name == "posix", "Requires POSIX process signals")
class SupervisorProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.process = None
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        if self.process is None:
            return
        # All test children share an isolated process group, even if orphaned.
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.communicate(timeout=5)

    def start_supervisor(self, before="", after=""):
        script = f"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, {str(DEPLOY)!r})
import service_entrypoint
root = Path({str(self.root)!r})
child_script = {CHILD_SCRIPT!r}
commands = [
    ([sys.executable, '-u', '-c', child_script, str(root), name], dict(os.environ))
    for name in ('first', 'second')
]
handlers = {{sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}}
{before}
try:
    result = service_entrypoint.supervise(commands)
except FileNotFoundError:
    result = 23
assert all(signal.getsignal(sig) == handler for sig, handler in handlers.items())
{after}
sys.exit(result)
"""
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        return self.process

    def wait_for_ready(self, names=("first", "second")):
        assert self.process is not None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all((self.root / ("ready." + name)).exists() for name in names):
                return
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                self.fail(f"Supervisor exited before readiness: {stdout} {stderr}")
            time.sleep(0.01)
        self.fail("Test services did not become ready within five seconds")

    def test_actual_subprocess_sigterm_gracefully_stops_both_children(self):
        process = self.start_supervisor()
        self.wait_for_ready()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        for name in ("first", "second"):
            self.assertEqual(
                (self.root / ("stopped." + name)).read_text(), str(int(signal.SIGTERM))
            )

    def test_actual_required_child_exit_stops_peer_and_returns_failure(self):
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code):
                self.cleanup_processes()
                for path in self.root.iterdir():
                    path.unlink()
                process = self.start_supervisor()
                self.wait_for_ready()
                pending_exit = self.root / "exit.pending"
                pending_exit.write_text(str(exit_code))
                pending_exit.replace(self.root / "exit.first")
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 1, stdout + stderr)
                self.assertIn("A required service exited", stderr)
                self.assertEqual(
                    (self.root / "stopped.second").read_text(), str(int(signal.SIGTERM))
                )

    def test_actual_second_spawn_failure_cleans_first_child(self):
        before = """
real_popen = subprocess.Popen
spawn_count = 0
def spawn(command, **kwargs):
    global spawn_count
    spawn_count += 1
    if spawn_count == 2:
        deadline = time.monotonic() + 5
        while not (root / 'ready.first').exists():
            if time.monotonic() >= deadline:
                raise RuntimeError('First test child did not become ready')
            time.sleep(0.01)
        command = [str(root / 'deliberately-missing-executable')]
    return real_popen(command, **kwargs)
service_entrypoint.subprocess.Popen = spawn
"""
        process = self.start_supervisor(before=before)
        stdout, stderr = process.communicate(timeout=8)
        self.assertEqual(process.returncode, 23, stdout + stderr)
        self.assertTrue((self.root / "ready.first").exists())
        self.assertFalse((self.root / "ready.second").exists())
        self.assertEqual(
            (self.root / "stopped.first").read_text(), str(int(signal.SIGTERM))
        )


if __name__ == "__main__":
    unittest.main()
