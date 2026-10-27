# SPDX-License-Identifier: GPL-3.0-or-later
FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM python:3.12-slim-trixie AS standard

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates weechat-headless weechat-plugins weechat-python \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 weechat
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-editable --no-cache
COPY bridge.py native_buffer.py command_manifest.py weechat_mcp.py container_entrypoint.py mcp_events.py jev_filter.py event_protocol.py event_webhooks.py managed_js.py managed_http.py LICENSE ./
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
# Starts as root only to take ownership of an optional WEECHAT_HOME volume mount,
# then permanently drops to the unprivileged weechat user before running anything.
USER 0:0
# HTTP requires a bearer unless explicitly using guarded Railway-private mode.
ENTRYPOINT ["python", "/app/container_entrypoint.py"]

# Optional build only: `docker build --target managed-js ...`. Nix is the
# authoritative patched build; Docker does not carry a separate CMake/patch path.
# Stock builds keep the existing apt-provided WeeChat and skip this stage.
FROM nixos/nix:2.34.9 AS managed-build
WORKDIR /source
COPY . .
RUN nix --extra-experimental-features 'nix-command flakes' build \
      --option sandbox false --cores 2 --max-jobs 2 --no-update-lock-file \
      .#weechat-managed-container --out-link /managed-result \
    && mkdir -p /closure/nix/store /closure/opt \
    && nix-store --query --requisites /managed-result > /runtime-closure \
    && while IFS= read -r store_path; do cp -a "$store_path" /closure/nix/store/; done < /runtime-closure \
    && ln -s "$(readlink -f /managed-result)" /closure/opt/weechat-managed

FROM standard AS managed-js
# Use ALL native plugins from the same fork build: never mix its ABI with the
# stock .so files. Trusted plugins/console remain unrestricted by managed policy.
COPY --from=managed-build /closure/ /
COPY policy/managed.rego /etc/weechat-managed/managed.rego
RUN printf '%s\n' '{"managed":{"buffers":[],"http_methods":[],"http_hosts":[],"log":false}}' > /etc/weechat-managed/data.json \
    && chmod 0444 /etc/weechat-managed/* \
    && install -d -m 0700 -o 10001 -g 10001 /home/weechat/managed-js
ENV WEECHAT_MANAGED_ROOT="/home/weechat/managed-js" \
    WEECHAT_MANAGED_POLICY="/etc/weechat-managed/managed.rego" \
    WEECHAT_MANAGED_DATA="/etc/weechat-managed/data.json"
# Runtime activation still requires WEECHAT_MCP_MANAGED_JS=1 and explicit
# WEECHAT_MCP_FULL_CONTROL=0 (limits the agent's MCP, not trusted native plugins).

# Keep the existing/default image as the final target. With BuildKit the optional
# compiler, fork and evaluator stages are skipped unless explicitly selected.
FROM standard AS default
