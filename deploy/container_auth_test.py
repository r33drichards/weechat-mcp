#!/usr/bin/env python3
"""Build/run real auth images behind a local TLS fixture. No GitHub login.

Requires Linux Docker. Verifies actual Pomerium/Envoy startup, Dex discovery,
MCP rejection/discovery, initial OIDC redirects/cookies, auth route isolation,
root-owned volume initialization, non-root processes, and restart persistence.
Does not claim authenticated owner/other-user login or an MCP data roundtrip.
"""

import argparse
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
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from refresh_flow_test import RefreshFlow, prepare_fixture

AUTH = "auth-test.up.railway.app"
MCP = "mcp-test.up.railway.app"


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, text=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-flow", action="store_true")
    args = parser.parse_args()
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
        pkce_probe = fixtures / "dex_pkce_test.py"
        shutil.copyfile(root / "deploy/dex_pkce_test.py", pkce_probe)
        pkce_probe.chmod(0o644)
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
        common = {
            "AUTH_ORIGIN": "https://" + AUTH,
            "MCP_ORIGIN": "https://" + MCP,
            "OWNER_EMAIL": "owner@example.test",
            "OWNER_GITHUB_ID": "12345678",
            "BACKEND_ORIGIN": "http://bridge.railway.internal:8000",
            "MCP_AUTH_MODE": "railway-private",
            "POMERIUM_LOCAL_KEYS": "true",
            "RAILWAY_ENVIRONMENT_ID": "synthetic-environment",
            "RAILWAY_SERVICE_ID": "synthetic-auth-topology",
            "POMERIUM_INTERNAL_ORIGIN": "http://pomerium:8080",
            "PORT": "8080",
            "GITHUB_CLIENT_ID": "synthetic-client",
            "GITHUB_CLIENT_SECRET": "synthetic-secret",
            "SSL_CERT_FILE": "/fixtures/ca.pem",
            "GOMAXPROCS": "2",
        }
        auth_extra = (
            prepare_fixture(root, fixtures, common) if args.refresh_flow else ()
        )
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
  location / {{ proxy_http_version 1.1; proxy_set_header Connection ""; proxy_pass http://auth:8080; proxy_set_header Host {AUTH}; proxy_set_header X-Forwarded-Proto https; proxy_buffering off; }}
 }}
 server {{
  listen 8443 ssl;
  ssl_certificate /fixtures/ca.pem;
  ssl_certificate_key /fixtures/key.pem;
  server_name {MCP};
  location / {{ proxy_http_version 1.1; proxy_set_header Connection ""; proxy_pass http://pomerium:8080; proxy_set_header Host {MCP}; proxy_set_header X-Forwarded-Proto https; proxy_buffering off; }}
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
                    *(auth_extra if label == "auth" else ()),
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

            def gateway_port():
                published = docker(
                    "port", gateway, "443/tcp", capture_output=True
                ).stdout.strip()
                return int(published.rsplit(":", 1)[1])

            port = gateway_port()
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
            if not args.refresh_flow:
                docker(
                    "exec",
                    "--user",
                    "10001:10001",
                    auth,
                    "python",
                    "/fixtures/dex_pkce_test.py",
                    "--deployment-root",
                    "/app",
                )

            def persisted_key_digest():
                return docker(
                    "exec",
                    "--user",
                    "10001:10001",
                    pom,
                    "python",
                    "-c",
                    "import hashlib; from pathlib import Path; "
                    "p=Path('/data/pomerium-keys/keys.json'); s=p.stat(); "
                    "assert (s.st_uid,s.st_gid,s.st_mode & 0o777)==(10001,10001,0o600); "
                    "print(hashlib.sha256(p.read_bytes()).hexdigest())",
                    capture_output=True,
                ).stdout.strip()

            # A second gateway using the same state must fail before services start.
            # No token records are read; startup reuses only synthetic fixture keys.
            docker(
                "exec",
                "--user",
                "0:0",
                pom,
                "python",
                "-c",
                """
import subprocess
from pathlib import Path
root = Path('/data/databroker.refresh-authority')
assert root.is_dir() and (root.stat().st_mode & 0o777) == 0o700
lock = root / '.lock'
assert lock.is_file() and (lock.stat().st_mode & 0o777) == 0o600
result = subprocess.run(
    ['python', '-I', '/app/volume_entrypoint.py', 'pomerium'],
    capture_output=True, text=True, timeout=20,
)
assert result.returncode != 0, 'Second gateway unexpectedly started'
output = result.stdout + result.stderr
if 'refresh authority already locked' not in output:
    # Fixed diagnostics only; never emit raw service output or key material.
    known = ('Volume initialization failed', 'Local keys require the fully dropped service identity',
             'Services must not run as root', 'Configuration or service startup failed')
    print('Second-process preflight:', [marker for marker in known if marker in output], flush=True)
    raise AssertionError('Missing process lock rejection')
""",
            )
            print("PASS refresh authority rejects a second process on the same volume")

            original_keys = persisted_key_digest()
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
            flow = (
                RefreshFlow(
                    request, metadata, resource["resource"], AUTH, MCP
                ).establish()
                if args.refresh_flow
                else None
            )
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
                        query = parse_qs(parsed.query)
                        assert query["code_challenge_method"] == ["S256"]
                        assert len(query["code_challenge"][0]) >= 43
                        assert query["client_id"] == ["pomerium"]
                        assert query["redirect_uri"] == [
                            "https://" + AUTH + "/oauth2/callback"
                        ]
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
            # Docker can allocate a new ephemeral host port after a restart.
            port = gateway_port()
            wait_ready()
            if flow is not None:
                flow.after_restart()
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
            assert persisted_key_digest() == original_keys, (
                "Local session keys changed on restart"
            )
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
        except BaseException as exc:
            # Full container logs can contain even synthetic OAuth credentials.
            print(
                "Topology validation failed; container logs omitted to protect token material"
            )
            if args.refresh_flow:
                frames = traceback.extract_tb(exc.__traceback__)
                locations = " -> ".join(
                    f"{Path(frame.filename).name}:{frame.lineno}" for frame in frames
                )
                raise SystemExit(
                    f"Authenticated topology check failed ({type(exc).__name__}) at {locations}; sensitive exception details suppressed"
                ) from None
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
