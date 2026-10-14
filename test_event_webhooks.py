# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline security regressions: every DNS answer, socket and TLS connection is fake."""

import asyncio
import base64
import socket
import ssl
import unittest
from unittest.mock import AsyncMock, Mock, patch

import event_webhooks as webhooks

SECRET = "whsec_" + base64.b64encode(bytes(range(32))).decode("ascii")
SECOND_SECRET = "whsec_" + base64.b64encode(b"x" * 32).decode("ascii")
BODY = '{"text":"snowman ☃", "n":1}'.encode()
URL = "https://callback.example.test/path?token=private-value"


def address(value, port=443):
    if ":" in value:
        return (
            socket.AF_INET6,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (value, port, 0, 0),
        )
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (value, port))


class SigningTests(unittest.TestCase):
    def test_known_standard_webhooks_vector(self):
        # Independently computed with OpenSSL HMAC-SHA256, then base64 encoded.
        headers = webhooks.signed_headers(
            "evt_fixed", BODY, SECRET, "sub_one", 1700000000
        )
        self.assertEqual(
            headers,
            {
                "Content-Type": "application/json",
                "webhook-id": "evt_fixed",
                "webhook-timestamp": "1700000000",
                "webhook-signature": "v1,lwl0Hmq2O0mop8RYKxbL35W+eB2Dsuyx8K+tNJxLAf8=",
                "X-MCP-Subscription-Id": "sub_one",
            },
        )
        changed = webhooks.signed_headers(
            "evt_fixed", BODY + b"\n", SECRET, "sub_one", 1700000000
        )
        self.assertNotEqual(headers["webhook-signature"], changed["webhook-signature"])

    def test_rotation_signs_every_key_and_fresh_timestamp(self):
        current = webhooks.signed_headers("evt_fixed", BODY, SECRET, "sub", 1700000000)
        previous = webhooks.signed_headers(
            "evt_fixed", BODY, SECOND_SECRET, "sub", 1700000000
        )
        both = webhooks.signed_headers(
            "evt_fixed", BODY, [SECRET, SECOND_SECRET], "sub", 1700000000
        )
        self.assertEqual(
            both["webhook-signature"],
            current["webhook-signature"] + " " + previous["webhook-signature"],
        )
        with patch.object(webhooks.time, "time", return_value=1700000001.9):
            fresh = webhooks.signed_headers("evt_fixed", BODY, SECRET, "sub")
        self.assertEqual(fresh["webhook-timestamp"], "1700000001")
        self.assertNotEqual(fresh["webhook-signature"], current["webhook-signature"])

    def test_key_bounds_and_strict_base64(self):
        for size in (24, 25, 32, 63, 64):
            with self.subTest(size=size):
                key = b"x" * size
                self.assertEqual(
                    webhooks.validate_secret("whsec_" + base64.b64encode(key).decode()),
                    key,
                )
        bad = [
            None,
            b"whsec_bytes",
            "",
            SECRET[6:],
            "whsec_" + "A" * 32 + "\n",
            "whsec_" + base64.b64encode(b"x" * 23).decode(),
            "whsec_" + base64.b64encode(b"x" * 65).decode(),
            "whsec_" + base64.b64encode(b"x" * 25).decode().rstrip("="),
            "whsec_" + base64.urlsafe_b64encode(b"\xff" * 32).decode(),
            # Same decoded bytes but nonzero padding bits are non-canonical.
            "whsec_" + base64.b64encode(b"x" * 25).decode()[:-3] + "B==",
            SECRET + "=",
        ]
        for secret in bad:
            with self.subTest(secret=repr(secret)):
                with self.assertRaises(webhooks.WebhookError) as error:
                    webhooks.validate_secret(secret)  # ty: ignore[invalid-argument-type]
                self.assertEqual(str(error.exception), "invalid_secret")

    def test_signing_rejects_payload_and_header_injection(self):
        for identifier in ("evil\r\nHost: localhost", "has space", "", "é"):
            with self.subTest(identifier=identifier):
                with self.assertRaisesRegex(webhooks.WebhookError, "invalid_headers"):
                    webhooks.signed_headers(identifier, BODY, SECRET, "sub")
                with self.assertRaisesRegex(webhooks.WebhookError, "invalid_headers"):
                    webhooks.signed_headers("id", BODY, SECRET, identifier)
        for timestamp in (-1, True, "123", 2**63):
            with self.subTest(timestamp=timestamp):
                with self.assertRaisesRegex(webhooks.WebhookError, "invalid_headers"):
                    webhooks.signed_headers("id", BODY, SECRET, "sub", timestamp)  # ty: ignore[invalid-argument-type]
        for keys in ([], [SECRET] * 9, None, ["bad-key"]):
            with self.subTest(keys=keys):
                with self.assertRaisesRegex(webhooks.WebhookError, "invalid_secret"):
                    webhooks.signed_headers("id", BODY, keys, "sub")  # ty: ignore[invalid-argument-type]
        with self.assertRaisesRegex(webhooks.WebhookError, "invalid_payload"):
            webhooks.signed_headers("id", "not bytes", SECRET, "sub")  # ty: ignore[invalid-argument-type]
        with self.assertRaisesRegex(webhooks.WebhookError, "payload_too_large"):
            webhooks.signed_headers(
                "id", b"x" * (webhooks.MAX_PAYLOAD_BYTES + 1), SECRET, "sub"
            )


