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
# A printer.cfg with Klipper's SAVE_CONFIG block, which must stay last.
printf '[printer]\nkinematics: corexy\n\n#*# <---------------------- SAVE_CONFIG ---------------------->\n#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.\n#*#\n' \
    >"${DATA}/config/printer.cfg"
cp "${DATA}/config/moonraker.conf" "${WORK}/moonraker.conf.before"
cp "${DATA}/config/printer.cfg" "${WORK}/printer.cfg.before"
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
    grep -qxF "ExecStart=\"${VENV}/bin/python\" -m printlog_bridge run --config \"${CONFIG}\"" "$UNIT"
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
check "the macros are next to printer.cfg" cmp -s "${DATA}/config/printlog.cfg" "${REPO}/klipper/printlog.cfg"
check "printer.cfg includes the macros" grep -qx '\[include printlog.cfg\]' "${DATA}/config/printer.cfg"
check "SAVE_CONFIG's block is still last in printer.cfg" \
    sh -c "tail -n 1 '${DATA}/config/printer.cfg' | grep -qx '#\*#'"
check "Klipper isn't restarted" sh -c "! grep -q klipper '${WORK}/systemctl.log'"
check "the config was written" test -f "$CONFIG"
check "the config has the printer" grep -qx 'printer_id = 42' "$CONFIG"
check "the config holds no API key" sh -c "! grep -q '^api_key' '$CONFIG'"
check "no key file without a key" test ! -e "${HOME}/.local/share/printlog-bridge/api_key"
check "only this user can read the config" test "$(stat -c %a "$CONFIG")" = 600
check "the config uses the local socket" \
    grep -qx "socket = ${DATA}/comms/moonraker.sock" "$CONFIG"
check "the command is in the virtualenv" "${VENV}/bin/printlog-bridge" --version
# What systemd would run, with the unit's environment.
pythonpath="$(sed -n 's/^Environment="PYTHONPATH=\(.*\)"$/\1/p' "$UNIT")"
check "the unit sets PYTHONPATH to the checkout" test "$pythonpath" = "${REPO}/src"
check "the service's command runs" env PYTHONPATH="$pythonpath" "${VENV}/bin/python" -m printlog_bridge --version
# The config loads; doctor fails only on what CI can't reach (the API and Moonraker).
"${VENV}/bin/printlog-bridge" doctor --config "$CONFIG" >"${WORK}/doctor.txt" || true
cat "${WORK}/doctor.txt"
check "doctor reads the config" grep -q "ok    Config" "${WORK}/doctor.txt"

echo "== install.sh again"
cp "${DATA}/config/moonraker.conf" "${WORK}/moonraker.conf.installed"
cp "${DATA}/config/printer.cfg" "${WORK}/printer.cfg.installed"
: >"${WORK}/systemctl.log"
"${REPO}/install.sh" --yes </dev/null
check "a second install leaves moonraker.conf alone" \
    cmp -s "${DATA}/config/moonraker.conf" "${WORK}/moonraker.conf.installed"
check "a second install doesn't restart Moonraker" sh -c "! grep -qx 'restart moonraker' '${WORK}/systemctl.log'"
check "a second install keeps the config" grep -qx 'printer_id = 42' "$CONFIG"
check "a second install leaves printer.cfg alone" \
    cmp -s "${DATA}/config/printer.cfg" "${WORK}/printer.cfg.installed"

echo "== install.sh for a second printer_data folder"
mkdir -p "${WORK}/other_data/config"
cp "${UNIT}" "${WORK}/unit.before"
check "a second Klipper instance is refused"     sh -c "! '${REPO}/install.sh' --yes --printer-data '${WORK}/other_data' </dev/null"
check "the refused install left the unit alone" cmp -s "$UNIT" "${WORK}/unit.before"

echo "== uninstall.sh with a --venv that isn't a virtualenv"
mkdir -p "${WORK}/not-a-venv"
touch "${WORK}/not-a-venv/keep-me"
"${REPO}/uninstall.sh" --venv "${WORK}/not-a-venv" </dev/null >/dev/null 2>&1 || true
check "uninstall never deletes a folder that isn't a virtualenv" test -f "${WORK}/not-a-venv/keep-me"
# Put the service back for the real uninstall below.
"${REPO}/install.sh" --yes </dev/null >/dev/null

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
check "printer.cfg is as it was before the install" \
    cmp "${DATA}/config/printer.cfg" "${WORK}/printer.cfg.before"
check "the macros are gone" test ! -e "${DATA}/config/printlog.cfg"
check "the virtualenv is gone" test ! -e "$VENV"
check "the config is kept" test -f "$CONFIG"

echo "== uninstall.sh --purge"
"${REPO}/uninstall.sh" --purge --yes </dev/null
check "the config is gone" test ! -e "$CONFIG"

# With a key but no printer, so install.sh doesn't run doctor (which would call the real API).
echo "== install.sh with an API key"
KEY_FILE="${HOME}/.local/share/printlog-bridge/api_key"
PRINTLOG_API_KEY=smoke-test-key "${REPO}/install.sh" --yes </dev/null >"${WORK}/install.txt"
check "the key is stored in the state directory" grep -qx smoke-test-key "$KEY_FILE"
check "only this user can read the key" test "$(stat -c %a "$KEY_FILE")" = 600
check "the key isn't in the config Moonraker serves" sh -c "! grep -q smoke-test-key '$CONFIG'"
check "install.sh never prints the key" sh -c "! grep -q smoke-test-key '${WORK}/install.txt'"
check "install.sh asks only for the printer" grep -q "Set printer_id" "${WORK}/install.txt"
"${REPO}/uninstall.sh" --purge --yes </dev/null >/dev/null
check "purge removes the key" test ! -e "$KEY_FILE"

echo "== install.sh --no-macros"
"${REPO}/install.sh" --yes --no-macros </dev/null >/dev/null
check "--no-macros leaves printer.cfg alone" \
    cmp -s "${DATA}/config/printer.cfg" "${WORK}/printer.cfg.before"
check "--no-macros installs no macros" test ! -e "${DATA}/config/printlog.cfg"
"${REPO}/uninstall.sh" --purge --yes </dev/null >/dev/null

if [ "$failures" -ne 0 ]; then
    echo "${failures} check(s) failed"
    exit 1
fi
echo "all checks passed"
