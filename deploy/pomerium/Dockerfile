# Build from repository root. No prepublished private base image is required.
# Toolchain images match the exact pinned Pomerium source Dockerfile.
FROM golang:1.26.8-bookworm@sha256:9fdc884aacc3bec89b20ffc69f4bb369c78210e3e4f600387b5128b12c199f81 AS source
WORKDIR /src
RUN git init . \
    && git remote add origin https://github.com/pomerium/pomerium.git \
    && git fetch --depth=1 origin 76042ed40db3bab9506b689f436716b6df624f23 \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = 76042ed40db3bab9506b689f436716b6df624f23
COPY deploy/pomerium/refresh-authority/pomerium-refresh-authority.patch /tmp/refresh-authority.patch
RUN git apply --check /tmp/refresh-authority.patch && git apply /tmp/refresh-authority.patch

FROM node:24.20.0-bookworm@sha256:be23f54a88d34e8824c741b19b91064094f92c1c97b194144bfc8b50d67258e2 AS ui
WORKDIR /build
COPY --from=source /src/.git ./.git
COPY --from=source /src/Makefile ./Makefile
COPY --from=source /src/ui/package-lock.json ./ui/package-lock.json
COPY --from=source /src/ui/package.json ./ui/package.json
RUN make npm-install
COPY --from=source /src/ui/ ./ui/
RUN make build-ui

FROM golang:1.26.8-bookworm@sha256:9fdc884aacc3bec89b20ffc69f4bb369c78210e3e4f600387b5128b12c199f81 AS build
WORKDIR /src
RUN apt-get update && apt-get install --no-install-recommends -y zip \
    && rm -rf /var/lib/apt/lists/*
COPY --from=source /src/ ./
COPY --from=ui /build/ui/dist ./ui/dist
RUN go test -race ./pkg/identity/refreshcoord ./pkg/identity/oidc ./pkg/identity/manager ./internal/mcp
RUN make build-go NAME=pomerium VERSION=v0.33.3

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install --no-install-recommends -y ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home pomerium \
    && mkdir -p /data
COPY --from=build /src/bin/pomerium /usr/local/bin/pomerium
WORKDIR /app
COPY deploy/pomerium_keys.py deploy/render_config.py deploy/service_entrypoint.py deploy/volume_entrypoint.py deploy/pomerium.yaml.template ./
# Only volume initialization runs as root; the supervisor/services run as UID 10001.
USER 0:0
ENV HOME=/home/pomerium XDG_CACHE_HOME=/home/pomerium/.cache \
    WEECHAT_REFRESH_AUTHORITY_DIR=/data/databroker.refresh-authority
EXPOSE 8080
ENTRYPOINT ["python", "-I", "/app/volume_entrypoint.py", "pomerium"]
