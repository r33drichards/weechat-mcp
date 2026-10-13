#!/usr/bin/env python3
"""Build/run real auth images behind a local TLS fixture. No GitHub login.

Requires Linux Docker. Verifies actual Pomerium/Envoy startup, Dex discovery,
MCP rejection/discovery, initial OIDC redirects/cookies, auth route isolation,
root-owned volume initialization, non-root processes, and restart persistence.
Does not claim authenticated owner/other-user login or an MCP data roundtrip.
"""

import base64
import hashlib
import http.client
import json
import re
import secrets
import shutil
import ssl
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

AUTH = "auth-test.up.railway.app"
MCP = "mcp-test.up.railway.app"


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, text=True, **kwargs)


def main():
    if not shutil.which("docker"):
        raise SystemExit("Docker unavailable: image/topology validation was NOT run")
    root = Path(__file__).resolve().parent.parent
    suffix = secrets.token_hex(5)
    network = "weechat-auth-test-" + suffix
    auth_image, pomerium_image = "weechat-auth:" + suffix, "weechat-pomerium:" + suffix
    names = []
    volumes = []
    with tempfile.TemporaryDirectory(prefix="auth-container-test-") as directory:
        fixtures = Path(directory)
        fixtures.chmod(0o755)
        cert, key = fixtures / "ca.pem", fixtures / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=synthetic-topology-test",
                "-addext",
                f"subjectAltName=DNS:{AUTH},DNS:{MCP},IP:127.0.0.1",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            check=True,
            capture_output=True,
        )
        cert.chmod(0o644)
        key.chmod(0o644)  # Synthetic ephemeral test key only, readable by UID 10001.
        secret = base64.b64encode(secrets.token_bytes(32)).decode()
        backend_token = secrets.token_urlsafe(32)
        idp_secret = secrets.token_urlsafe(32)
        common = {
            "AUTH_ORIGIN": "https://" + AUTH,
            "MCP_ORIGIN": "https://" + MCP,
            "OWNER_EMAIL": "owner@example.test",
            "OWNER_GITHUB_ID": "12345678",
            "BACKEND_ORIGIN": "http://127.0.0.1:9",
            "MCP_AUTH_TOKEN": backend_token,
            "POMERIUM_INTERNAL_ORIGIN": "http://pomerium:8080",
            "PORT": "8080",
            "SHARED_SECRET": secret,
            "COOKIE_SECRET": secret,
            "IDP_CLIENT_SECRET": idp_secret,
            "POMERIUM_CLIENT_SECRET": idp_secret,
            "GITHUB_CLIENT_ID": "synthetic-client",
            "GITHUB_CLIENT_SECRET": "synthetic-secret",
            "SSL_CERT_FILE": "/fixtures/ca.pem",
            "GOMAXPROCS": "2",
        }
        envfile = fixtures / "runtime.env"
        envfile.write_text("".join(f"{k}={v}\n" for k, v in common.items()))
        envfile.chmod(0o600)
        edge = fixtures / "edge.conf"
        edge.write_text(f"""worker_processes 1;
pid /tmp/edge.pid;
error_log stderr warn;
events {{ worker_connections 128; }}
http {{
 access_log off;
 client_body_temp_path /tmp/body;
 proxy_temp_path /tmp/proxy;
 fastcgi_temp_path /tmp/fastcgi;
 uwsgi_temp_path /tmp/uwsgi;
 scgi_temp_path /tmp/scgi;
 server {{
  listen 8443 ssl;
  ssl_certificate /fixtures/ca.pem;
  ssl_certificate_key /fixtures/key.pem;
  server_name {AUTH};
  location / {{ proxy_pass http://auth:8080; proxy_set_header Host {AUTH}; proxy_set_header X-Forwarded-Proto https; proxy_buffering off; }}
 }}
 server {{
  listen 8443 ssl;
  ssl_certificate /fixtures/ca.pem;
  ssl_certificate_key /fixtures/key.pem;
  server_name {MCP};
  location / {{ proxy_pass http://pomerium:8080; proxy_set_header Host {MCP}; proxy_set_header X-Forwarded-Proto https; proxy_buffering off; }}
 }}
}}""")
        edge.chmod(0o644)
        try:
            docker(
                "build", "-f", "deploy/auth/Dockerfile", "-t", auth_image, ".", cwd=root
            )
            docker(
                "build",
                "-f",
                "deploy/pomerium/Dockerfile",
                "-t",
                pomerium_image,
                ".",
                cwd=root,
            )
            docker("network", "create", network, stdout=subprocess.DEVNULL)

            def volume_mount(volume_name, readonly=False):
                return f"type=volume,src={volume_name},dst=/data,volume-nocopy" + (
                    ",readonly" if readonly else ""
                )

            service_volumes = {}
            for label, image in (("auth", auth_image), ("pomerium", pomerium_image)):
                volume_name = network + "-" + label + "-data"
                docker("volume", "create", volume_name, stdout=subprocess.DEVNULL)
                volumes.append(volume_name)
                service_volumes[label] = volume_name
                # Without volume-nocopy Docker would copy image ownership and hide
                # the real Railway failure. Assert the initial mounted state.
                docker(
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--mount",
                    volume_mount(volume_name),
                    "--entrypoint",
                    "python",
                    image,
                    "-c",
                    "from pathlib import Path; p=Path('/data'); "
                    "s=p.stat(); assert (s.st_uid,s.st_gid)==(0,0); "
                    "assert not list(p.iterdir())",
                )

            def start(label, image, extra=(), command=()):
                name = network + "-" + label
                names.append(name)
                docker(
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--network",
                    network,
                    "--network-alias",
                    label,
                    "--env-file",
                    str(envfile),
                    "--mount",
                    f"type=bind,src={fixtures},dst=/fixtures,readonly",
                    *(
                        ("--mount", volume_mount(service_volumes[label]))
                        if label in service_volumes
                        else ()
                    ),
                    *extra,
                    image,
                    *command,
                    stdout=subprocess.DEVNULL,
                )
                return name

            # A single fixture TLS listener must also be reachable on 443 inside the
            # Docker network, because production origins have no non-default port.
            edge.write_text(
                edge.read_text().replace("listen 8443 ssl", "listen 443 ssl")
            )
            auth = start("auth", auth_image, ("--cpus", "0.5", "--memory", "512m"))
            pom = start("pomerium", pomerium_image, ("--cpus", "0.5", "--memory", "1g"))
            gateway = start(
                "edge",
                auth_image,
                (
                    "--user",
                    "0:0",
                    "--network-alias",
                    AUTH,
                    "--network-alias",
                    MCP,
                    "-p",
                    "127.0.0.1::443",
                    "--entrypoint",
                    "nginx",
                ),
                ("-e", "stderr", "-c", "/fixtures/edge.conf", "-g", "daemon off;"),
            )
            published = docker(
                "port", gateway, "443/tcp", capture_output=True
            ).stdout.strip()
            port = int(published.rsplit(":", 1)[1])
            context = ssl.create_default_context(cafile=str(cert))

            def request(url, method="GET", data=None, headers=None):
                parsed = urlsplit(url)
                assert parsed.hostname in (AUTH, MCP), "Test refused a non-fixture host"
                conn = http.client.HTTPSConnection(
                    "127.0.0.1", port, context=context, timeout=10
                )
                request_headers = {"Host": parsed.hostname, **(headers or {})}
                body = json.dumps(data) if isinstance(data, dict) else data
                conn.request(
                    method,
                    parsed.path + ("?" + parsed.query if parsed.query else ""),
                    body,
                    request_headers,
                )
                result = conn.getresponse()
                status, response_headers, payload = (
                    result.status,
                    result.headers,
                    result.read(),
                )
                conn.close()
                assert backend_token.encode() not in payload
                return status, response_headers, payload

            def wait_ready():
                deadline = time.monotonic() + 90
                previous = None
                while True:
                    status = status_mcp = None
                    challenge_present = False
                    error = None
                    try:
                        status, _, body = request(
                            "https://" + AUTH + "/dex/.well-known/openid-configuration"
                        )
                        status_mcp, headers, _ = request("https://" + MCP + "/mcp")
                        challenge_present = "WWW-Authenticate" in headers
                    except (OSError, http.client.HTTPException) as exc:
                        error = type(exc).__name__  # Never serialize URLs/credentials.
                    current = (status, status_mcp, challenge_present, error)
                    if current != previous:
                        print(
                            f"Readiness auth={status} mcp={status_mcp} "
                            f"www_authenticate={challenge_present} error={error}",
                            flush=True,
                        )
                        previous = current
                    if status == 200 and status_mcp == 401:
                        return body, headers
                    if time.monotonic() > deadline:
                        raise AssertionError(
                            "Real container topology did not become ready: "
                            f"auth={status} mcp={status_mcp} "
                            f"www_authenticate={challenge_present} error={error}"
                        )
                    time.sleep(0.5)

            def check_processes(name):
                docker(
                    "exec",
                    "--user",
                    "10001:10001",
                    name,
                    "python",
                    "-c",
                    """
import os
from pathlib import Path
seen = 0
for path in Path('/proc').glob('[0-9]*/status'):
    if path.parent.name == str(os.getpid()):
        continue  # This Docker exec probe did not pass through the initializer.
    try:
        fields = dict(line.split(':', 1) for line in path.read_text().splitlines())
    except FileNotFoundError:
        continue
    assert fields['Uid'].split() == ['10001'] * 4, 'Root/foreign process UID'
    assert fields['Gid'].split() == ['10001'] * 4, 'Root/foreign process GID'
    assert not fields['Groups'].split(), 'Unexpected supplementary groups'
    seen += 1
assert seen >= 2, 'Service processes are missing'
p = Path('/data')
s = p.stat()
assert (s.st_uid, s.st_gid, s.st_mode & 0o777) == (10001, 10001, 0o700)
""",
                )

            body, headers = wait_ready()
            for name in (auth, pom):
                check_processes(name)
            print(
                "PASS root-owned empty volume boot; all service process identities are non-root"
            )
            discovery = json.loads(body)
            assert discovery["issuer"] == "https://" + AUTH + "/dex"
            status, _, body = request(discovery["jwks_uri"])
            assert status == 200
            dex_keys = json.loads(body)
            match = re.search(
                r'resource_metadata="([^"]+)"', headers["WWW-Authenticate"]
            )
            assert match is not None
            metadata_url = match.group(1)
            status, _, body = request(metadata_url)
            assert status == 200
            resource = json.loads(body)
            issuer = resource["authorization_servers"][0]
            status, _, body = request(
                issuer + "/.well-known/oauth-authorization-server"
            )
            assert status == 200
            metadata = json.loads(body)
            assert metadata["issuer"] == issuer
            assert "S256" in metadata["code_challenge_methods_supported"]
            status, _, body = request(
                metadata["registration_endpoint"],
                "POST",
                {
                    "client_name": "Synthetic test",
                    "redirect_uris": ["http://localhost:9999/callback"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
                {"Content-Type": "application/json"},
            )
            assert status in (200, 201)
            client_id = json.loads(body)["client_id"]
            challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(b"synthetic-verifier-" * 4).digest()
                )
                .decode()
                .rstrip("=")
            )
            url = (
                metadata["authorization_endpoint"]
                + "?"
                + urlencode(
                    {
                        "response_type": "code",
                        "client_id": client_id,
                        "redirect_uri": "http://localhost:9999/callback",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "state": "synthetic-state",
                        "resource": resource["resource"],
                    }
                )
            )
            authorization_url = url

            def start_oidc(url):
                cookies = []
                for _ in range(8):
                    status, response_headers, _ = request(url)
                    cookies.extend(response_headers.get_all("Set-Cookie", []))
                    assert status in (302, 303), (
                        f"Expected initial login redirect, got {status}"
                    )
                    next_url = urljoin(url, response_headers["Location"])
                    parsed = urlsplit(next_url)
                    if parsed.hostname == AUTH and parsed.path.startswith("/dex/auth"):
                        assert cookies, "Expected authentication/CSRF cookies"
                        return cookies
                    url = next_url
                raise AssertionError(
                    "Pomerium did not redirect to the shared auth-origin Dex issuer"
                )

            cookies = start_oidc(authorization_url)
            for cookie in cookies:
                lowered = cookie.lower()
                assert (
                    "secure" in lowered
                    and "httponly" in lowered
                    and "domain=" not in lowered
                )
            for path in (
                "/mcp",
                "/metrics",
                "/debug/pprof",
                "/.pomerium/mcp/authorize",
                "/.pomerium/callback/",
            ):
                assert request("https://" + AUTH + path)[0] == 404
            assert (
                request(
                    "https://" + MCP + "/mcp",
                    headers={"Authorization": "Bearer forged"},
                )[0]
                == 401
            )
            for name in (auth, pom, gateway):
                state = json.loads(docker("inspect", name, capture_output=True).stdout)[
                    0
                ]["State"]
                assert state["Running"]
            print(
                "PASS real images: boot, MCP 401/discovery/DCR/PKCE start, OIDC redirect and host-only secure cookies; auth route isolation"
            )
            print(
                "NOT TESTED: GitHub consent/login, owner/other-user policy, token exchange, upstream header replacement, MCP streaming, Railway"
            )
            # Verify both actual databases exist on the mounts, and retain a
            # synthetic marker across container recreation (not just process restart).
            for name, state_path in ((auth, "dex.db"), (pom, "databroker")):
                docker(
                    "exec",
                    "--user",
                    "10001:10001",
                    name,
                    "python",
                    "-c",
                    "from pathlib import Path; p=Path('/data'); "
                    f"assert (p/{state_path!r}).exists(); "
                    "(p/'restart-marker').write_text('persistent fixture')",
                )
            docker("stop", "--time", "15", auth, pom, stdout=subprocess.DEVNULL)
            for name in (auth, pom):
                state = json.loads(docker("inspect", name, capture_output=True).stdout)[
                    0
                ]["State"]
                assert not state["Running"] and state["ExitCode"] == 0
                docker("rm", name, stdout=subprocess.DEVNULL)
                names.remove(name)
            auth = start("auth", auth_image, ("--cpus", "0.5", "--memory", "512m"))
            pom = start("pomerium", pomerium_image, ("--cpus", "0.5", "--memory", "1g"))
            # nginx resolves the fixture service IPs at startup; recreation may
            # assign different IPs. Production auth routing uses a runtime resolver.
            docker("restart", gateway, stdout=subprocess.DEVNULL)
            wait_ready()
            for name in (auth, pom):
                check_processes(name)
                docker(
                    "exec",
                    "--user",
                    "10001:10001",
                    name,
                    "python",
                    "-c",
                    "from pathlib import Path; "
                    "assert Path('/data/restart-marker').read_text() == 'persistent fixture'",
                )
            status, _, body = request(discovery["jwks_uri"])
            assert status == 200 and json.loads(body) == dex_keys, (
                "Dex signing keys did not persist"
            )
            start_oidc(
                authorization_url
            )  # The original DCR client must remain registered.
            print(
                "PASS container recreation: persistent markers, Dex keys and registered MCP client"
            )
            docker(
                "stop", "--time", "15", auth, pom, gateway, stdout=subprocess.DEVNULL
            )
            for name in (auth, pom, gateway):
                state = json.loads(docker("inspect", name, capture_output=True).stdout)[
                    0
                ]["State"]
                assert not state["Running"] and state["ExitCode"] == 0
            print("PASS actual container SIGTERM shutdown")
            for label, image in (("auth", auth_image), ("pomerium", pomerium_image)):
                result = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--mount",
                        volume_mount(service_volumes[label], readonly=True),
                        image,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                assert result.returncode == 1, "Read-only storage did not fail closed"
                assert "Volume initialization failed" in result.stderr
            print("PASS read-only volumes fail before rendering or starting services")
        except BaseException:
            # No real secrets used. Avoid logging OAuth redirect/access tokens anyway.
            for name in names:
                result = subprocess.run(
                    ["docker", "logs", "--tail", "100", name],
                    capture_output=True,
                    text=True,
                )
                logs = result.stdout + result.stderr
                for value in (secret, backend_token, idp_secret):
                    logs = logs.replace(value, "[synthetic-redacted]")
                print(f"{name}: {logs}")
            raise
        finally:
            for name in reversed(names):
                subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            subprocess.run(["docker", "network", "rm", network], capture_output=True)
            for volume_name in reversed(volumes):
                subprocess.run(
                    ["docker", "volume", "rm", volume_name], capture_output=True
                )


if __name__ == "__main__":
    main()
