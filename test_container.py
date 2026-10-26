# SPDX-License-Identifier: GPL-3.0-or-later
"""Packaging configuration/security and supervisor cleanup regressions."""

import asyncio
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bridge import create_server, http_settings
from container_entrypoint import (
    MANAGED_BINARY,
    MANAGED_PLUGIN_DIR,
    configuration,
    home,
    initialize_home,
    initialize_managed_root,
    managed_settings,
    run,
    stop,
    weechat_command,
)

PRIVATE_ENV = {
    "MCP_AUTH_MODE": "railway-private",
    "RAILWAY_ENVIRONMENT_ID": "synthetic-environment",
    "RAILWAY_SERVICE_ID": "synthetic-bridge-service",
    "RAILWAY_PRIVATE_DOMAIN": "bridge.railway.internal",
    "MCP_HOST": "bridge.railway.internal",
    "MCP_ALLOWED_HOSTS": "mcp.example.test",
}


class ConfigurationTests(unittest.TestCase):
    def test_runtime_modules_are_in_docker_build_context(self):
        root = Path(__file__).parent
        rules = (root / ".dockerignore").read_text().splitlines()
        dockerfile = (root / "Dockerfile").read_text()
        for name in (
            "bridge.py",
            "weechat_mcp.py",
            "native_buffer.py",
            "command_manifest.py",
            "container_entrypoint.py",
            "managed_js.py",
            "managed_http.py",
        ):
            with self.subTest(name=name):
                self.assertIn("!" + name, rules)
                self.assertTrue((root / name).is_file())
                self.assertIn(name, dockerfile)

    def test_managed_build_uses_authoritative_nix_without_replacing_default(self):
        root = Path(__file__).parent
        dockerfile = (root / "Dockerfile").read_text()
        rules = (root / ".dockerignore").read_text().splitlines()
        self.assertTrue(dockerfile.rstrip().endswith("FROM standard AS default"))
        self.assertIn("FROM standard AS managed-js", dockerfile)
        self.assertIn(".#weechat-managed-container", dockerfile)
        self.assertIn("nix-store --query --requisites /managed-result", dockerfile)
        self.assertIn("COPY --from=managed-build /closure/ /", dockerfile)
        self.assertNotIn("cmake ", dockerfile)
        self.assertNotIn("LD_PRELOAD", dockerfile)
        self.assertNotIn("safe_tcl", dockerfile)
        for name in (
            "flake.nix",
            "flake.lock",
            "policy/Cargo.toml",
            "policy/Cargo.lock",
            "policy/managed.rego",
            "policy/example-data.json",
            "native/weechat-js-managed.cpp",
            "native/weechat-js-managed.h",
        ):
            with self.subTest(name=name):
                self.assertIn("!" + name, rules)
                self.assertTrue((root / name).is_file())

    def test_nix_output_builds_an_actual_image_with_locked_python_dependencies(self):
        root = Path(__file__).parent
        image = (root / "nix/image.nix").read_text()
        python = (root / "nix/python-env.nix").read_text()
        self.assertIn("dockerTools.buildLayeredImage", image)
        self.assertIn('tag = "managed-js-nix"', image)
        self.assertIn("${bridgeEnv}/bin/python", image)
        self.assertIn('version("fastmcp") == "4.1.0"', image)
        self.assertIn('version("mcp") == "2.3.0"', image)
        self.assertIn("WEECHAT_MANAGED_POLICY=${sourceRoot}/policy/managed.rego", image)
        self.assertIn("WEECHAT_MANAGED_DATA=${policyData}", image)
        self.assertIn("uv2nix.lib.workspace.loadWorkspace", python)
        self.assertIn("workspace.deps.default", python)
        self.assertNotIn("uv sync", image + python)

    def test_managed_default_keeps_stock_launch_and_trusted_profile_semantics(self):
        self.assertIsNone(managed_settings({}))
        self.assertIsNone(managed_settings({"WEECHAT_MCP_MANAGED_JS": "0"}))
        self.assertIsNone(managed_settings({"WEECHAT_HOME": "/persistent"}))
        self.assertEqual(
            weechat_command("/profile", ["/connect docker"]),
            [
                "weechat-headless",
                "-d",
                "/profile",
                "-p",
                "--stdout",
                "-r",
                "/connect docker",
            ],
        )
        managed = weechat_command("/profile", [], managed=True)
        self.assertEqual(managed, [MANAGED_BINARY, "-d", "/profile", "-p", "--stdout"])
        self.assertNotIn("-a", managed)
        self.assertNotIn("-s", managed)

    def test_managed_settings_restrict_mcp_and_separate_authority_from_sources(self):
        valid = {
            "WEECHAT_MCP_MANAGED_JS": "1",
            "WEECHAT_MCP_FULL_CONTROL": "0",
            "WEECHAT_MANAGED_ROOT": "/data/managed",
            "WEECHAT_MANAGED_POLICY": "/etc/weechat-managed/managed.rego",
            "WEECHAT_MANAGED_DATA": "/etc/weechat-managed/data.json",
            "WEECHAT_HOME": "/data/weechat",
        }
        self.assertEqual(managed_settings(valid)["root"], Path("/data/managed"))
        for key, value in (
            ("WEECHAT_MCP_MANAGED_JS", "true"),
            ("WEECHAT_MCP_FULL_CONTROL", "1"),
            ("WEECHAT_MCP_FULL_CONTROL", ""),
            ("WEECHAT_MANAGED_ROOT", "relative"),
            ("WEECHAT_MANAGED_ROOT", "/data/../managed"),
            ("WEECHAT_MANAGED_ROOT", "/app/python/autoload"),
            ("WEECHAT_MANAGED_ROOT", "/usr/local/lib"),
            ("WEECHAT_MANAGED_ROOT", "/nix/store/runtime"),
            ("WEECHAT_MANAGED_ROOT", "/etc/managed-sources"),
            ("WEECHAT_MANAGED_ROOT", "/opt/weechat-managed/lib"),
            ("WEECHAT_MANAGED_ROOT", "/data/weechat/javascript/autoload"),
            ("WEECHAT_MANAGED_ROOT", "/data"),
            ("WEECHAT_MANAGED_POLICY", "/data/managed/policy.rego"),
            ("WEECHAT_MANAGED_DATA", "/data/managed/data.json"),
            ("WEECHAT_MANAGED_POLICY", "relative.rego"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                managed_settings({**valid, key: value})
        for key in (
            "WEECHAT_MCP_FULL_CONTROL",
            "WEECHAT_MANAGED_ROOT",
            "WEECHAT_MANAGED_POLICY",
            "WEECHAT_MANAGED_DATA",
        ):
            with self.subTest(missing=key), self.assertRaises(ValueError):
                managed_settings({k: v for k, v in valid.items() if k != key})

    def test_managed_loads_matching_plugins_before_mcp(self):
        commands = configuration(
            {"IRC_HOST": "irc.example.org", "WEECHAT_MCP_MANAGED_JS": "1"}
        )
        self.assertEqual(
            commands[:3],
            [
                "/plugin load " + MANAGED_PLUGIN_DIR + "/" + name + ".so"
                for name in ("irc", "python", "javascript")
            ],
        )
        self.assertTrue(commands[3].endswith("/weechat_mcp.py"))
        self.assertEqual(commands[-1], "/connect docker")

    def test_managed_root_private_initialization_rejects_links_and_writable_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "managed"
            initialize_managed_root(root)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            root.chmod(0o777)
            with self.assertRaisesRegex(ValueError, "permissions"):
                initialize_managed_root(root)
            root.chmod(0o700)
            link = Path(temporary) / "link"
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                initialize_managed_root(link)

    def test_invalid_managed_mode_fails_before_privileges_storage_or_children(self):
        with (
            patch.dict(os.environ, {"WEECHAT_MCP_MANAGED_JS": "1"}, clear=True),
            patch("container_entrypoint.initialize_home") as profile,
            patch("container_entrypoint.initialize_managed_root") as sources,
            patch("container_entrypoint.drop_privileges") as privileges,
            patch("container_entrypoint.subprocess.Popen") as spawn,
            self.assertRaisesRegex(ValueError, "FULL_CONTROL"),
        ):
            run()
        profile.assert_not_called()
        sources.assert_not_called()
        privileges.assert_not_called()
        spawn.assert_not_called()

    def test_defaults_tls_scope(self):
        commands = configuration({"IRC_HOST": "irc.example.org"})
        self.assertIn("/server add docker irc.example.org/6697 -tls", commands)
        self.assertIn("/set irc.server.docker.tls_verify on", commands)
        self.assertIn("/set plugins.var.python.mcp.buffers irc.docker.#wchat", commands)

    def test_multiple_channels(self):
        commands = configuration(
            {
                "IRC_HOST": "127.0.0.1",
                "IRC_CHANNELS": "#one,#two",
                "IRC_TLS": "false",
                "IRC_PORT": "6667",
            }
        )
        self.assertIn(
            "/set plugins.var.python.mcp.buffers irc.docker.#one,irc.docker.#two",
            commands,
        )
        self.assertIn("/server add docker 127.0.0.1/6667 -notls", commands)

    def test_reject_injection_and_invalid_config(self):
        for key, value in [
            ("IRC_HOST", ""),
            ("IRC_HOST", "host; /quit"),
            ("IRC_HOST", "${env:SECRET}"),
            ("IRC_PORT", "0"),
            ("IRC_PORT", "65536"),
            ("IRC_TLS", "yes"),
            ("IRC_NICK", "nick\n/quit"),
            ("IRC_CHANNELS", "#a #b"),
            ("IRC_CHANNELS", "#a,#a"),
            ("IRC_CHANNELS", "#${env:SECRET}"),
        ]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                configuration(dict({"IRC_HOST": "localhost"}, **{key: value}))

    def test_http_requires_secret(self):
        for secret in ("", "short", "x" * 32 + "\n", "é" * 32):
            with self.assertRaises(ValueError):
                http_settings({"MCP_AUTH_TOKEN": secret})

    def test_http_auth_and_bind_defaults(self):
        secret = "test-only-not-a-real-secret-123456789"
        verifier, settings = http_settings({"MCP_AUTH_TOKEN": secret})
        assert verifier is not None
        self.assertEqual(settings["host"], "127.0.0.1")
        self.assertFalse(settings["stateless_http"])
        self.assertTrue(settings["host_origin_protection"])
        self.assertIsNone(asyncio.run(verifier.verify_token("wrong")))
        self.assertIsNotNone(asyncio.run(verifier.verify_token(secret)))
        self.assertNotIn(secret, repr(verifier.__dict__))

    def test_railway_port_and_hostname(self):
        _, settings = http_settings(
            {
                "MCP_AUTH_TOKEN": "x" * 32,
                "PORT": "4321",
                "MCP_HOST": "0.0.0.0",
                "MCP_ALLOWED_HOSTS": "service.example.org",
            }
        )
        self.assertEqual(settings["port"], 4321)
        self.assertIn("service.example.org", settings["allowed_hosts"])

    def test_railway_private_mode_needs_no_bearer_and_preserves_host_protection(self):
        auth, settings = http_settings(PRIVATE_ENV)
        self.assertIsNone(auth)
        self.assertEqual(settings["host"], "bridge.railway.internal")
        self.assertTrue(settings["host_origin_protection"])
        self.assertFalse(settings["stateless_http"])
        self.assertEqual(
            settings["allowed_hosts"],
            ["localhost:*", "127.0.0.1:*", "[::1]:*", "mcp.example.test"],
        )

    def test_railway_private_mode_is_explicit_and_guarded(self):
        for key in PRIVATE_ENV:
            env = dict(PRIVATE_ENV)
            del env[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                http_settings(env)
        for key, value in (
            ("MCP_AUTH_MODE", "none"),
            ("MCP_AUTH_MODE", ""),
            ("MCP_AUTH_MODE", "bearer"),
            ("MCP_HOST", "0.0.0.0"),
            ("MCP_HOST", "::"),
            ("MCP_HOST", "127.0.0.1"),
            ("MCP_HOST", "other.railway.internal"),
            ("RAILWAY_PRIVATE_DOMAIN", "bridge.example.test"),
            ("RAILWAY_PRIVATE_DOMAIN", "bridge.railway.internal.attacker.test"),
            ("RAILWAY_ENVIRONMENT_ID", " "),
            ("RAILWAY_PUBLIC_DOMAIN", "bridge.up.railway.app"),
            ("RAILWAY_TCP_PROXY_DOMAIN", "test.proxy.rlwy.net"),
            ("MCP_ALLOWED_HOSTS", "*"),
            ("MCP_ALLOWED_HOSTS", "mcp.example.test,*"),
            ("PORT", "0"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                http_settings({**PRIVATE_ENV, key: value})

    def test_later_boot_skips_environment_setup(self):
        env = {"IRC_HOST": "irc.example.org", "IRC_CHANNELS": "#one"}
        first = configuration(env, first_boot=True)
        later = configuration(env, first_boot=False)
        self.assertIn("/server add docker irc.example.org/6697 -tls", first)
        self.assertNotIn("/server add docker irc.example.org/6697 -tls", later)
        self.assertFalse(any(c.startswith("/set ") for c in later))
        self.assertEqual(later[0], "/plugin load irc")
        self.assertEqual(later[-1], "/connect docker")
        self.assertTrue(any(c.startswith("/python load ") for c in later))

    def test_home_validation(self):
        self.assertIsNone(home({}))
        self.assertIsNone(home({"WEECHAT_HOME": ""}))
        self.assertEqual(home({"WEECHAT_HOME": "/data/weechat"}), Path("/data/weechat"))
        for value in ("relative/path", "/data/../etc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                home({"WEECHAT_HOME": value})

    def test_initialize_home_detects_first_boot_and_clears_stale_socket(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "weechat"
            self.assertTrue(initialize_home(path, drop=lambda: None))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            (path / "weechat.conf").write_text("")
            (path / "mcp").mkdir()
            (path / "mcp" / "bridge.sock").write_text("")
            self.assertFalse(initialize_home(path, drop=lambda: None))
            self.assertFalse((path / "mcp" / "bridge.sock").exists())
            path.chmod(0o777)
            with self.assertRaises(ValueError):
                initialize_home(path, drop=lambda: None)

    def test_stop_escalates_and_reaps(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("child", 5), 0]
        stop(child)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 2)

    def test_stop_already_exited(self):
        child = Mock()
        child.poll.return_value = 0
        stop(child)
        child.terminate.assert_not_called()


class ManagedImageSmokeTests(unittest.TestCase):
    def test_embedded_probe_and_harness_compile(self):
        from docker_managed_js_test import HARNESS, PROBE

        compile(PROBE, "<embedded WeeChat probe>", "exec")
        compile("PROBE = " + repr(PROBE) + "\n" + HARNESS, "<image harness>", "exec")

    def test_nix_image_build_load_and_explicit_compatibility_paths(self):
        import docker_managed_js_test as smoke

        for args, executable in (
            ([], "nix"),
            (["--dockerfile"], "docker"),
            (["--skip-build"], "docker"),
        ):
            with (
                self.subTest(args=args),
                patch("sys.argv", ["docker_managed_js_test.py", *args]),
                patch.object(smoke.subprocess, "run") as execute,
                patch("builtins.print"),
            ):
                smoke.main()
                self.assertEqual(execute.call_args_list[0].args[0][0], executable)
                if not args:
                    self.assertIn(
                        ".#managed-js-image", execute.call_args_list[0].args[0]
                    )
                    self.assertEqual(execute.call_args_list[1].args[0][1], "load")
                if args == ["--skip-build"]:
                    self.assertEqual(execute.call_count, 1)
                command = execute.call_args_list[-1].args[0]
                self.assertIn("--network", command)
                self.assertIn("none", command)
                self.assertIn("/app/.venv/bin/python", command)


class HTTPAuthenticationTests(unittest.TestCase):
    def initialize(self, env, headers):
        from starlette.testclient import TestClient

        from test_mcp import FakeBackend

        auth, settings = http_settings(env)
        del settings["host"], settings["port"]
        app = create_server(FakeBackend(), auth=auth).http_app(**settings)
        with TestClient(app) as client:
            return client.post(
                "/mcp",
                headers={"Accept": "application/json, text/event-stream", **headers},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2026-07-28",
                        "capabilities": {},
                        "clientInfo": {"name": "private-mode-test", "version": "1"},
                    },
                },
            )

    def test_private_mode_accepts_no_token_but_rejects_wrong_host_and_origin(self):
        headers = {"Host": "mcp.example.test"}
        response = self.initialize(PRIVATE_ENV, headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn('"serverInfo":{"name":"weechat-mcp"', response.text)
        self.assertEqual(
            self.initialize(PRIVATE_ENV, {"Host": "wrong.example.test"}).status_code,
            421,
        )
        self.assertEqual(
            self.initialize(
                PRIVATE_ENV, {**headers, "Origin": "https://wrong.example.test"}
            ).status_code,
            403,
        )

    def test_default_http_still_rejects_missing_and_wrong_bearer(self):
        token = "synthetic-test-only-backend-token-12345"
        env = {"MCP_AUTH_TOKEN": token, "MCP_ALLOWED_HOSTS": "mcp.example.test"}
        for authorization in (None, "Bearer wrong", "Bearer " + token):
            headers = {"Host": "mcp.example.test"}
            if authorization is not None:
                headers["Authorization"] = authorization
            with self.subTest(authorization=authorization):
                self.assertEqual(
                    self.initialize(env, headers).status_code,
                    200 if authorization == "Bearer " + token else 401,
                )
