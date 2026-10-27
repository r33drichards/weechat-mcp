# SPDX-License-Identifier: GPL-3.0-or-later
"""Public HTTP through WeeChat's native hook_url, never a separate HTTP client.

A fixed, trusted hook_process function resolves DNS in WeeChat's supported forked
helper (no exec). The main loop checks every answer, then pins one public address
in hook_url. Managed JavaScript never sees raw hooks, pointers or libcurl options.
"""

import ctypes
import ipaddress
import json
import re
import socket
import time
from urllib.parse import urlsplit

MAX_URL_BYTES = 2048
MAX_BODY_BYTES = 16384
MAX_RESPONSE_BYTES = 65536
MAX_DNS_BYTES = 8192
MAX_HEADER_BYTES = 32768
DEADLINE_SECONDS = 10.0
METHODS = frozenset({"GET", "POST", "PUT", "DELETE"})
MUTATING = frozenset({"POST", "PUT", "DELETE"})
_ALLOWED_HEADERS = {"accept": "Accept", "content-type": "Content-Type"}
_RESPONSE_HEADERS = frozenset({"content-type", "content-language", "retry-after"})
# Explicit conservative tables make policy independent of Python's changing
# is_global classifications. IPv6 is additionally limited to 2000::/3.
_DENY_V4 = tuple(
    ipaddress.ip_network(value)
    for value in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
_DENY_V6 = tuple(
    ipaddress.ip_network(value)
    for value in ("2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20")
)
_GLOBAL_V6 = ipaddress.ip_network("2000::/3")


class HttpError(ValueError):
    """Stable, URL/body-free errors; mutation uncertainty is never retried."""

    def __init__(self, code, outcome="not_sent"):
        super().__init__(code)
        self.outcome = outcome


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HttpError("deadline_exceeded")
    return remaining


def _public_address(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise HttpError("invalid_address") from exc
    if str(address) != value:
        raise HttpError("noncanonical_address")
    if isinstance(address, ipaddress.IPv4Address):
        denied = any(address in network for network in _DENY_V4)
    else:
        denied = address not in _GLOBAL_V6 or any(
            address in network for network in _DENY_V6
        )
    if denied or not address.is_global or address.is_multicast:
        raise HttpError("nonpublic_address")
    return address


def _url(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_URL_BYTES
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
        or "\\" in value
        or "#" in value
        or re.search(r"%(?![0-9a-fA-F]{2})", value)
    ):
        raise HttpError("invalid_url")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise HttpError("invalid_url") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not value.startswith(parsed.scheme + "://")
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or "%" in host
        or host != host.lower()
    ):
        raise HttpError("invalid_url")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        _public_address(host)
        authority = "[" + host + "]" if literal.version == 6 else host
    else:
        labels = host.split(".")
        if (
            len(host) > 253
            or len(labels) < 2
            or labels[-1].isdigit()
            or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in labels
            )
        ):
            raise HttpError("invalid_hostname")
        authority = host
    if port is not None:
        if not 1 <= port <= 65535:
            raise HttpError("invalid_port")
        authority += ":" + str(port)
    if parsed.netloc != authority:
        raise HttpError("noncanonical_authority")
    path = parsed.path or "/"
    # Do not authorize one path then let libcurl normalize it into another.
    # Also reject encoded separators/dot segments rather than relying on a
    # remote application's percent-decoding or route-normalization behavior.
    if re.search(r"%(?:2f|5c)", path, re.IGNORECASE):
        raise HttpError("ambiguous_path")
    decoded_path = re.sub(
        r"%([0-9a-fA-F]{2})", lambda match: chr(int(match[1], 16)), path
    )
    if any(segment in {".", ".."} for segment in decoded_path.split("/")):
        raise HttpError("ambiguous_path")
    target = path
    if parsed.query:
        target += "?" + parsed.query
    return (
        parsed.scheme,
        host,
        port or (443 if parsed.scheme == "https" else 80),
        authority,
        target,
    )


def _headers(values):
    if values is None:
        return {}
    if not isinstance(values, dict) or len(values) > len(_ALLOWED_HEADERS):
        raise HttpError("invalid_headers")
    result = {}
    for key, value in values.items():
        if not isinstance(key, str) or key.lower() not in _ALLOWED_HEADERS:
            raise HttpError("invalid_headers")
        name = _ALLOWED_HEADERS[key.lower()]
        if (
            name in result
            or not isinstance(value, str)
            or not 1 <= len(value) <= 256
            or any(ord(char) < 32 or ord(char) > 126 for char in value)
        ):
            raise HttpError("invalid_headers")
        result[name] = value
    return result


