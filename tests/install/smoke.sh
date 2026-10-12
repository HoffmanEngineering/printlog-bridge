#!/usr/bin/env bash
# Runs install.sh and uninstall.sh for real, in a throwaway home with a Moonraker data folder, and
# checks what they leave behind. systemctl is a fake that records its calls, and unit files go to
# a temporary folder, so nothing on the machine running it changes. CI runs it (install-smoke) in
# a clean python:3.9-slim container; it needs bash, git, python3 with venv, and the network for pip.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

export HOME="${WORK}/home"
DATA="${HOME}/printer_data"
mkdir -p "${DATA}/config" "${DATA}/comms" "${WORK}/systemd" "${WORK}/bin"

# A moonraker.conf without a newline at its end, and an asvc with Moonraker's defaults.
printf '[server]\nhost: 0.0.0.0\n\n[update_manager mainsail]\ntype: web\nrepo: mainsail-crew/mainsail\npath: ~/mainsail' \
    >"${DATA}/config/moonraker.conf"
printf 'klipper_mcu\nwebcamd\nMoonCord\nKlipperScreen\nmoonraker-telegram-bot\ncrowsnest\n' \
    >"${DATA}/moonraker.asvc"
cp "${DATA}/config/moonraker.conf" "${WORK}/moonraker.conf.before"
cp "${DATA}/moonraker.asvc" "${WORK}/moonraker.asvc.before"

cat >"${WORK}/bin/systemctl" <<EOF
#!/bin/sh
echo "\$*" >>"${WORK}/systemctl.log"
EOF
chmod +x "${WORK}/bin/systemctl"

export PRINTLOG_SYSTEMD_DIR="${WORK}/systemd"
export PRINTLOG_SYSTEMCTL="${WORK}/bin/systemctl"
export PRINTLOG_SUDO=""
export PRINTLOG_ALLOW_ROOT=1 # CI's container runs as root
# No API key, so nothing (install.sh's doctor, or this script's) calls the real 3D Print Log API.
unset PRINTLOG_API_KEY

failures=0
check() {
    local what="$1"
    shift
    if "$@"; then
        echo "ok    $what"
    else
        echo "FAIL  $what"
        failures=$((failures + 1))
    fi
}
called() { grep -qx "$1" "${WORK}/systemctl.log"; }

echo "== install.sh"
"${REPO}/install.sh" --yes --printer-id 42 </dev/null

UNIT="${WORK}/systemd/printlog-bridge.service"
VENV="${HOME}/printlog-bridge-env"
CONFIG="${DATA}/config/printlog-bridge.cfg"
check "the unit file is installed" test -f "$UNIT"
check "the unit runs the bridge from the virtualenv" \
    grep -qx "ExecStart=${VENV}/bin/python -m printlog_bridge run --config ${CONFIG}" "$UNIT"
check "the unit runs as this user" grep -qx "User=$(id -un)" "$UNIT"
check "systemd reloaded" called "daemon-reload"
check "the service is enabled" called "enable printlog-bridge"
check "Moonraker restarted to load the update manager" called "restart moonraker"
check "the service started" called "restart printlog-bridge"
check "moonraker.conf has the update manager section" \
    grep -qx '\[update_manager printlog-bridge\]' "${DATA}/config/moonraker.conf"
check "moonraker.conf keeps what it had" \
    grep -qx 'repo: mainsail-crew/mainsail' "${DATA}/config/moonraker.conf"
check "moonraker.asvc allows the service" grep -qx printlog-bridge "${DATA}/moonraker.asvc"
check "the config was written" test -f "$CONFIG"
check "the config has the printer" grep -qx 'printer_id = 42' "$CONFIG"
check "the config waits for an API key" grep -qx 'api_key = ' "$CONFIG"
check "only this user can read the config" test "$(stat -c %a "$CONFIG")" = 600
check "the config uses the local socket" \
    grep -qx "socket = ${DATA}/comms/moonraker.sock" "$CONFIG"
check "the command is in the virtualenv" "${VENV}/bin/printlog-bridge" --version
# What systemd would run, with the unit's environment.
pythonpath="$(sed -n 's/^Environment=PYTHONPATH=//p' "$UNIT")"
check "the service's command runs" env PYTHONPATH="$pythonpath" "${VENV}/bin/python" -m printlog_bridge --version
# The config loads; doctor fails only on what CI can't reach (the API and Moonraker).
"${VENV}/bin/printlog-bridge" doctor --config "$CONFIG" >"${WORK}/doctor.txt" || true
cat "${WORK}/doctor.txt"
check "doctor reads the config" grep -q "ok    Config" "${WORK}/doctor.txt"

echo "== install.sh again"
cp "${DATA}/config/moonraker.conf" "${WORK}/moonraker.conf.installed"
: >"${WORK}/systemctl.log"
"${REPO}/install.sh" --yes </dev/null
check "a second install leaves moonraker.conf alone" \
    cmp -s "${DATA}/config/moonraker.conf" "${WORK}/moonraker.conf.installed"
check "a second install doesn't restart Moonraker" sh -c "! grep -qx 'restart moonraker' '${WORK}/systemctl.log'"
check "a second install keeps the config" grep -qx 'printer_id = 42' "$CONFIG"

echo "== uninstall.sh"
: >"${WORK}/systemctl.log"
"${REPO}/uninstall.sh" </dev/null
check "the service was stopped" called "stop printlog-bridge"
check "the service was disabled" called "disable printlog-bridge"
check "the unit file is gone" test ! -e "$UNIT"
check "moonraker.conf is as it was before the install" \
    cmp "${DATA}/config/moonraker.conf" "${WORK}/moonraker.conf.before"
check "moonraker.asvc is as it was before the install" \
    cmp "${DATA}/moonraker.asvc" "${WORK}/moonraker.asvc.before"
check "the virtualenv is gone" test ! -e "$VENV"
check "the config is kept" test -f "$CONFIG"

echo "== uninstall.sh --purge"
"${REPO}/uninstall.sh" --purge --yes </dev/null
check "the config is gone" test ! -e "$CONFIG"

if [ "$failures" -ne 0 ]; then
    echo "${failures} check(s) failed"
    exit 1
fi
echo "all checks passed"
