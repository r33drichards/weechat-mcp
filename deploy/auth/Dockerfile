# Build from repository root: docker build -f deploy/auth/Dockerfile .
FROM ghcr.io/dexidp/dex:v2.45.1 AS dex
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install --no-install-recommends -y nginx ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home auth \
    && mkdir -p /data /srv/dex/web && chown 10001:10001 /data
COPY --from=dex /usr/local/bin/dex /usr/local/bin/dex
COPY --from=dex /srv/dex/web /srv/dex/web
WORKDIR /app
COPY deploy/render_config.py deploy/service_entrypoint.py deploy/dex.yaml.template deploy/router.conf.template ./
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "/app/service_entrypoint.py", "auth"]