class URLTests(unittest.TestCase):
    def test_https_urls_keep_original_identity(self):
        for url in (
            URL,
            "https://Example.test:443/",
            "https://8.8.8.8:8443/callback",
            "https://[2606:4700:4700::1111]/callback",
            "https://xn--bcher-kva.example/path",
        ):
            with self.subTest(url=url):
                self.assertEqual(webhooks.validate_url(url), url)

    def test_invalid_urls(self):
        for url in (
            None,
            b"https://example.com",
            "",
            "http://example.com/",
            "file:///etc/passwd",
            "https://user:password@example.com/",
            "https://user@example.com/",
            "https://example.com/#fragment",
            "https://example.com/#",
            "https://example.com:0/",
            "https://example.com:65536/",
            "https://example.com:/",
            "https://example.com\r\nHost: localhost/",
            "https://example.com\t/",
            "https://example.com/back\\slash",
            "https://example.com/a b",
            " https://example.com/",
            "https://[fe80::1%25eth0]/",
            "https://%31%32%37.0.0.1/",
            "https://[8.8.8.8]/",
            "https://-bad.example/",
            "https://bad..example/",
            "https://example.com../",
            "https://éxample.com/",
            "https://example.com/" + "a" * 4096,
        ):
            with self.subTest(url=url):
                with self.assertRaises(webhooks.WebhookError):
                    webhooks.validate_url(url)  # ty: ignore[invalid-argument-type]

    def test_nonpublic_literal_addresses_blocked(self):
        for ip in (
            "127.0.0.1",
            "0.0.0.0",
            "10.1.2.3",
            "100.64.0.1",
            "169.254.169.254",
            "172.16.0.1",
            "192.168.0.1",
            "192.0.0.9",
            "192.88.99.1",
            "192.0.2.1",
            "198.18.0.1",
            "198.51.100.1",
            "203.0.113.1",
            "224.0.0.1",
            "255.255.255.255",
            "::",
            "::1",
            "fc00::1",
            "fe80::1",
            "ff02::1",
            "2001:db8::1",
            "3fff::1",
            "::ffff:127.0.0.1",
            "::ffff:8.8.8.8",
            "64:ff9b::7f00:1",
            "2002:7f00:1::1",
            "2001::1",
        ):
            with self.subTest(ip=ip):
                host = f"[{ip}]" if ":" in ip else ip
                with self.assertRaisesRegex(webhooks.WebhookError, "blocked_address"):
                    webhooks.validate_url(f"https://{host}/")


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sender = webhooks.SecureWebhookSender()
        self.target = webhooks._target(URL)
        self.fake_loop = Mock()
        self.fake_loop.getaddrinfo = AsyncMock(return_value=[address("8.8.8.8")])
        self.fake_loop.sock_connect = AsyncMock()
        self.fake_socket = Mock()
        self.fake_open = AsyncMock(return_value=(Mock(), Mock()))

    async def connect(self):
        with (
            patch.object(asyncio, "get_running_loop", return_value=self.fake_loop),
            patch.object(webhooks.socket, "socket", return_value=self.fake_socket),
            patch.object(asyncio, "open_connection", self.fake_open),
        ):
            return await self.sender._connect(self.target)

    async def test_numeric_pinning_and_original_tls_hostname(self):
        await self.connect()
        self.fake_loop.getaddrinfo.assert_awaited_once_with(
            "callback.example.test",
            443,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        self.fake_loop.sock_connect.assert_awaited_once_with(
            self.fake_socket, ("8.8.8.8", 443)
        )
        kwargs = self.fake_open.call_args.kwargs
        self.assertNotIn("host", kwargs)
        self.assertEqual(kwargs["server_hostname"], "callback.example.test")
        self.assertIs(kwargs["sock"], self.fake_socket)
        self.assertTrue(kwargs["ssl"].check_hostname)
        self.assertEqual(kwargs["ssl"].verify_mode, ssl.CERT_REQUIRED)
        self.fake_socket.setblocking.assert_called_once_with(False)

    async def test_ipv6_numeric_pinning(self):
        self.fake_loop.getaddrinfo.return_value = [address("2606:4700:4700::1111")]
        await self.connect()
        self.fake_loop.sock_connect.assert_awaited_once_with(
            self.fake_socket, ("2606:4700:4700::1111", 443, 0, 0)
        )

    async def test_mixed_dns_answers_never_connect(self):
        for private in (
            "127.0.0.1",
            "10.0.0.1",
            "::1",
            "::ffff:127.0.0.1",
            "169.254.169.254",
        ):
            with self.subTest(private=private):
                self.fake_loop.getaddrinfo.return_value = [
                    address("8.8.8.8"),
                    address(private),
                ]
                with self.assertRaisesRegex(webhooks.WebhookError, "blocked_address"):
                    await self.connect()
        self.fake_loop.sock_connect.assert_not_awaited()
        self.fake_open.assert_not_awaited()

    async def test_dns_rebinding_rechecked_on_each_request(self):
        self.fake_loop.getaddrinfo.side_effect = [
            [address("8.8.8.8")],
            [address("127.0.0.1")],
        ]
        await self.connect()
        with self.assertRaisesRegex(webhooks.WebhookError, "blocked_address"):
            await self.connect()
        self.assertEqual(self.fake_loop.getaddrinfo.await_count, 2)
        self.assertEqual(self.fake_loop.sock_connect.await_count, 1)
        self.assertEqual(self.fake_open.await_count, 1)

    async def test_no_dns_answers(self):
        self.fake_loop.getaddrinfo.return_value = []
        with self.assertRaisesRegex(webhooks.WebhookError, "dns_error"):
            await self.connect()
        self.fake_loop.sock_connect.assert_not_awaited()

    async def test_tls_failure_closes_socket_without_retry(self):
        self.fake_loop.getaddrinfo.return_value = [
            address("8.8.8.8"),
            address("1.1.1.1"),
        ]
        self.fake_open.side_effect = ssl.SSLCertVerificationError(
            "private certificate detail"
        )
        with self.assertRaises(ssl.SSLCertVerificationError):
            await self.connect()
        self.fake_socket.close.assert_called_once()
        self.assertEqual(self.fake_loop.sock_connect.await_count, 1)

    async def test_failure_can_try_another_prevalidated_ip(self):
        self.fake_loop.getaddrinfo.return_value = [
            address("8.8.8.8"),
            address("1.1.1.1"),
        ]
        self.fake_loop.sock_connect.side_effect = [ConnectionRefusedError(), None]
        await self.connect()
        self.assertEqual(self.fake_loop.sock_connect.await_count, 2)
        self.assertEqual(self.fake_loop.getaddrinfo.await_count, 1)

    async def test_cancellation_closes_socket(self):
        self.fake_loop.sock_connect.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.connect()
        self.fake_socket.close.assert_called_once()


class ResponseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sender = webhooks.SecureWebhookSender(timeout=0.1, max_response_bytes=32)
        self.writer = Mock()
        self.writer.drain = AsyncMock()
        self.headers = webhooks.signed_headers(
            "evt_fixed", BODY, SECRET, "sub", 1700000000
        )

    async def post(self, response):
        reader = asyncio.StreamReader(limit=webhooks.MAX_HEADER_LINE)
        reader.feed_data(response)
        reader.feed_eof()
        with patch.object(
            self.sender, "_connect", AsyncMock(return_value=(reader, self.writer))
        ):
            return await self.sender.post(URL, BODY, self.headers)

    async def test_body_bytes_unchanged_and_headers_correct(self):
        status, body = await self.post(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"
        )
        self.assertEqual((status, body), (200, b"{}"))
        request = self.writer.write.call_args.args[0]
        head, actual_body = request.split(b"\r\n\r\n", 1)
        self.assertEqual(actual_body, BODY)
        self.assertIn(b"POST /path?token=private-value HTTP/1.1\r\n", head)
        self.assertIn(b"Host: callback.example.test", head)
        self.assertIn(f"Content-Length: {len(BODY)}".encode(), head)
        self.assertIn(
            b"webhook-signature: v1,lwl0Hmq2O0mop8RYKxbL35W+eB2Dsuyx8K+tNJxLAf8=", head
        )
        self.assertIn(b"Connection: close", head)
        self.writer.close.assert_called_once()
        self.writer.transport.abort.assert_called_once()

    async def test_statuses_returned_for_manager_retry_policy(self):
        for status in (202, 204, 205, 400, 410, 413, 429, 500, 503):
            with self.subTest(status=status):
                self.assertEqual(
                    await self.post(
                        f"HTTP/1.1 {status} Result\r\nContent-Length: 0\r\n\r\n".encode()
                    ),
                    (status, b""),
                )

    async def test_redirects_rejected_without_reading_body_or_following(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                response = f"HTTP/1.1 {status} Redirect\r\nLocation: http://127.0.0.1/private\r\nContent-Length: 999\r\n\r\n".encode()
                with self.assertRaisesRegex(webhooks.WebhookError, "redirect_blocked"):
                    await self.post(response)

    async def test_bounded_chunked_response(self):
        response = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2;ext=yes\r\n{}\r\n0\r\nX-Extra: value\r\n\r\n"
        self.assertEqual(await self.post(response), (200, b"{}"))

    async def test_eof_body_and_informational_response(self):
        self.assertEqual(
            await self.post(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.0 200 OK\r\n\r\n{}"),
            (200, b"{}"),
        )

    async def test_oversize_response_in_all_framing_modes(self):
        responses = [
            b"HTTP/1.1 200 OK\r\nContent-Length: 33\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 99999999999999999\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n21\r\n",
            b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * 33,
        ]
        for response in responses:
            with self.subTest(response=response):
                with self.assertRaisesRegex(
                    webhooks.WebhookError, "response_too_large"
                ):
                    await self.post(response)

    async def test_invalid_response_framing(self):
        for response in (
            b"HTTP/2 200 OK\r\n\r\n",
            b"HTTP/1.1 200 OK\n\n",
            b"HTTP/1.1 101 Switching\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}",
            b"HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip, chunked\r\n\r\n",
            b"HTTP/1.1 200 OK\r\n Folded: no\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nHeader: \x00\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n{}",
            b"HTTP/1.1 204 Empty\r\nContent-Length: 2\r\n\r\n{}",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n-1\r\n",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}xx",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nContent-Length: 5\r\n\r\n",
            (b"HTTP/1.1 100 Continue\r\n\r\n" * 7),
        ):
            with self.subTest(response=response):
                with self.assertRaisesRegex(webhooks.WebhookError, "invalid_response"):
                    await self.post(response)

    async def test_headers_and_lines_are_bounded(self):
        for response in (
            b"HTTP/1.1 200 OK\r\nX-Long: "
            + b"a" * webhooks.MAX_HEADER_LINE
            + b"\r\n\r\n",
            b"HTTP/1.1 200 OK\r\n" + b"X-Header: value\r\n" * 101 + b"\r\n",
            b"HTTP/1.1 200 OK\r\n"
            + (b"X-Header: " + b"a" * 8000 + b"\r\n") * 5
            + b"\r\n",
        ):
            with self.assertRaises(webhooks.WebhookError):
                await self.post(response)

    async def test_request_injection_and_payload_size_fail_before_network(self):
        for headers in (
            {"Host": "localhost"},
            {"Transfer-Encoding": "chunked"},
            {"Content-Length": "1"},
            {"X-Test\r\nHost": "localhost"},
            {"X-Test": "value\r\nHost: localhost"},
            {"Connection": "keep-alive"},
            {"Proxy-Authorization": "secret"},
            {"X-Test": "one", "x-test": "two"},
            {"X-Test": "é"},
        ):
            with self.subTest(headers=headers):
                with patch.object(self.sender, "_connect", AsyncMock()) as connect:
                    with self.assertRaisesRegex(
                        webhooks.WebhookError, "invalid_headers"
                    ):
                        await self.sender.post(URL, BODY, headers)
                    connect.assert_not_awaited()
        with patch.object(self.sender, "_connect", AsyncMock()) as connect:
            with self.assertRaisesRegex(webhooks.WebhookError, "payload_too_large"):
                await self.sender.post(URL, b"x" * (webhooks.MAX_PAYLOAD_BYTES + 1), {})
            connect.assert_not_awaited()

    async def test_total_deadline_covers_connect_and_stalled_response(self):
        async def never_connect(_target):
            await asyncio.sleep(10)

        with patch.object(self.sender, "_connect", never_connect):
            with self.assertRaisesRegex(webhooks.WebhookError, "timeout"):
                await self.sender.post(URL, BODY, self.headers)
        reader = asyncio.StreamReader()
        reader.feed_data(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n")
        with patch.object(
            self.sender, "_connect", AsyncMock(return_value=(reader, self.writer))
        ):
            with self.assertRaisesRegex(webhooks.WebhookError, "timeout"):
                await self.sender.post(URL, BODY, self.headers)
        self.writer.close.assert_called_once()
        self.writer.transport.abort.assert_called_once()

    async def test_network_errors_are_sanitized(self):
        for failure, expected in (
            (socket.gaierror("sensitive hostname"), "dns_error"),
            (ssl.SSLError("private TLS hostname"), "tls_error"),
            (
                ConnectionRefusedError("secret URL and network details"),
                "connection_failed",
            ),
            (TimeoutError("secret timeout detail"), "timeout"),
        ):
            with self.subTest(expected=expected):
                with patch.object(
                    self.sender, "_connect", AsyncMock(side_effect=failure)
                ):
                    with self.assertRaises(webhooks.WebhookError) as error:
                        await self.sender.post(URL, BODY, self.headers)
                self.assertEqual(str(error.exception), expected)
                self.assertEqual(error.exception.reason, expected)
                self.assertIsNone(error.exception.__cause__)
                self.assertTrue(error.exception.__suppress_context__)

    async def test_proxy_environment_is_not_used(self):
        with patch.dict(
            "os.environ",
            {
                "HTTPS_PROXY": "http://127.0.0.1:8080",
                "ALL_PROXY": "http://127.0.0.1:8080",
            },
        ):
            self.assertEqual(
                await self.post(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"),
                (200, b""),
            )
        request = self.writer.write.call_args.args[0]
        self.assertTrue(request.startswith(b"POST /path?"))
        self.assertNotIn(b"CONNECT ", request)


if __name__ == "__main__":
    unittest.main()
