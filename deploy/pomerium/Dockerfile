# Build from repository root: docker build -f deploy/pomerium/Dockerfile .
FROM pomerium/pomerium:v0.33.3 AS pomerium
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install --no-install-recommends -y ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home pomerium \
    && mkdir -p /data && chown 10001:10001 /data
COPY --from=pomerium /bin/pomerium /usr/local/bin/pomerium
WORKDIR /app
COPY deploy/render_config.py deploy/service_entrypoint.py deploy/pomerium.yaml.template ./
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "/app/service_entrypoint.py", "pomerium"]