def _checked_addresses(records, port):
    if not records or len(records) > 32:
        raise HttpError("invalid_dns_answer")
    checked = []
    for family, kind, protocol, _canonical, address in records:
        if (
            family not in {socket.AF_INET, socket.AF_INET6}
            or kind != socket.SOCK_STREAM
            or protocol not in {0, socket.IPPROTO_TCP}
        ):
            raise HttpError("invalid_dns_answer")
        if len(address) != (2 if family == socket.AF_INET else 4) or address[1] != port:
            raise HttpError("invalid_dns_answer")
        ip = _public_address(address[0])
        if (ip.version == 4) != (family == socket.AF_INET):
            raise HttpError("invalid_dns_answer")
        if family == socket.AF_INET6 and tuple(address[2:]) != (0, 0):
            raise HttpError("invalid_dns_answer")
        checked.append((family, address))
    return checked


class _CurlVersion(ctypes.Structure):
    # Stable prefix of curl_version_info_data, requested at CURLVERSION_FIRST.
    _fields_ = [
        ("age", ctypes.c_int),
        ("version", ctypes.c_char_p),
        ("version_num", ctypes.c_uint),
    ]


def _loaded_curl_version():
    """Inspect WeeChat's loaded libcurl; never run ldconfig/find_library/exec."""
    try:
        function = ctypes.CDLL(None).curl_version_info
        function.argtypes = [ctypes.c_int]
        function.restype = ctypes.POINTER(_CurlVersion)
        info = function(0)
        if not info:
            raise HttpError("libcurl_unavailable")
        return info.contents.version_num
    except (AttributeError, OSError) as exc:
        raise HttpError("libcurl_unavailable") from exc


_FIXED_OPTIONS = {
    "followlocation": "0",
    "path_as_is": "1",
    "nosignal": "1",
    "maxredirs": "0",
    "proxy": "",
    "pre_proxy": "",
    "noproxy": "*",
    "netrc": "ignored",
    "httpauth": "none",
    "proxyauth": "none",
    "unrestricted_auth": "0",
    "disallow_username_in_url": "1",
    "protocols_str": "http,https",
    "redir_protocols_str": "http,https",
    "http_version": "1_1",
    "http09_allowed": "0",
    "ssl_verifypeer": "1",
    "ssl_verifyhost": "2",
    "fresh_connect": "1",
    "forbid_reuse": "1",
    "maxfilesize_large": str(MAX_RESPONSE_BYTES),
    "http_content_decoding": "0",
    "http_transfer_decoding": "1",
}
_REQUIRED_OPTIONS = set(_FIXED_OPTIONS) | {
    "resolve",
    "customrequest",
    "postfields",
    "httpheader",
    "timeout_ms",
    "connecttimeout_ms",
}
_REQUIRED_CONSTANTS = {
    "netrc": "ignored",
    "httpauth": "none",
    "proxyauth": "none",
    "http_version": "1_1",
}


def _check_native(w):
    if not callable(getattr(w, "hook_url", None)) or not callable(
        getattr(w, "hook_process", None)
    ):
        raise HttpError("native_http_unavailable")
    # Earlier versions only bound downloads with a known Content-Length.
    if _loaded_curl_version() < 0x080400:
        raise HttpError("libcurl_8_4_required")
    info = w.infolist_get("url_options", "", "")
    if not info:
        raise HttpError("native_url_options_unavailable")
    options = {}
    try:
        while w.infolist_next(info):
            options[w.infolist_string(info, "name").lower()] = (
                w.infolist_string(info, "constants").lower().split(",")
            )
    finally:
        w.infolist_free(info)
    if not _REQUIRED_OPTIONS <= options.keys() or any(
        value not in options.get(key, []) for key, value in _REQUIRED_CONSTANTS.items()
    ):
        raise HttpError("native_url_options_unavailable")


def dns_worker(data):
    """Called only by the fixed native fork callback; no WeeChat API calls here."""
    try:
        value = json.loads(data)
        host, port = value["host"], value["port"]
        # Repeat strict destination validation in the helper; parent still checks
        # every answer before any HTTP transfer is dispatched.
        authority = "[" + host + "]" if ":" in host else host
        _url("https://" + authority + ":" + str(port) + "/")
        records = socket.getaddrinfo(
            host, port, socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP
        )
        _checked_addresses(records, port)
        encoded = json.dumps(records, separators=(",", ":"))
        if len(encoded.encode()) > MAX_DNS_BYTES:
            raise HttpError("dns_response_too_large")
        return encoded
    except (ValueError, KeyError, TypeError, OSError):
        return "null"


