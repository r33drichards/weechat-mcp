# Build from repository root: docker build -f deploy/pomerium/Dockerfile .
FROM pomerium/pomerium:v0.33.3 AS pomerium
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install --no-install-recommends -y ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home pomerium \
    && mkdir -p /data
COPY --from=pomerium /bin/pomerium /usr/local/bin/pomerium
WORKDIR /app
COPY deploy/render_config.py deploy/service_entrypoint.py deploy/volume_entrypoint.py deploy/pomerium.yaml.template ./
# Only volume initialization runs as root; the supervisor/services run as UID 10001.
USER 0:0
ENV HOME=/home/pomerium XDG_CACHE_HOME=/home/pomerium/.cache
EXPOSE 8080
ENTRYPOINT ["python", "-I", "/app/volume_entrypoint.py", "pomerium"]
