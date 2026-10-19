#!/usr/bin/env python3
"""Opt-in synthetic full-image OAuth flow for container_auth_test.py.

This module never edits a production template. prepare_fixture() writes a
throwaway Dex template mounted read-only over /app/dex.yaml.template in the
*test auth container only*. No external provider, password, or real account is
used. Run through: python deploy/container_auth_test.py --refresh-flow

The transport must retain the topology harness's HTTPS CA verification and
fixture-host allowlist. Tokens/cookies stay in memory and are never printed.
"""

import base64
import hashlib
import http.cookiejar
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import Request

OWNER_EMAIL = "kilgore@kilgore.trout"  # Pinned Dex mockCallback's fixed identity.
CALLBACK = "http://localhost:9999/callback"


def prepare_fixture(root, fixtures, environment):
    """Return auth-container-only Docker mount arguments; mutate test env only."""
    production = (root / "deploy/dex.yaml.template").read_text()
    assert production.count("\nconnectors:\n") == 1
    template = production.split("\nconnectors:\n", 1)[0]
    assert "enforce: true" in template and "[S256]" in template
    assert "enablePasswordDB: false" in template
    assert "\nexpiry:" not in template
    template += """
# Ephemeral integration-test override. Never included in a production image.
expiry:
  refreshTokens:
    disableRotation: false
    reuseInterval: "0s"
connectors:
  - type: mockCallback
    id: synthetic
    name: Synthetic fixture only
    config: {}
"""
    path = fixtures / "synthetic-dex.yaml.template"
    path.write_text(template)
    path.chmod(0o644)
    environment["OWNER_EMAIL"] = OWNER_EMAIL
    return ("--mount", f"type=bind,src={path},dst=/app/dex.yaml.template,readonly")


class CookieResponse:
    def __init__(self, headers):
        self.headers = headers

    def info(self):
        return self.headers


class Browser:
    """HTTP redirect driver with standard domain/path/secure cookie handling."""

    def __init__(self, request, auth_host, mcp_host):
        self.request = request
        self.auth_host = auth_host
        self.allowed = {auth_host, mcp_host}
        self.cookies = http.cookiejar.CookieJar()

    def authorize(self, url, state, expect_login):
        saw_pkce = False
        previous = None
        dispatcher_request = None
        for hop in range(30):
            parsed = urlsplit(url)
            if (
                parsed.scheme == "http"
                and parsed.netloc == "localhost:9999"
                and parsed.path == "/callback"
            ):
                query = parse_qs(parsed.query)
                assert query.get("state") == [state], "OAuth state mismatch"
                assert "error" not in query and len(query.get("code", [])) == 1, (
                    "OAuth callback did not contain a code"
                )
                assert saw_pkce == expect_login, (
                    "Unexpected fresh upstream login; clients must share one browser session"
                )
                return query["code"][0]
            assert parsed.scheme == "https" and parsed.hostname in self.allowed, (
                "Refused redirect outside synthetic fixture"
            )
            if parsed.hostname == self.auth_host and parsed.path == "/dex/auth":
                query = parse_qs(parsed.query, keep_blank_values=True)
                if "req" in query:
                    # Pinned Dex uses /auth for both OAuth entry and its signed
                    # post-connector dispatcher. The latter carries req+hmac,
                    # not a second copy of the original PKCE parameters. Dex
                    # validates the HMAC; here validate its fixture-only shape
                    # and ordering without claiming to verify its signature.
                    assert saw_pkce, "Dex dispatcher preceded S256 authorization"
                    assert previous is not None and (
                        previous.hostname == self.auth_host
                        and previous.path.startswith("/dex/")
                    ), "Dex dispatcher did not follow a fixture Dex redirect"
                    assert set(query) == {"req", "hmac"}, (
                        "Unexpected Dex dispatcher parameters"
                    )
                    assert all(
                        len(query[name]) == 1 and query[name][0]
                        for name in ("req", "hmac")
                    ), "Missing or duplicated Dex dispatcher proof"
                    if dispatcher_request is not None:
                        assert dispatcher_request == query["req"][0], (
                            "Dex dispatcher changed authorization request"
                        )
                    dispatcher_request = query["req"][0]
                else:
                    assert not saw_pkce, (
                        "Unexpected repeated upstream OAuth authorization"
                    )
                    assert query.get("code_challenge_method") == ["S256"]
                    assert len(query.get("code_challenge", [])) == 1
                    assert len(query["code_challenge"][0]) >= 43
                    assert query.get("client_id") == ["pomerium"]
                    assert query.get("response_type") == ["code"]
                    assert query.get("redirect_uri") == [
                        f"https://{self.auth_host}/oauth2/callback"
                    ]
                    saw_pkce = True
            req = Request(url)
            self.cookies.add_cookie_header(req)
            status, headers, _ = self.request(url, headers=dict(req.header_items()))
            # Log only enumerated paths: an unexpected path might itself carry
            # a credential. Never print query strings, Location, or cookies.
            safe_paths = {
                "/.pomerium/mcp/authorize",
                "/.pomerium/callback/",
                "/.pomerium/sign_in",
                "/oauth2/callback",
                "/dex/auth",
                "/dex/auth/synthetic",
                "/dex/callback",
                "/dex/approval",
            }
            path_label = parsed.path if parsed.path in safe_paths else "<other>"
            role = "auth" if parsed.hostname == self.auth_host else "mcp"
            print(
                f"OAuth hop={hop} host={role} path={path_label} status={status}",
                flush=True,
            )
            # CookieJar only calls info(); the transport has already read the body.
            self.cookies.extract_cookies(CookieResponse(headers), req)  # ty: ignore[invalid-argument-type]
            assert status in (302, 303, 307, 308), (
                f"OAuth redirect hop {hop} returned HTTP {status}"
            )
            assert headers.get("Location"), "OAuth redirect has no Location"
            previous = parsed
            url = urljoin(url, headers["Location"])
        raise AssertionError("OAuth flow exceeded redirect limit")


