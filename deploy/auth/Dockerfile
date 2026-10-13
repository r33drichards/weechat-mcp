# Build from repository root: docker build -f deploy/auth/Dockerfile .
FROM ghcr.io/dexidp/dex:v2.46.0@sha256:933fcd3f523338c847b88ef84a7145fb2fcb7b9917f08418612afeaaa2001777 AS dex
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install --no-install-recommends -y nginx ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home auth \
    && mkdir -p /data /srv/dex/web
COPY --from=dex /usr/local/bin/dex /usr/local/bin/dex
COPY --from=dex /srv/dex/web /srv/dex/web
WORKDIR /app
COPY deploy/pomerium_keys.py deploy/render_config.py deploy/service_entrypoint.py deploy/volume_entrypoint.py deploy/dex.yaml.template deploy/router.conf.template ./
# Only volume initialization runs as root; the supervisor/services run as UID 10001.
USER 0:0
ENV HOME=/home/auth XDG_CACHE_HOME=/home/auth/.cache
EXPOSE 8080
ENTRYPOINT ["python", "-I", "/app/volume_entrypoint.py", "auth"]
