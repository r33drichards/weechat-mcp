#!/usr/bin/env python3
"""Exercise real Dex PKCE enforcement with the production template and a fake IdP.

Only the connector, loopback listener, issuer and temporary storage are replaced.
No GitHub request is made. No authorization code or token is printed.
"""

import argparse
import base64
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-root", type=Path, default=Path(__file__).parent)
    parser.add_argument("--dex", default="dex")
    parser.add_argument("--dex-web", default="/srv/dex/web")
    args = parser.parse_args()
    sys.path.insert(0, str(args.deployment_root))
    from render_config import render

    port, telemetry = free_port(), free_port()
    callback = "https://auth-test.up.railway.app/oauth2/callback"
    issuer = f"http://127.0.0.1:{port}/dex"
    env = dict(
        os.environ,
        AUTH_ORIGIN="https://auth-test.up.railway.app",
        POMERIUM_INTERNAL_ORIGIN="http://pomerium:8080",
        PORT="8080",
    )
    with tempfile.TemporaryDirectory(prefix="dex-pkce-fixture-") as directory:
        root = Path(directory)
        render("auth", env, root)
        config = root / "dex.yaml"
        source = config.read_text()
        assert "    public: true" in source and "secretEnv:" not in source
        source = source.replace(
            'issuer: "https://auth-test.up.railway.app/dex"',
            "issuer: " + json.dumps(issuer),
        )
        source = source.replace(
            "file: /data/dex.db", "file: " + json.dumps(str(root / "dex.db"))
        )
        source = source.replace("http: 127.0.0.1:5556", f"http: 127.0.0.1:{port}")
        source = source.replace("http: 127.0.0.1:5558", f"http: 127.0.0.1:{telemetry}")
        source = source.replace("dir: /srv/dex/web", "dir: " + json.dumps(args.dex_web))
        source, connector = source.split("connectors:\n", 1)
        assert "type: github" in connector
        source += "connectors:\n  - type: mockCallback\n    id: mock\n    name: Synthetic identity fixture\n"
        config.write_text(source)
        with (root / "dex.log").open("w") as logfile:
            process = subprocess.Popen(
                [args.dex, "serve", str(config)],
                env=env,
                stdout=logfile,
                stderr=logfile,
            )

        def request(path, method="GET", params=None, basic=False):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            headers, body = {}, None
            if params is not None:
                body = urlencode(params)
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            if basic:
                headers["Authorization"] = (
                    "Basic " + base64.b64encode(b"pomerium:").decode()
                )
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            result = response.status, response.headers, response.read()
            connection.close()
            return result

        try:
            deadline = time.monotonic() + 15
            while True:
                assert process.poll() is None, "Real Dex PKCE fixture exited"
                try:
                    status, _, body = request("/dex/.well-known/openid-configuration")
                    if status == 200:
                        break
                except OSError:
                    pass
                assert time.monotonic() < deadline, (
                    "Real Dex PKCE fixture did not start"
                )
                time.sleep(0.05)
            assert json.loads(body)["code_challenge_methods_supported"] == ["S256"]
            verifier = "synthetic-pkce-verifier-0123456789_ABCDEFGHIJKLmno"
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )

            def authorize(mode, redirect=callback):
                query = dict(
                    client_id="pomerium",
                    response_type="code",
                    scope="openid email",
                    redirect_uri=redirect,
                    state="fixture-state",
                )
                if mode:
                    query.update(
                        code_challenge=challenge if mode == "S256" else verifier,
                        code_challenge_method=mode,
                    )
                path = "/dex/auth?" + urlencode(query)
                for _ in range(8):
                    status, headers, _ = request(path)
                    if status not in (302, 303):
                        return status, None
                    target = urlsplit(headers["Location"])
                    if target.scheme + "://" + target.netloc + target.path == callback:
                        return status, parse_qs(target.query)
                    assert not target.netloc or target.netloc == f"127.0.0.1:{port}", (
                        "External redirect refused"
                    )
                    path = target.path + ("?" + target.query if target.query else "")
                raise AssertionError("Too many fixture redirects")

            for mode in (None, "plain"):
                status, params = authorize(mode)
                assert (
                    status == 303
                    and params
                    and params.get("error") == ["invalid_request"]
                )
            for supplied, basic in (
                (None, False),
                ("wrong-verifier", False),
                (verifier, False),
                (verifier, True),
            ):
                status, params = authorize("S256")
                assert status in (302, 303) and params and "code" in params
                form = dict(
                    grant_type="authorization_code",
                    client_id="pomerium",
                    redirect_uri=callback,
                    code=params["code"][0],
                )
                if supplied is not None:
                    form["code_verifier"] = supplied
                status, _, body = request("/dex/token", "POST", form, basic)
                payload = json.loads(body)
                if supplied == verifier:
                    assert (
                        status == 200
                        and payload.get("access_token")
                        and payload.get("id_token")
                    )
                else:
                    assert status == 400 and payload.get("error")
            for redirect in (
                "https://evil.test/oauth2/callback",
                callback.replace("https:", "http:"),
                callback + "/",
                callback + "?extra=yes",
                "http://localhost:9999/callback",
                "urn:ietf:wg:oauth:2.0:oob",
            ):
                assert authorize("S256", redirect)[0] == 400
            print(
                "PASS real Dex public client: mandatory S256, wrong/missing verifier rejected, exact HTTPS callback enforced, valid token exchanges"
            )
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    if not shutil.which("dex") and "--dex" not in sys.argv:
        raise SystemExit("Dex unavailable: real PKCE validation was NOT run")
    main()
