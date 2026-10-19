# Build from repository root: docker build -f deploy/auth/Dockerfile .
FROM ghcr.io/dexidp/dex:v2.46.0@sha256:933fcd3f523338c847b88ef84a7145fb2fcb7b9917f08418612afeaaa2001777 AS dex-assets
# Compiler digest matches the pinned Dex v2.46.0 source Dockerfile.
FROM golang:1.27.1-alpine3.24@sha256:8a5910f31396cd4d89662f56c68b3ae31d374308270a1c3bd96672ee5ed43414 AS build
RUN apk add --no-cache git build-base bash
WORKDIR /src/dex
RUN git init . \
    && git remote add origin https://github.com/dexidp/dex.git \
    && git fetch --depth=1 origin 05a275057112ba93e17db21f600b756f000f1c21 \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = 05a275057112ba93e17db21f600b756f000f1c21
# Extract the exact reviewed coordinator for the real-Dex acceptance harness.
RUN git init /src/pomerium \
    && git -C /src/pomerium remote add origin https://github.com/pomerium/pomerium.git \
    && git -C /src/pomerium fetch --depth=1 origin 76042ed40db3bab9506b689f436716b6df624f23 \
    && git -C /src/pomerium checkout --detach FETCH_HEAD \
    && test "$(git -C /src/pomerium rev-parse HEAD)" = 76042ed40db3bab9506b689f436716b6df624f23
COPY deploy/pomerium/refresh-authority /repair
RUN git -C /src/pomerium apply --check /repair/pomerium-refresh-authority.patch \
    && git -C /src/pomerium apply /repair/pomerium-refresh-authority.patch
ENV CGO_ENABLED=1
RUN sh /repair/dex-acceptance/run.sh /src/dex /src/pomerium
RUN make release-binary VERSION=v2.46.0-weechat-refresh

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install --no-install-recommends -y nginx ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home auth \
    && mkdir -p /data /srv/dex/web
COPY --from=build /go/bin/dex /usr/local/bin/dex
COPY --from=dex-assets /srv/dex/web /srv/dex/web
WORKDIR /app
COPY deploy/pomerium_keys.py deploy/render_config.py deploy/service_entrypoint.py deploy/volume_entrypoint.py deploy/dex.yaml.template deploy/router.conf.template ./
# Only volume initialization runs as root; the supervisor/services run as UID 10001.
USER 0:0
ENV HOME=/home/auth XDG_CACHE_HOME=/home/auth/.cache
EXPOSE 8080
ENTRYPOINT ["python", "-I", "/app/volume_entrypoint.py", "auth"]
