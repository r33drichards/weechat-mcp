# SPDX-License-Identifier: GPL-3.0-or-later
FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM python:3.12-slim-trixie

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates weechat-headless weechat-plugins weechat-python \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 weechat
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-editable --no-cache
COPY bridge.py weechat_mcp.py container_entrypoint.py LICENSE ./
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER weechat
# HTTP requires a bearer unless explicitly using guarded Railway-private mode.
ENTRYPOINT ["python", "/app/container_entrypoint.py"]
