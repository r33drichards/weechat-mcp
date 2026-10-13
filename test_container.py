# SPDX-License-Identifier: GPL-3.0-or-later
"""Packaging configuration/security and supervisor cleanup regressions."""

import asyncio
import subprocess
import unittest
from unittest.mock import Mock

from bridge import http_settings
from container_entrypoint import configuration, stop


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