class NativeHTTP:
    """Event-loop-only controller; two calls total, including DNS resolution."""

    def __init__(self, w, complete):
        _check_native(w)
        self.w = w
        self.complete = complete
        self.pending = {}
        self.closed = False

    def request(self, ident, method, url, body="", headers=None):
        if self.closed:
            raise HttpError("http_closed")
        if (
            isinstance(ident, bool)
            or not isinstance(ident, int)
            or ident < 0
            or ident in self.pending
        ):
            raise HttpError("invalid_request_id")
        if len(self.pending) >= 2:
            raise HttpError("too_many_requests")
        if not isinstance(method, str) or method not in METHODS:
            raise HttpError("invalid_method")
        if not isinstance(body, str) or len(body) > MAX_BODY_BYTES or "\x00" in body:
            raise HttpError("invalid_body")
        try:
            encoded = body.encode("utf-8")
        except UnicodeError as exc:
            raise HttpError("invalid_body") from exc
        if len(encoded) > MAX_BODY_BYTES or (method == "GET" and body):
            raise HttpError("invalid_body")
        scheme, host, port, _authority, _target = _url(url)
        del scheme
        outbound_headers = _headers(headers)
        outbound_headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        outbound_headers.setdefault("Accept", "*/*")
        outbound_headers.update({"Accept-Encoding": "identity", "Expect": ""})
        data = json.dumps(
            {"id": ident, "host": host, "port": port}, separators=(",", ":")
        )
        item = {
            "method": method,
            "url": url,
            "body": body,
            "host": host,
            "port": port,
            "headers": outbound_headers,
            "deadline": time.monotonic() + DEADLINE_SECONDS,
            "hook": "",
            "stage": "dns",
            "dns": "",
            "dns_error": False,
        }
        self.pending[ident] = item
        try:
            hook = self.w.hook_process(
                "func:managed_dns_worker",
                int(DEADLINE_SECONDS * 1000),
                "managed_dns_done",
                data,
            )
            if not hook:
                raise HttpError("dns_hook_failed")
            item["hook"] = hook
        except Exception:
            self.pending.pop(ident, None)
            raise

    def _finish(self, ident, *, result=None, error=None):
        item = self.pending.pop(ident, None)
        if item is None:
            return
        if error is not None:
            outcome = (
                "uncertain"
                if item["stage"] == "url" and item["method"] in MUTATING
                else "failed"
                if item["stage"] == "url"
                else "not_sent"
            )
            self.complete({"id": ident, "error": error, "outcome": outcome})
        else:
            self.complete({"id": ident, "result": result})

    def dns_done(self, data, command, returncode, out, err):
        try:
            ident = json.loads(data)["id"]
        except (ValueError, KeyError, TypeError):
            return self.w.WEECHAT_RC_OK
        item = self.pending.get(ident)
        if item is None or item["stage"] != "dns":
            return self.w.WEECHAT_RC_OK
        item["dns"] += out
        item["dns_error"] = item["dns_error"] or bool(err)
        if len(item["dns"].encode("utf-8")) > MAX_DNS_BYTES:
            if item["hook"]:
                self.w.unhook(item["hook"])
            self._finish(ident, error="dns_response_too_large")
            return self.w.WEECHAT_RC_OK
        if returncode == self.w.WEECHAT_HOOK_PROCESS_RUNNING:
            return self.w.WEECHAT_RC_OK
        item["hook"] = ""
        try:
            if (
                command != "func:managed_dns_worker"
                or returncode != 0
                or item["dns_error"]
            ):
                raise HttpError("dns_failed")
            records = json.loads(item["dns"])
            addresses = _checked_addresses(records, item["port"])
            remaining = max(1, int(_remaining(item["deadline"]) * 1000))
            options = dict(_FIXED_OPTIONS)
            options.update(
                {
                    "customrequest": item["method"],
                    "timeout_ms": str(remaining),
                    "connecttimeout_ms": str(min(3000, remaining)),
                    "httpheader": "\n".join(
                        key + ": " + value for key, value in item["headers"].items()
                    ),
                }
            )
            if item["method"] != "GET":
                options["postfields"] = item["body"]
            # IP-literal URLs need no resolver entry. DNS names retain their URL
            # hostname (Host + TLS SNI/certificate name); only the socket IP is pinned.
            try:
                ipaddress.ip_address(item["host"])
            except ValueError:
                address = addresses[0][1][0]
                pinned = "[" + address + "]" if ":" in address else address
                options["resolve"] = (
                    item["host"] + ":" + str(item["port"]) + ":" + pinned
                )
            # Thread-creation failures can invoke the native callback before
            # hook_url returns. Publish the stage first and never retain the
            # already-freed handle returned after such synchronous completion.
            item["stage"] = "url"
            hook = self.w.hook_url(
                item["url"], options, remaining, "managed_url_done", str(ident)
            )
            if self.pending.get(ident) is not item:
                return self.w.WEECHAT_RC_OK
            if not hook:
                item["stage"] = "dns"  # no transfer was dispatched
                raise HttpError("url_hook_failed")
            item["hook"] = hook
        except (HttpError, ValueError, TypeError, KeyError) as exc:
            self._finish(
                ident,
                error=str(exc) if isinstance(exc, HttpError) else "invalid_dns_answer",
            )
        return self.w.WEECHAT_RC_OK

    def url_done(self, data, url, output):
        try:
            ident = int(data)
        except ValueError:
            return self.w.WEECHAT_RC_OK
        item = self.pending.get(ident)
        if item is None or item["stage"] != "url":
            return self.w.WEECHAT_RC_OK
        item["hook"] = ""
        try:
            _remaining(item["deadline"])
            if (
                url != item["url"]
                or output.get("error")
                or output.get("error_code")
                or output.get("error_code_curl")
            ):
                raise HttpError("transport_failed")
            body = output.get("output", "")
            headers = output.get("headers", "")
            if (
                not isinstance(body, str)
                or not isinstance(headers, str)
                or "\x00" in body
            ):
                raise HttpError("response_not_text")
            if (
                len(body.encode("utf-8")) > MAX_RESPONSE_BYTES
                or len(headers.encode("utf-8")) > MAX_HEADER_BYTES
            ):
                raise HttpError("response_too_large")
            status = int(output.get("response_code", "0"))
            if not 200 <= status <= 599:
                raise HttpError("invalid_http_response")
            result_headers = {}
            content_length = None
            transfer_encoding = False
            for line in headers.splitlines():
                if line.startswith("HTTP/"):
                    result_headers = {}
                    content_length = None
                    transfer_encoding = False
                    continue
                key, separator, value = line.partition(":")
                if not separator:
                    continue
                key, value = key.lower(), value.strip()
                if key == "content-encoding" and value.lower() != "identity":
                    raise HttpError("unsupported_content_encoding")
                if key == "content-length":
                    length = int(value)
                    if length < 0 or content_length not in (None, length):
                        raise HttpError("invalid_http_response")
                    content_length = length
                if key == "transfer-encoding":
                    transfer_encoding = True
                if key in _RESPONSE_HEADERS:
                    result_headers[key] = value
            if (
                content_length is not None
                and not transfer_encoding
                and status not in {204, 304}
                and content_length != len(body.encode("utf-8"))
            ):
                # Native hook_url is C-string text, not binary-safe. A known
                # length mismatch detects incomplete or NUL-truncated bodies.
                raise HttpError("nontext_or_incomplete_response")
            self._finish(
                ident,
                result={"status": status, "body": body, "headers": result_headers},
            )
        except (HttpError, ValueError, UnicodeError, AttributeError, TypeError) as exc:
            self._finish(
                ident,
                error=str(exc)
                if isinstance(exc, HttpError)
                else "invalid_http_response",
            )
        return self.w.WEECHAT_RC_OK

    def cancel(self, ident):
        """Stop local work only; a dispatched mutation may already have executed."""
        item = self.pending.pop(ident, None)
        if item and item["hook"]:
            self.w.unhook(item["hook"])

    def close(self):
        """Cancel local work, never resend; an already dispatched mutation may run."""
        self.closed = True
        pending, self.pending = self.pending, {}
        for item in pending.values():
            if item["hook"]:
                self.w.unhook(item["hook"])


def validate_request(method, url, body):
    """No network: return canonical strings for the native policy decision."""
    if method not in METHODS:
        raise HttpError("invalid_method")
    if not isinstance(body, str) or "\x00" in body:
        raise HttpError("invalid_body")
    try:
        if len(body.encode("utf-8")) > MAX_BODY_BYTES or (method == "GET" and body):
            raise HttpError("invalid_body")
    except UnicodeError as exc:
        raise HttpError("invalid_body") from exc
    scheme, host, port, authority, target = _url(url)
    canonical_url = scheme + "://" + authority + target
    return {
        "method": method,
        "url": canonical_url,
        "body": body,
        "scheme": scheme,
        "host": host,
        "port": str(port),
    }
