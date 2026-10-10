# A real Moonraker, no Klipper, for the bridge's integration tests.
# Klippy stays "disconnected", which is enough for connection, auth and agent behavior.
FROM python:3.11-slim

RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends git curl libsodium23 build-essential >/dev/null \
 && rm -rf /var/lib/apt/lists/*

ARG MOONRAKER_REF=v0.9.3
RUN git clone -q https://github.com/Arksine/moonraker /moonraker \
 && git -C /moonraker checkout -q "$MOONRAKER_REF" \
 && pip install -q --no-cache-dir -r /moonraker/scripts/moonraker-requirements.txt

# The bridge is installed here at test time, for local-mode tests that run inside the container.
RUN python -m venv /bridge-venv && /bridge-venv/bin/pip install -q --no-cache-dir "aiohttp>=3.8"

RUN mkdir -p /root/printer_data/config /root/printer_data/comms /root/printer_data/logs \
    /root/printer_data/database
COPY moonraker.conf /root/printer_data/config/moonraker.conf

EXPOSE 7125
CMD ["python", "/moonraker/moonraker/moonraker.py", "-d", "/root/printer_data"]
