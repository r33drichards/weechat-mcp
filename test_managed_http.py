# SPDX-License-Identifier: GPL-3.0-or-later
"""Native-hook HTTP policy regressions; no external network or alternate client."""

import json
import socket
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import managed_http as http
import weechat_mcp

URL = "https://public.example.test/path?x=1"
PUBLIC = "93.184.216.34"


def answer(ip=PUBLIC, port=443):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    address = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)


class FakeWeeChat:
    WEECHAT_RC_OK = 0
    WEECHAT_HOOK_PROCESS_RUNNING = -1

    def __init__(self):
        self.processes = []
        self.urls = []
        self.unhooked = []
        self.options = {
            key: http._REQUIRED_CONSTANTS.get(key, "") for key in http._REQUIRED_OPTIONS
        }
        self.index = -1
        self.freed = False

    def infolist_get(self, *args):
        self.index = -1
        self.names = sorted(self.options)
        return "info"

    def infolist_next(self, _info):
        self.index += 1
        return self.index < len(self.names)

    def infolist_string(self, _info, field):
        name = self.names[self.index]
        return name.upper() if field == "name" else self.options[name].upper()

    def infolist_free(self, _info):
        self.freed = True

    def hook_process(self, *args):
        self.processes.append(args)
        return "dns-" + str(len(self.processes))

    def hook_url(self, *args):
        self.urls.append(args)
        return "url-" + str(len(self.urls))

    def unhook(self, hook):
        self.unhooked.append(hook)


class PolicyTests(unittest.TestCase):
    def test_public_ip_literals_and_dns_names(self):
        for url in (
            URL,
            "http://93.184.216.34:8080/",
            "https://[2606:4700:4700::1111]/",
        ):
            with self.subTest(url=url):
                self.assertTrue(http._url(url))

    def test_url_rejections(self):
        for url in (
            "file:///etc/passwd",
            "gopher://public.example/",
            "HTTP://public.example/",
            "https://Public.example/",
            "https://user:pass@public.example/",
            "https://public.example/#fragment",
            "https://public.example:0/",
            "https://public.example:0443/",
            "https://public.example:/",
            "https://public.example./",
            "https://local/",
            "https://127.1/",
            "https://2130706433/",
            "https://0x7f000001/",
            "https://0177.0.0.1/",
            "https://public.example\\@127.0.0.1/",
            "https://public%2eexample/",
            "https://public.example/\r\nInjected: yes",
            "https://public.example/%zz",
            "https://[fe80::1%25eth0]/",
            "https://[2606:4700:4700:0:0:0:0:1111]/",
            " https://public.example/",
            "https://éxample.test/",
            URL + "a" * 2048,
        ):
            with self.subTest(url=url):
                with self.assertRaises(http.HttpError):
                    http._url(url)

    def test_nonpublic_and_transition_addresses(self):
        for ip in (
            "0.0.0.0",
            "10.0.0.1",
            "100.64.0.1",
            "127.0.0.1",
            "169.254.169.254",
            "172.16.0.1",
            "192.0.0.9",
            "192.0.2.1",
            "192.88.99.1",
            "192.168.1.1",
            "198.18.0.1",
            "198.51.100.1",
            "203.0.113.1",
            "224.0.0.1",
            "240.0.0.1",
            "255.255.255.255",
            "::",
            "::1",
            "::ffff:8.8.8.8",
            "64:ff9b::808:808",
            "64:ff9b:1::1",
            "100::1",
            "2001::1",
            "2001:db8::1",
            "2002:808:808::1",
            "3fff::1",
            "fc00::1",
            "fe80::1",
            "ff02::1",
        ):
            with self.subTest(ip=ip):
                with self.assertRaises(http.HttpError):
                    http._public_address(ip)

    def test_dns_answer_shapes(self):
        bad = [
            [answer()] * 33,
            [(socket.AF_UNIX, socket.SOCK_STREAM, 0, "", ("/tmp/file", 443))],
            [(socket.AF_INET, socket.SOCK_DGRAM, 0, "", (PUBLIC, 443))],
            [answer(port=80)],
            [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("2606:4700::1", 443, 0, 1))],
            [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("2606:4700::1", 443))],
        ]
        for records in bad:
            with self.subTest(records=records), self.assertRaises(http.HttpError):
                http._checked_addresses(records, 443)

    def test_dns_worker_has_fixed_data_only_function(self):
        data = json.dumps({"id": 1, "host": "public.example.test", "port": 443})
        with patch.object(
            http.socket, "getaddrinfo", return_value=[answer()]
        ) as resolve:
            self.assertEqual(
                json.loads(http.dns_worker(data)), json.loads(json.dumps([answer()]))
            )
        resolve.assert_called_once_with(
            "public.example.test",
            443,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
        )
        for records in ([answer(), answer("127.0.0.1")], [answer("::1")], []):
            with patch.object(http.socket, "getaddrinfo", return_value=records):
                self.assertEqual(http.dns_worker(data), "null")
        with patch.object(http.socket, "getaddrinfo") as resolve:
            self.assertEqual(http.dns_worker('{"host":"localhost","port":443}'), "null")
            resolve.assert_not_called()


