# printlog-bridge in remote mode: watches printers over Moonraker's WebSocket from any host on
# the network (a NAS, a Pi, a home server). Config, state and the API key live in /data.
#
#   docker run -d --name printlog-bridge --restart unless-stopped --network host \
#     -v /volume1/docker/printlog-bridge:/data ghcr.io/hoffmanengineering/printlog-bridge
#
# With no config it pairs (a QR code in the log), writes a starter printlog-bridge.cfg in
# /data and waits for printers to be added to it.
#
# Built for linux/amd64, linux/arm64 and linux/arm/v7 by .github/workflows/docker.yml. The `addon`
# stage is the Home Assistant app's image (home-assistant/), ghcr.io/hoffmanengineering/
# printlog-bridge-addon; the default target is the normal image.

ARG PYTHON_VERSION=3.12

# The virtualenv is built here, with a compiler in case a platform has no wheel for aiohttp or
# its C extensions (linux/arm/v7 may not), and copied into a clean image without it.
FROM python:${PYTHON_VERSION}-slim-bookworm AS build
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/printlog-bridge
COPY requirements.txt /src/
RUN /opt/printlog-bridge/bin/pip install -r /src/requirements.txt
COPY pyproject.toml README.md LICENSE /src/
COPY src /src/src
RUN /opt/printlog-bridge/bin/pip install --no-deps /src \
    && { /opt/printlog-bridge/bin/pip uninstall -y pip setuptools wheel || true; }

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
LABEL org.opencontainers.image.title="printlog-bridge" \
      org.opencontainers.image.description="Logs every Klipper/Moonraker print to 3D Print Log automatically." \
      org.opencontainers.image.source="https://github.com/HoffmanEngineering/printlog-bridge" \
      org.opencontainers.image.licenses="MIT"

# The bridge's own user, 1000:1000. The container starts as root, and every printlog-bridge
# command (run, and anything run with `docker exec`) first gives /data to the user it runs as
# and switches to it (cli/data_owner.py): PUID/PGID when set, else the owner of the folder
# mounted at /data unless that's root, else this user. So a NAS needs no permission step. A
# container started with `user: "<uid>:<gid>"` runs as that user throughout, as before.
RUN groupadd --gid 1000 printlog \
    && useradd --uid 1000 --gid 1000 --home-dir /data --no-create-home --shell /usr/sbin/nologin printlog \
    && mkdir /data && chown 1000:1000 /data

COPY --from=build /opt/printlog-bridge /opt/printlog-bridge

# HOME is /data too, so `~` in the config and anything else that writes to the home folder land
# in the volume, whichever uid the container runs as. TZ only changes the times in the log.
ENV PATH=/opt/printlog-bridge/bin:$PATH \
    HOME=/data \
    PRINTLOG_BRIDGE_CONFIG=/data/printlog-bridge.cfg \
    PRINTLOG_BRIDGE_DATA_DIR=/data \
    PRINTLOG_BRIDGE_STATE_DIR=/data/state \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Etc/UTC

USER root
WORKDIR /data
VOLUME ["/data"]

# Exits 0 only while the bridge runs (its status file is fresh and it hasn't stopped), by the same
# rule as `printlog-bridge status`. A lighter command than `status`, which imports aiohttp and
# cost a NAS about a second of CPU every minute.
HEALTHCHECK --interval=60s --timeout=20s --start-period=30s --retries=3 \
    CMD ["printlog-bridge-health"]

ENTRYPOINT ["printlog-bridge"]
CMD ["run"]

# The Home Assistant app. The Supervisor gives the app a root-owned /data with its options in
# /data/options.json, so this starts as root: printlog-bridge-addon (cli/addon.py) writes the
# config from the options, gives /data to the printlog user and runs `printlog-bridge run` as
# that user. The health check above is the app's watchdog.
FROM runtime AS addon
USER root
# printlog-bridge-addon hands /data over itself and runs the bridge as the printlog user.
ENV PRINTLOG_BRIDGE_DATA_DIR=
ENTRYPOINT ["printlog-bridge-addon"]
CMD []

# The default target: the normal image, exactly as the runtime stage built it.
FROM runtime
