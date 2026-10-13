#!/usr/bin/env python3
"""Render the two-public-origin Railway layout using private runtime inputs."""

import argparse
import ipaddress
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def required(name, env):
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing environment variable: {name}")
    return value


def origin(name, env, public=False):
    value = required(name, env).rstrip("/")
    parsed = urlsplit(value)
    schemes = ("https",) if public else ("http", "https")
    if (
        parsed.scheme not in schemes
        or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in value)
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"[A-Za-z0-9.:[\]-]+", parsed.netloc)
    ):
        raise ValueError(f"{name} must be an origin without path or credentials")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError(f"{name} has an invalid port")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{ipaddress.IPv6Address(host)}]"
    authority = host
    if (
        parsed.port is not None
        and parsed.port != {"http": 80, "https": 443}[parsed.scheme]
    ):
        authority += f":{parsed.port}"
    return parsed.scheme + "://" + authority


def resolver_addresses(path=Path("/etc/resolv.conf")):
    addresses = []
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "nameserver":
            address = ipaddress.ip_address(fields[1])
            addresses.append(f"[{address}]" if address.version == 6 else str(address))
    if not addresses:
        raise ValueError("No DNS nameserver found for the private Pomerium upstream")
    return " ".join(addresses)


def backend_headers(env, backend_origin):
    mode = env.get("MCP_AUTH_MODE", "bearer")
    if mode == "railway-private":
        for name in ("RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID"):
            required(name, env)
        if not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.railway\.internal",
            urlsplit(backend_origin).hostname or "",
        ):
            raise ValueError(
                "railway-private requires a Railway-private BACKEND_ORIGIN"
            )
        # End-user OAuth terminates here. Never forward it to the private bridge.
        return "    remove_request_headers: [Authorization]"
    if mode != "bearer":
        raise ValueError("MCP_AUTH_MODE must be bearer or railway-private")
    token = required("MCP_AUTH_TOKEN", env)
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", token):
        raise ValueError("MCP_AUTH_TOKEN must have at least 32 URL-safe characters")
    return "    set_request_headers:\n      Authorization: " + json.dumps(
        "Bearer " + token
    )


def configuration(service, env):
    values = {"AUTH_ORIGIN": origin("AUTH_ORIGIN", env, True)}
    values["DEX_ISSUER"] = values["AUTH_ORIGIN"] + "/dex"
    values["DEX_CALLBACK"] = values["DEX_ISSUER"] + "/callback"
    values["AUTH_CALLBACK"] = values["AUTH_ORIGIN"] + "/oauth2/callback"
    if service in ("all", "pomerium"):
        values["MCP_ORIGIN"] = origin("MCP_ORIGIN", env, True)
        if values["MCP_ORIGIN"].lower() == values["AUTH_ORIGIN"].lower():
            raise ValueError("MCP_ORIGIN and AUTH_ORIGIN must be distinct")
        values["BACKEND_ORIGIN"] = origin("BACKEND_ORIGIN", env)
        values["OWNER_EMAIL"] = required("OWNER_EMAIL", env)
        if "@" not in values["OWNER_EMAIL"] or any(
            c.isspace() for c in values["OWNER_EMAIL"]
        ):
            raise ValueError("OWNER_EMAIL must be the exact email returned by Dex")
        values["BACKEND_HEADERS"] = backend_headers(env, values["BACKEND_ORIGIN"])
    if service in ("all", "auth"):
        values["POMERIUM_INTERNAL_ORIGIN"] = origin("POMERIUM_INTERNAL_ORIGIN", env)
        values["AUTH_AUTHORITY"] = urlsplit(values["AUTH_ORIGIN"]).netloc
        values["DNS_RESOLVERS"] = resolver_addresses()
        port = env.get("PORT", "8080")
        if not re.fullmatch(r"[0-9]+", port) or not 1024 <= int(port) <= 65535:
            raise ValueError("PORT must be an unprivileged TCP port (1024-65535)")
        values["PORT"] = port
    return values


def render(service, env, output):
    values = configuration(service, env)
    root = Path(__file__).resolve().parent
    names = {
        "all": ("pomerium", "dex", "router"),
        "pomerium": ("pomerium",),
        "auth": ("dex", "router"),
    }[service]
    # The directory must be private even when it pre-existed. Never follow links.
    if any(
        path.is_symlink() for path in (output.absolute(), *output.absolute().parents)
    ):
        raise ValueError("Output directory must not be a symlink")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.chmod(0o700)
    for name in names:
        extension = "conf" if name == "router" else "yaml"
        text = (root / f"{name}.{extension}.template").read_text()
        if name == "pomerium":
            # Insert the fixed fragment before scalar interpolation, so user
            # input cannot introduce a raw-fragment placeholder of its own.
            text = text.replace("@@BACKEND_HEADERS@@", values["BACKEND_HEADERS"])
        for key, value in values.items():
            if key == "BACKEND_HEADERS":
                continue
            # Nginx interpolations are strict validated origins, authority, IPs, port.
            replacement = value if name == "router" else json.dumps(value)
            text = text.replace("@@" + key + "@@", replacement)
        if "@@" in text:
            raise ValueError(f"Unresolved template variable in {name}")
        destination = output / f"{name}.{extension}"
        with tempfile.NamedTemporaryFile(mode="w", dir=output, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=("all", "pomerium", "auth"), default="all")
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).parent / "generated"
    )
    args = parser.parse_args()
    render(args.service, os.environ, args.output)
    print("Rendered private configuration; values were not logged.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)