def token_request(request, endpoint, fields, expected=200):
    status, _, body = request(
        endpoint,
        "POST",
        urlencode(fields),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    # Do not include payload or URLs in assertion messages: they contain tokens.
    assert status == expected, (
        f"Token endpoint returned HTTP {status}; expected {expected}"
    )
    result = json.loads(body)
    if expected == 200:
        assert isinstance(result.get("access_token"), str) and result["access_token"]
        assert isinstance(result.get("refresh_token"), str) and result["refresh_token"]
        assert result.get("token_type", "").lower() == "bearer"
        assert result.get("expires_in", 0) > 0
    else:
        assert result.get("error") == "invalid_grant", "Expected PKCE invalid_grant"
    return result


class RefreshFlow:
    def __init__(self, request, metadata, resource, auth_host, mcp_host):
        self.request = request
        self.metadata = metadata
        self.resource = resource
        self.browser = Browser(request, auth_host, mcp_host)
        self.clients = []

    def establish(self):
        """Two distinct DCR clients, one authenticated browser/upstream lineage."""
        for index in range(2):
            status, _, body = self.request(
                self.metadata["registration_endpoint"],
                "POST",
                {
                    "client_name": f"Synthetic refresh client {index + 1}",
                    "redirect_uris": [CALLBACK],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
                {"Content-Type": "application/json"},
            )
            assert status in (200, 201), f"DCR returned HTTP {status}"
            client_id = json.loads(body)["client_id"]
            assert all(client["client_id"] != client_id for client in self.clients)
            verifier = secrets.token_urlsafe(48)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            state = secrets.token_urlsafe(24)
            url = (
                self.metadata["authorization_endpoint"]
                + "?"
                + urlencode(
                    {
                        "response_type": "code",
                        "client_id": client_id,
                        "redirect_uri": CALLBACK,
                        "state": state,
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "resource": self.resource,
                    }
                )
            )
            code = self.browser.authorize(url, state, expect_login=index == 0)
            fields = {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "redirect_uri": CALLBACK,
                "code": code,
                "code_verifier": verifier,
            }
            # Wrong proof must not consume a valid code or grant tokens.
            token_request(
                self.request,
                self.metadata["token_endpoint"],
                {**fields, "code_verifier": secrets.token_urlsafe(48)},
                expected=400,
            )
            tokens = token_request(
                self.request, self.metadata["token_endpoint"], fields
            )
            # Authorization codes remain single-use.
            token_request(
                self.request, self.metadata["token_endpoint"], fields, expected=400
            )
            self.clients.append({"client_id": client_id, "tokens": tokens})
        print(
            "PASS two synthetic MCP clients: shared browser session, complete S256/code exchange, bad-proof rejection and code single use",
            flush=True,
        )
        self.exercise("before restart")
        return self

    def refresh(self, client):
        previous = client["tokens"]["refresh_token"]
        tokens = token_request(
            self.request,
            self.metadata["token_endpoint"],
            {
                "grant_type": "refresh_token",
                "client_id": client["client_id"],
                "refresh_token": previous,
            },
        )
        assert tokens["refresh_token"] != previous, (
            "Downstream refresh token failed to rotate"
        )
        client["tokens"] = tokens

    def exercise(self, phase):
        # Every grant invokes the real Pomerium provider with strict Dex rotation.
        # Alternate both clients repeatedly so stale shared-upstream copies cannot
        # pass merely because only one client was ever exercised.
        for _ in range(3):
            for client in self.clients:
                self.refresh(client)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(self.refresh, self.clients))
        # Check both successors again after the overlapping refresh calls.
        for client in self.clients:
            self.refresh(client)
        print(
            f"PASS strict Dex rotation: both MCP clients repeatedly refresh, including overlap ({phase})",
            flush=True,
        )

    def after_restart(self):
        # No new authorization or browser activity: retained downstream tokens
        # must resolve through the on-volume authority after process recreation.
        self.exercise("after container recreation")
        print(
            "PASS persisted refresh authority: original clients refresh without reauthentication after recreation",
            flush=True,
        )
