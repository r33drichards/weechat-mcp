#!/usr/bin/env python3
"""Render explicit deployment inputs into private YAML; never print credentials."""

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing environment variable: {name}")
    return value


def origin(name, public=False):
    value = required(name).rstrip("/")
    parsed = urlsplit(value)
    schemes = ("https",) if public else ("http", "https")
    if (
        parsed.scheme not in schemes
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{name} must be an origin with no path, query, or credentials"
        )
    return value


def main():
    root = Path(__file__).resolve().parent
    values = {
        name: origin(name, True) for name in ("MCP_ORIGIN", "AUTH_ORIGIN", "DEX_ORIGIN")
    }
    if len(set(values.values())) != 3:
        raise ValueError("This template requires three distinct public origins")
    values.update(
        {name: origin(name) for name in ("BACKEND_ORIGIN", "DEX_INTERNAL_ORIGIN")}
    )
    values["OWNER_EMAIL"] = required("OWNER_EMAIL")
    if "@" not in values["OWNER_EMAIL"] or any(
        c.isspace() for c in values["OWNER_EMAIL"]
    ):
        raise ValueError("OWNER_EMAIL must be the exact owner email returned by Dex")
    token = required("MCP_AUTH_TOKEN")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", token):
        raise ValueError(
            "MCP_AUTH_TOKEN must contain at least 32 URL-safe random characters"
        )
    values["BACKEND_AUTHORIZATION"] = "Bearer " + token
    values["DEX_ISSUER"] = values["DEX_ORIGIN"] + "/dex"
    values["DEX_CALLBACK"] = values["DEX_ISSUER"] + "/callback"
    values["AUTH_CALLBACK"] = values["AUTH_ORIGIN"] + "/oauth2/callback"
    output = root / "generated"
    output.mkdir(mode=0o700, exist_ok=True)
    for name in ("pomerium", "dex"):
        text = (root / f"{name}.yaml.template").read_text()
        for key, value in values.items():
            text = text.replace("@@" + key + "@@", json.dumps(value))
        if "@@" in text:
            raise ValueError(f"Unresolved template variable in {name}")
        destination = output / f"{name}.yaml"
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        destination.chmod(0o600)
    print(
        "Rendered private deployment configuration in deploy/generated (not committed)."
    )


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