class NativeHTTPTests(unittest.TestCase):
    def setUp(self):
        self.w = FakeWeeChat()
        self.results = []
        with patch.object(http, "_loaded_curl_version", return_value=0x080400):
            self.client = http.NativeHTTP(self.w, self.results.append)

    def resolve(self, records=None, ident=1):
        process = next(
            item for item in self.w.processes if json.loads(item[3])["id"] == ident
        )
        self.client.dns_done(
            process[3],
            process[0],
            0,
            json.dumps(records if records is not None else [answer()]),
            "",
        )

    def finish(self, output=None, ident=1):
        url_hook = next(item for item in self.w.urls if item[4] == str(ident))
        self.client.url_done(
            str(ident),
            url_hook[0],
            output
            or {
                "response_code": "200",
                "output": "ok",
                "headers": "Content-Type: text/plain\r\n",
            },
        )

    def test_requires_loaded_recent_curl_and_all_native_options(self):
        for version in (0, 0x080300):
            with (
                patch.object(http, "_loaded_curl_version", return_value=version),
                self.assertRaisesRegex(http.HttpError, "libcurl_8_4_required"),
            ):
                http.NativeHTTP(self.w, self.results.append)
        for key in http._REQUIRED_OPTIONS:
            w = FakeWeeChat()
            del w.options[key]
            with (
                patch.object(http, "_loaded_curl_version", return_value=0x080400),
                self.assertRaisesRegex(
                    http.HttpError, "native_url_options_unavailable"
                ),
            ):
                http.NativeHTTP(w, self.results.append)
            self.assertTrue(w.freed)
        w = FakeWeeChat()
        w.options["netrc"] = "optional,required"
        with (
            patch.object(http, "_loaded_curl_version", return_value=0x080400),
            self.assertRaises(http.HttpError),
        ):
            http.NativeHTTP(w, self.results.append)

    def test_native_worker_disables_signals_and_requires_support(self):
        self.client.request(1, "GET", URL)
        self.resolve()
        self.assertEqual(self.w.urls[-1][1]["nosignal"], "1")
        w = FakeWeeChat()
        del w.options["nosignal"]
        with (
            patch.object(http, "_loaded_curl_version", return_value=0x080400),
            self.assertRaisesRegex(http.HttpError, "native_url_options_unavailable"),
        ):
            http.NativeHTTP(w, self.results.append)

    def test_synchronous_native_thread_failure_does_not_leave_stale_handle(self):
        def fail_in_hook(url, _options, _timeout, _callback, data):
            self.client.url_done(
                data, url, {"error_code": "100", "error": "pthread failure"}
            )
            return "already-freed-native-handle"

        self.client.request(1, "POST", URL)
        with patch.object(self.w, "hook_url", side_effect=fail_in_hook):
            self.resolve()
        self.assertEqual(
            self.results,
            [{"id": 1, "error": "transport_failed", "outcome": "uncertain"}],
        )
        self.assertFalse(self.client.pending)
        self.client.close()
        self.assertNotIn("already-freed-native-handle", self.w.unhooked)

    def test_exact_native_dns_and_url_hooks_pin_original_hostname(self):
        self.client.request(1, "GET", URL)
        self.assertEqual(len(self.w.processes), 1)
        command, timeout, callback, data = self.w.processes[0]
        self.assertEqual(
            (command, timeout, callback),
            ("func:managed_dns_worker", 10000, "managed_dns_done"),
        )
        self.assertEqual(
            json.loads(data), {"id": 1, "host": "public.example.test", "port": 443}
        )
        self.resolve()
        self.assertEqual(len(self.w.urls), 1)
        url, options, timeout, callback, data = self.w.urls[0]
        self.assertEqual(url, URL)
        self.assertEqual(options["resolve"], "public.example.test:443:" + PUBLIC)
        self.assertEqual((callback, data), ("managed_url_done", "1"))
        self.assertTrue(0 < timeout <= 10000)
        for key, value in http._FIXED_OPTIONS.items():
            self.assertEqual(options[key], value)
        self.assertEqual(options["customrequest"], "GET")
        self.assertNotIn("postfields", options)
        self.assertNotIn("file_in", options)
        self.assertNotIn("file_out", options)
        self.assertNotIn("connect_to", options)
        self.assertNotIn("unix_socket_path", options)
        self.assertIn("Accept-Encoding: identity", options["httpheader"])
        self.finish()
        self.assertEqual(
            self.results,
            [
                {
                    "id": 1,
                    "result": {
                        "status": 200,
                        "body": "ok",
                        "headers": {"content-type": "text/plain"},
                    },
                }
            ],
        )
        self.assertFalse(self.client.pending)

    def test_registered_url_callback_accepts_native_four_argument_abi(self):
        # The pinned Python binding invokes hook_url callbacks with "sshh":
        # data, URL, request options, response output. Exercise the registered
        # module-level wrapper, not just NativeHTTP.url_done's internal API.
        api = SimpleNamespace(w=self.w, managed=SimpleNamespace(http=self.client))
        self.client.request(1, "GET", URL)
        self.resolve()
        url, options, _timeout, callback, data = self.w.urls[-1]
        output = {
            "response_code": "200",
            "output": "native-response",
            "headers": "Content-Type: text/plain\r\nContent-Length: 15\r\n",
        }
        with patch.object(weechat_mcp, "api", api):
            result = getattr(weechat_mcp, callback)(data, url, options, output)
        self.assertEqual(result, self.w.WEECHAT_RC_OK)
        self.assertEqual(self.results[0]["result"]["body"], "native-response")
        self.assertFalse(self.client.pending)

        # A late callback after adapter shutdown is harmless but keeps the same
        # native ABI and return code.
        api.managed = None
        with patch.object(weechat_mcp, "api", api):
            result = getattr(weechat_mcp, callback)(data, url, options, output)
        self.assertEqual(result, self.w.WEECHAT_RC_OK)
        self.assertEqual(len(self.results), 1)

    def test_public_http_opt_in_preserves_mandatory_address_and_transport_guards(self):
        grant = json.loads(
            (Path(__file__).parent / "policy/cuse2-public-data.json").read_text()
        )["managed"]
        self.assertIs(grant["public_http"], True)
        self.assertEqual(set(grant["http_methods"]), http.METHODS)
        self.assertEqual(grant["buffers"], ["irc.docker.#cuse2"])
        self.assertFalse(grant["log"])
        for index, method in enumerate(grant["http_methods"], 1):
            body = "" if method == "GET" else "synthetic"
            with self.subTest(method=method):
                for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "[::1]"):
                    with self.assertRaises(http.HttpError):
                        self.client.request(index, method, f"http://{address}/", body)
                self.assertFalse(self.w.processes)
                self.client.request(index, method, URL, body)
                self.resolve([answer(), answer("10.0.0.1")], ident=index)
                self.assertEqual(self.results[-1]["outcome"], "not_sent")
                self.assertFalse(self.w.urls)
                self.w.processes.clear()
                self.client.request(index, method, URL, body)
                self.resolve(ident=index)
                options = self.w.urls[-1][1]
                self.assertEqual(
                    options["resolve"], "public.example.test:443:" + PUBLIC
                )
                self.assertEqual(options["followlocation"], "0")
                self.assertEqual(options["maxredirs"], "0")
                self.assertEqual(options["proxy"], "")
                self.assertEqual(options["netrc"], "ignored")
                self.assertEqual(options["ssl_verifypeer"], "1")
                self.assertEqual(self.w.processes[-1][0], "func:managed_dns_worker")
                self.finish(ident=index)
                self.w.urls.clear()
                self.w.processes.clear()

    def test_every_dns_answer_checked_again_in_parent(self):
        for records in (
            [answer(), answer("127.0.0.1")],
            [answer(), answer("::1")],
            [],
            "malformed",
            None,
        ):
            with self.subTest(records=records):
                self.client.request(1, "POST", URL, "x")
                process = self.w.processes[-1]
                self.client.dns_done(process[3], process[0], 0, json.dumps(records), "")
                self.assertEqual(self.results[-1]["outcome"], "not_sent")
                self.assertFalse(self.w.urls)
                self.assertFalse(self.client.pending)

    def test_ipv6_dns_answer_bracketed_and_literal_avoids_resolve_key(self):
        self.client.request(1, "GET", URL)
        self.resolve([answer("2606:4700:4700::1111")])
        self.assertEqual(
            self.w.urls[-1][1]["resolve"],
            "public.example.test:443:[2606:4700:4700::1111]",
        )
        self.finish()
        self.client.request(2, "GET", "https://[2606:4700:4700::1111]/")
        self.resolve([answer("2606:4700:4700::1111")], ident=2)
        self.assertNotIn("resolve", self.w.urls[-1][1])

    def test_only_four_methods_and_strict_body_headers(self):
        for method, body, headers in (
            ("HEAD", "", None),
            ("get", "", None),
            (None, "", None),
            ("GET", "body", None),
            ("POST", b"bytes", None),
            ("POST", "x" * (http.MAX_BODY_BYTES + 1), None),
            ("POST", "\ud800", None),
            ("POST", "nul\x00byte", None),
            ("GET", "", {"Authorization": "secret"}),
            ("GET", "", {"Cookie": "secret"}),
            ("GET", "", {"Host": "localhost"}),
            ("GET", "", {"Proxy-Authorization": "secret"}),
            ("GET", "", {"Accept": "x\r\nHost: localhost"}),
            ("GET", "", {"accept": "x", "Accept": "y"}),
            ("GET", "", {"Content-Type": "x" * 257}),
        ):
            with (
                self.subTest(method=method, headers=headers),
                self.assertRaises(http.HttpError),
            ):
                self.client.request(1, method, URL, body, headers)
        self.assertFalse(self.w.processes)
        for index, method in enumerate(sorted(http.METHODS)):
            body = "snowman ☃" if method != "GET" else ""
            self.client.request(index, method, URL, body)
            self.resolve(ident=index)
            options = self.w.urls[-1][1]
            self.assertEqual(options["customrequest"], method)
            if body:
                self.assertEqual(options["postfields"], body)
            self.finish(ident=index)

    def test_maximum_two_pending_including_dns(self):
        self.client.request(1, "GET", URL)
        self.client.request(2, "POST", URL, "x")
        with self.assertRaisesRegex(http.HttpError, "too_many_requests"):
            self.client.request(3, "GET", URL)
        with self.assertRaisesRegex(http.HttpError, "invalid_request_id"):
            self.client.request(1, "GET", URL)
        self.resolve(ident=1)
        with self.assertRaisesRegex(http.HttpError, "too_many_requests"):
            self.client.request(3, "GET", URL)
        self.finish(ident=1)
        self.client.request(3, "GET", URL)
        self.assertEqual(len(self.client.pending), 2)

    def test_dns_chunks_bound_failure_and_timeout(self):
        self.client.request(1, "GET", URL)
        command, _, _, data = self.w.processes[-1]
        raw = json.dumps([answer()])
        self.client.dns_done(data, command, -1, raw[:10], "")
        self.assertFalse(self.w.urls)
        self.client.dns_done(data, command, 0, raw[10:], "")
        self.assertEqual(len(self.w.urls), 1)
        self.finish()
        self.client.request(2, "DELETE", URL)
        command, _, _, data = self.w.processes[-1]
        self.client.dns_done(data, command, -2, "", "private DNS diagnostic")
        self.assertEqual(
            self.results[-1], {"id": 2, "error": "dns_failed", "outcome": "not_sent"}
        )
        self.client.request(3, "GET", URL)
        command, _, _, data = self.w.processes[-1]
        self.client.dns_done(data, command, -1, "x" * (http.MAX_DNS_BYTES + 1), "")
        self.assertEqual(self.results[-1]["error"], "dns_response_too_large")
        self.assertIn("dns-3", self.w.unhooked)

    def test_dns_and_transfer_share_one_deadline(self):
        with patch.object(http.time, "monotonic", return_value=10.0):
            self.client.request(1, "GET", URL)
        with patch.object(http.time, "monotonic", return_value=19.0):
            self.resolve()
        self.assertEqual(self.w.urls[-1][2], 1000)
        self.assertEqual(self.w.urls[-1][1]["timeout_ms"], "1000")
        with patch.object(http.time, "monotonic", return_value=20.1):
            self.finish()
        self.assertEqual(self.results[-1]["error"], "deadline_exceeded")
        self.client.request(2, "GET", URL)
        with patch.object(
            http.time, "monotonic", return_value=self.client.pending[2]["deadline"] + 1
        ):
            self.resolve(ident=2)
        self.assertEqual(self.results[-1]["outcome"], "not_sent")
        self.assertEqual(len(self.w.urls), 1)

    def test_no_redirect_cookie_or_credential_forwarding(self):
        self.client.request(1, "GET", URL)
        self.resolve()
        self.finish(
            {
                "response_code": "302",
                "output": "",
                "headers": "Location: http://127.0.0.1/private\r\nSet-Cookie: secret=value\r\n",
            }
        )
        self.assertEqual(
            self.results[-1]["result"], {"status": 302, "body": "", "headers": {}}
        )
        self.assertEqual(len(self.w.urls), 1)
        options = self.w.urls[-1][1]
        self.assertEqual(options["proxy"], "")
        self.assertEqual(options["pre_proxy"], "")
        self.assertEqual(options["noproxy"], "*")
        self.assertEqual(options["netrc"], "ignored")
        self.assertNotIn("cookie", options)
        self.assertNotIn("username", options)

    def test_mutations_uncertain_after_dispatch_never_retried(self):
        for index, method in enumerate(sorted(http.METHODS)):
            self.client.request(index, method, URL)
            self.resolve(ident=index)
            self.finish(
                {"error": "private error containing URL/body", "error_code": "2"},
                ident=index,
            )
            self.assertEqual(
                self.results[-1],
                {
                    "id": index,
                    "error": "transport_failed",
                    "outcome": "failed" if method == "GET" else "uncertain",
                },
            )
        self.assertEqual(len(self.w.urls), 4)
        self.assertEqual(len(self.w.processes), 4)

    def test_response_limits_and_unexpected_compression(self):
        for output in (
            {"response_code": "200", "output": "x" * (http.MAX_RESPONSE_BYTES + 1)},
            {"response_code": "200", "headers": "x" * (http.MAX_HEADER_BYTES + 1)},
            {"response_code": "200", "headers": "Content-Encoding: gzip\r\n"},
            {"response_code": "0"},
            {"response_code": "not a number"},
            {"response_code": "200", "output": "\ud800"},
            {"response_code": "200", "output": b"\xff"},
            {"response_code": "200", "headers": b"Content-Type: \xff"},
            {"response_code": "200", "output": "nul\x00byte"},
            {
                "response_code": "200",
                "output": "short",
                "headers": "Content-Length: 9\r\n",
            },
        ):
            self.client.request(1, "POST", URL)
            self.resolve()
            self.finish(output)
            self.assertIn("error", self.results[-1])
            self.assertEqual(self.results[-1]["outcome"], "uncertain")

    def test_close_unhooks_all_and_ignores_late_callbacks(self):
        self.client.request(1, "POST", URL)
        self.resolve()
        self.client.request(2, "GET", URL)
        self.client.close()
        self.assertEqual(set(self.w.unhooked), {"url-1", "dns-2"})
        self.finish()
        self.resolve(ident=2)
        self.assertFalse(self.results)
        with self.assertRaisesRegex(http.HttpError, "http_closed"):
            self.client.request(3, "GET", URL)


if __name__ == "__main__":
    unittest.main()


class CanonicalRequestTests(unittest.TestCase):
    def test_policy_receives_canonical_url_and_dot_paths_rejected(self):
        from managed_http import HttpError, validate_request

        self.assertEqual(
            validate_request("GET", "https://example.com", "")["url"],
            "https://example.com/",
        )
        for path in (
            "/public/../admin",
            "/./admin",
            "/public/%2e%2e/admin",
            "/public/.%2E/admin",
            "/public%2f..%2fadmin",
            "/public%5cadmin",
        ):
            with self.subTest(path=path), self.assertRaises(HttpError):
                validate_request("GET", "https://example.com" + path, "")
        self.assertEqual(
            validate_request("GET", "https://example.com/public?q=../admin", "")["url"],
            "https://example.com/public?q=../admin",
        )
