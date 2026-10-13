# SPDX-License-Identifier: GPL-3.0-or-later
"""Packaging configuration/security and supervisor cleanup regressions."""

import asyncio
import subprocess
import unittest
from unittest.mock import Mock

from bridge import create_server, http_settings
from container_entrypoint import configuration, stop

PRIVATE_ENV = {
    "MCP_AUTH_MODE": "railway-private",
    "RAILWAY_ENVIRONMENT_ID": "synthetic-environment",
    "RAILWAY_SERVICE_ID": "synthetic-bridge-service",
    "RAILWAY_PRIVATE_DOMAIN": "bridge.railway.internal",
    "MCP_HOST": "bridge.railway.internal",
    "MCP_ALLOWED_HOSTS": "mcp.example.test",
}


class ConfigurationTests(unittest.TestCase):
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
