#!/usr/bin/env python3
"""Test real nginx + Dex + supervisor with a mock private Pomerium upstream.

No external requests, credentials, or IRC data. This does NOT prove Pomerium OAuth,
container builds, Railway ingress, or real GitHub login. Run separately from units.
"""

import argparse
import base64
import hashlib
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit


class IPv6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


class Probe(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def do_DELETE(self):
        self.respond()

    def do_OPTIONS(self):
        self.respond()

    def respond(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.requests.append((self.command, self.path, dict(self.headers), data))
        if self.path == "/.pomerium/session_binding_info":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Mcp-Session-Id", "synthetic-session")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            time.sleep(0.6)
            self.wfile.write(b"data: second\n\n")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header(
            "Set-Cookie", "synthetic=one; Path=/; Secure; HttpOnly; SameSite=Lax"
        )
        self.send_header(
            "Set-Cookie", "pkce=two; Path=/oauth2/callback; Secure; HttpOnly"
        )
        self.send_header(
            "Location", "https://auth-test.up.railway.app/oauth2/callback?q=a%2Fb"
        )
        self.end_headers()
        self.wfile.write(json.dumps({"mock": True}).encode())


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port, path, method="GET", headers=None, body=None, host="127.0.0.1"):
    connection = http.client.HTTPConnection(host, port, timeout=5)
    headers = {"Host": "auth-test.up.railway.app", **(headers or {})}
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    return connection, response


def wait_ready(process, port):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError("Real auth service exited before readiness")
        try:
            connection, response = request(
                port, "/dex/.well-known/openid-configuration"
            )
            data = response.read()
            connection.close()
            if response.status == 200:
                return json.loads(data)
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError("Real auth service failed readiness")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nginx", type=Path, required=True)
    parser.add_argument("--dex", type=Path, required=True)
    parser.add_argument("--dex-web", type=Path, required=True)
    args = parser.parse_args()
    auth_origin = "https://auth-test.up.railway.app"
    source = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="auth-router-test-") as directory:
        root = Path(directory)
        port, dex_port, telemetry_port = [free_port() for _ in range(3)]
        upstream = IPv6Server(("::1", 0), Probe)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        for name in (
            "pomerium_keys.py",
            "render_config.py",
            "service_entrypoint.py",
            "dex.yaml.template",
            "router.conf.template",
        ):
            shutil.copy2(source / name, root / name)
        dex_config = (root / "dex.yaml.template").read_text()
        dex_config = dex_config.replace("/data/dex.db", str(root / "dex.db"))
        dex_config = dex_config.replace("/srv/dex/web", str(args.dex_web.resolve()))
        dex_config = dex_config.replace(":5556", f":{dex_port}").replace(
            ":5558", f":{telemetry_port}"
        )
        (root / "dex.yaml.template").write_text(dex_config)
        router_config = (root / "router.conf.template").read_text()
        router_config = router_config.replace(":5556", f":{dex_port}")
        router_config = router_config.replace(
            "/tmp/weechat-auth-", str(root / "nginx-")
        )
        (root / "router.conf.template").write_text(router_config)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "dex").symlink_to(args.dex.resolve())
        (bin_dir / "nginx").symlink_to(args.nginx.resolve())
        env = dict(
            os.environ,
            PATH=str(bin_dir) + ":" + os.environ["PATH"],
            AUTH_ORIGIN=auth_origin,
            PORT=str(port),
            POMERIUM_INTERNAL_ORIGIN=f"http://[::1]:{upstream.server_port}",
            GITHUB_CLIENT_ID="synthetic-github-client",
            GITHUB_CLIENT_SECRET="synthetic-github-secret",
        )
        log = root / "service.log"
        process = None
        try:
            with log.open("w") as output:
                process = subprocess.Popen(
                    [sys.executable, str(root / "service_entrypoint.py"), "auth"],
                    env=env,
                    stdout=output,
                    stderr=output,
                )
            discovery = wait_ready(process, port)
            assert discovery["issuer"] == auth_origin + "/dex"
            assert discovery["authorization_endpoint"] == auth_origin + "/dex/auth"
            print(
                "PASS real Dex discovery through real nginx; external /dex issuer preserved"
            )
            query = urlencode(
                {
                    "client_id": "pomerium",
                    "redirect_uri": auth_origin + "/oauth2/callback",
                    "response_type": "code",
                    "scope": "openid email profile",
                    "state": "synthetic-state",
                    "code_challenge_method": "S256",
                    "code_challenge": base64.urlsafe_b64encode(
                        hashlib.sha256(b"synthetic-verifier-" * 4).digest()
                    )
                    .decode()
                    .rstrip("="),
                }
            )
            path = "/dex/auth?" + query
            github = None
            for _ in range(5):
                connection, response = request(port, path)
                location = response.getheader("Location")
                response.read()
                connection.close()
                assert response.status in (302, 303), response.status
                parsed = urlsplit(location)
                if parsed.hostname == "github.com":
                    github = parse_qs(parsed.query)
                    break
                assert not parsed.netloc or parsed.netloc == "auth-test.up.railway.app"
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
            assert github is not None, "Dex did not produce GitHub authorization URL"
            assert github["redirect_uri"] == [auth_origin + "/dex/callback"]
            print(
                "PASS real Dex constructs dedicated GitHub callback (no GitHub network request)"
            )
            denied = [
                "/mcp",
                "/mcp/",
                "/dexter",
                "/metrics",
                "/healthz",
                "/readyz",
                "/debug/pprof",
                "/server_info",
                "/config_dump",
                "/stats",
                "/quitquitquit",
                "/.pomerium/api/v1/routes",
                "/.pomerium/mcp/authorize",
                "/.pomerium/callback/",
                "/.pomerium/jwt",
                "/.well-known/oauth-authorization-server",
                "/.well-known/oauth-protected-resource",
                "/.pomerium/sign_in/anything",
                "/oauth2/callback/anything",
            ]
            before = len(Probe.requests)
            for path in denied:
                connection, response = request(port, path)
                response.read()
                connection.close()
                assert response.status == 404, (path, response.status)
            assert len(Probe.requests) == before
            for path in (
                "/dex/../mcp",
                "/dex/%2e%2e/oauth2/callback",
                "/%2epomerium/sign_in",
                "//oauth2/callback",
                "/.pomerium//sign_in",
                "/oauth2/callback%2f..%2fmcp",
            ):
                connection, response = request(port, path)
                response.read()
                connection.close()
                assert response.status == 400, (path, response.status)
            for host in (
                "mcp-test.up.railway.app",
                "evil.test",
                "auth-test.up.railway.app:80",
            ):
                connection, response = request(port, "/", headers={"Host": host})
                response.read()
                connection.close()
                assert response.status == 421
            assert len(Probe.requests) == before
            print(
                "PASS router blocks MCP/admin/unknown hosts and encoded/dot/duplicate-slash path bypasses"
            )
            for method in ("GET", "POST", "OPTIONS"):
                headers = {
                    "X-Forwarded-Proto": "http",
                    "X-Forwarded-Host": "evil.test",
                    "X-Forwarded-For": "1.2.3.4",
                    "Forwarded": "host=evil.test;proto=http",
                    "X-Pomerium-Reproxy-Policy": "forged",
                    "X-Pomerium-Jwt-Assertion": "forged",
                    "Cookie": "synthetic=fixture",
                    "MCP-Protocol-Version": "2026-07-28",
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                }
                path = "/oauth2/callback?code=synthetic&state=a%2Fb"
                connection, response = request(
                    port, path, method, headers, "code=synthetic"
                )
                response.read()
                connection.close()
                assert response.status == 200
                assert len(response.headers.get_all("Set-Cookie")) == 2
                assert response.getheader("Location").startswith(auth_origin)
                seen_method, seen_path, seen_headers, seen_body = Probe.requests[-1]
                assert (seen_method, seen_path, seen_body) == (
                    method,
                    path,
                    b"code=synthetic",
                )
                assert seen_headers["Host"] == "auth-test.up.railway.app"
                assert seen_headers["X-Forwarded-Proto"] == "https"
                assert seen_headers["X-Forwarded-Host"] == "auth-test.up.railway.app"
                assert seen_headers["X-Forwarded-For"] == "127.0.0.1"
                assert "Forwarded" not in seen_headers
                assert "X-Pomerium-Reproxy-Policy" not in seen_headers
                assert "X-Pomerium-Jwt-Assertion" not in seen_headers
                assert seen_headers["Cookie"] == "synthetic=fixture"
            print(
                "PASS mock-upstream callback methods/query/body, fixed Host/HTTPS and cookie/header preservation"
            )
            start = time.monotonic()
            connection, response = request(port, "/.pomerium/session_binding_info")
            assert response.getheader("Mcp-Session-Id") == "synthetic-session"
            assert response.read(13) == b"data: first\n\n"
            assert time.monotonic() - start < 0.5, "nginx buffered streaming response"
            assert response.read() == b"data: second\n\n"
            connection.close()
            connection, response = request(
                port, "/.well-known/pomerium/jwks.json", host="::1"
            )
            response.read()
            connection.close()
            assert response.status == 200
            print(
                "PASS immediate unbuffered response and IPv6 listener/upstream (mock Pomerium)"
            )
            upstream.shutdown()
            upstream.server_close()
            connection, response = request(
                port,
                "/oauth2/callback?code=SYNTHETIC_ERROR_CODE&state=SYNTHETIC_ERROR_STATE",
            )
            response.read()
            connection.close()
            assert response.status == 502
            time.sleep(0.1)
            assert "SYNTHETIC_ERROR_CODE" not in log.read_text()
            assert "SYNTHETIC_ERROR_STATE" not in log.read_text()
            print(
                "PASS failed-upstream OAuth callback does not leak query credentials to error logs"
            )
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=15) == 0
            for closed_port in (port, dex_port, telemetry_port):
                with socket.socket() as sock:
                    assert sock.connect_ex(("127.0.0.1", closed_port)) != 0
            logs = log.read_text()
            assert (
                "synthetic-github-secret" not in logs
                and "synthetic-client-secret" not in logs
            )
            assert "code=synthetic" not in logs
            print(
                "PASS actual SIGTERM stops nginx + Dex listeners; synthetic credentials/query omitted from logs"
            )
        except BaseException:
            # This fixture is synthetic-only; logs are safe for diagnostics, not production.
            print(
                log.read_text() if log.exists() else "No process log", file=sys.stderr
            )
            raise
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
