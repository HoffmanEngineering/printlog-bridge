#!/usr/bin/env bash
# Installs printlog-bridge in local mode, on the printer's own host (MainsailOS, FluiddPi, or any
# Klipper install with Moonraker):
#
#   cd ~ && git clone https://github.com/HoffmanEngineering/printlog-bridge.git
#   ./printlog-bridge/install.sh
#
# It makes a virtualenv, writes printlog-bridge.cfg next to printer.cfg, installs and starts a
# printlog-bridge systemd service, and lets Moonraker manage it: `printlog-bridge` in
# moonraker.asvc, so Mainsail and Fluidd can restart it, and an [update_manager printlog-bridge]
# section in moonraker.conf, so they offer its updates. It also puts the PRINTLOG_SET_SPOOL
# macros (printlog.cfg) next to printer.cfg and includes them from it, unless --no-macros.
# Running it again is safe. uninstall.sh undoes all of it.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE=printlog-bridge
DEFAULT_ORIGIN=https://github.com/HoffmanEngineering/printlog-bridge.git
API_KEYS_PAGE=https://www.3dprintlog.com/api-keys

PRINTER_DATA="${HOME}/printer_data"
VENV="${HOME}/printlog-bridge-env"
MOONRAKER_SERVICE=moonraker
PRINTER_ID=""
ASSUME_YES=0
MACROS=1

# For tests only (tests/install/smoke.sh): where unit files go, and the commands that change the
# system, so CI can run this script against a fake systemctl.
SYSTEMD_DIR="${PRINTLOG_SYSTEMD_DIR:-/etc/systemd/system}"
SYSTEMCTL="${PRINTLOG_SYSTEMCTL:-systemctl}"
PYTHON="${PRINTLOG_PYTHON:-python3}"
if [ "$(id -u)" -eq 0 ]; then SUDO_DEFAULT=""; else SUDO_DEFAULT="sudo"; fi
SUDO="${PRINTLOG_SUDO-$SUDO_DEFAULT}"

usage() {
    cat <<EOF
Usage: $0 [options]

Installs printlog-bridge as a systemd service next to Moonraker, and adds it to Moonraker's
update manager.

Options:
  -d, --printer-data DIR    Moonraker's data folder (default: ~/printer_data)
  -e, --venv DIR            where to make the virtualenv (default: ~/printlog-bridge-env)
  -p, --printer-id N        the printer's number in 3D Print Log (the end of its page's address)
  -m, --moonraker-service S Moonraker's systemd service (default: moonraker)
      --no-macros           don't install the PRINTLOG_SET_SPOOL macros (printlog.cfg) or
                            include them from printer.cfg
  -y, --yes                 don't ask anything; fill in the config afterwards
  -h, --help                show this

The API key is asked for, or taken from PRINTLOG_API_KEY. It is never taken from the command line,
and is stored in ~/.local/share/printlog-bridge/api_key (mode 600), not in printlog-bridge.cfg,
which Moonraker serves to anyone who can open Mainsail or Fluidd.
EOF
}

say() { printf '\033[1m%s\033[0m\n' "$*"; }
fail() {
    printf 'install.sh: %s\n' "$*" >&2
    exit 1
}
run_root() { ${SUDO:+"$SUDO"} "$@"; }

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            -d | --printer-data) PRINTER_DATA="${2:?$1 needs a folder}"; shift 2 ;;
            -e | --venv) VENV="${2:?$1 needs a folder}"; shift 2 ;;
            -p | --printer-id) PRINTER_ID="${2:?$1 needs a number}"; shift 2 ;;
            -m | --moonraker-service) MOONRAKER_SERVICE="${2:?$1 needs a name}"; shift 2 ;;
            --no-macros) MACROS=0; shift ;;
            -y | --yes) ASSUME_YES=1; shift ;;
            -h | --help) usage; exit 0 ;;
            *) usage >&2; fail "unknown option: $1" ;;
        esac
    done
    PRINTER_DATA="${PRINTER_DATA%/}"
    VENV="${VENV%/}"
    CONFIG="${PRINTER_DATA}/config/printlog-bridge.cfg"
    MOONRAKER_CONF="${PRINTER_DATA}/config/moonraker.conf"
    MOONRAKER_ASVC="${PRINTER_DATA}/moonraker.asvc"
    RECORD="${REPO_DIR}/.install-record.json"
    PRINTER_CFG="${PRINTER_DATA}/config/printer.cfg"
    MACROS_RECORD="${REPO_DIR}/.install-macros.json"
    # The API key goes here, not in printlog-bridge.cfg: Moonraker serves the config folder to
    # anyone who can open Mainsail or Fluidd. The bridge's default state directory.
    KEY_FILE="${HOME}/.local/share/printlog-bridge/api_key"
}

check_system() {
    if [ "$(id -u)" -eq 0 ] && [ "${PRINTLOG_ALLOW_ROOT:-0}" != 1 ]; then
        fail "run this as the user Klipper and Moonraker run as (such as pi), not as root."
    fi
    command -v "$PYTHON" >/dev/null || fail "$PYTHON not found. Install it: sudo apt install python3"
    # One service serves one Klipper instance for now. Installing for a second printer_data folder
    # would silently repoint the service and overwrite the install record the uninstaller relies on.
    existing="${SYSTEMD_DIR}/${SERVICE}.service"
    if [ -f "$existing" ] && ! grep -qF -- "--config \"${CONFIG}\"" "$existing"         && ! grep -qF -- "--config ${CONFIG}" "$existing"; then
        fail "printlog-bridge is already installed for another printer_data folder ($(grep -o -- '--config .*' "$existing")). Only one Klipper instance is supported for now; run uninstall.sh for that one first."
    fi
    "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' ||
        fail "printlog-bridge needs Python 3.9 or newer; this is $("$PYTHON" -V 2>&1)."
    [ -f "$MOONRAKER_CONF" ] ||
        fail "$MOONRAKER_CONF not found. Is Moonraker installed? If its data folder is elsewhere, pass it with --printer-data."
}

make_venv() {
    say "Installing printlog-bridge into ${VENV}"
    if [ ! -x "${VENV}/bin/python" ]; then
        "$PYTHON" -m venv "$VENV" ||
            fail "couldn't make a virtualenv. Install venv support (sudo apt install python3-venv) and run this again."
    fi
    # An editable install: Moonraker updates the code with git, and the venv runs it as it is.
    # Bullseye's pip (20.3) predates editable installs from pyproject.toml, hence the upgrade.
    "${VENV}/bin/python" -m pip install --quiet --upgrade pip
    "${VENV}/bin/python" -m pip install --quiet -r "${REPO_DIR}/requirements.txt"
    "${VENV}/bin/python" -m pip install --quiet -e "$REPO_DIR"
}

# Pairing (#2) will replace this: it will start `printlog-bridge pair`, which shows a code to
# enter at 3dprintlog.com and fills in the config itself. Until then the bridge signs in with a
# personal API key and needs the printer's number in 3D Print Log.
ask_for_account() {
    if [ -f "$CONFIG" ] || [ "$ASSUME_YES" = 1 ] || [ ! -t 0 ]; then
        return
    fi
    echo
    say "Link this printer to 3D Print Log"
    if [ -z "${PRINTLOG_API_KEY:-}" ]; then
        echo "Create an API key at ${API_KEYS_PAGE} and paste it here (or press Enter to do it later)."
        read -r -s -p "API key: " PRINTLOG_API_KEY
        echo
        export PRINTLOG_API_KEY
    fi
    if [ -z "$PRINTER_ID" ]; then
        echo "Open the printer's page in 3D Print Log; its number is the end of the address,"
        echo "such as 42 in https://www.3dprintlog.com/printers/42 (or press Enter to do it later)."
        read -r -p "Printer number: " PRINTER_ID
    fi
}

write_config() {
    local name result
    name="$(hostname 2>/dev/null || echo printer)"
    result="$(
        "$PYTHON" "${REPO_DIR}/scripts/install_helper.py" write-config \
            --path "$CONFIG" --printer-data "$PRINTER_DATA" --name "$name" \
            --key-file "$KEY_FILE" ${PRINTER_ID:+--printer-id "$PRINTER_ID"}
    )"
    if [ "$result" = created ]; then
        say "Wrote ${CONFIG}"
        if [ -s "$KEY_FILE" ]; then
            say "Stored the API key in ${KEY_FILE} (only you can read it)"
        fi
    else
        say "Keeping ${CONFIG}"
    fi
}

install_service() {
    say "Installing the ${SERVICE} service"
    local unit
    unit="$(mktemp)"
    cat >"$unit" <<EOF
[Unit]
Description=printlog-bridge: logs every print to 3D Print Log
Documentation=https://github.com/HoffmanEngineering/printlog-bridge
After=network-online.target ${MOONRAKER_SERVICE}.service
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=${REPO_DIR}
# The code from the git checkout Moonraker updates, so a rebuilt virtualenv still runs it.
Environment="PYTHONPATH=${REPO_DIR}/src"
ExecStart="${VENV}/bin/python" -m printlog_bridge run --config "${CONFIG}"
Restart=always
RestartSec=10
# Exit status 2 is a config to fix (no API key, no printer_id): restarting won't help, and
# restarting it from Mainsail or Fluidd once it's fixed will.
RestartPreventExitStatus=2
# SIGTERM stops the bridge cleanly, and it records the stop for \`printlog-bridge status\`.
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF
    run_root install -m 644 "$unit" "${SYSTEMD_DIR}/${SERVICE}.service"
    rm -f "$unit"
    run_root "$SYSTEMCTL" daemon-reload
    run_root "$SYSTEMCTL" enable "$SERVICE"
}

add_to_moonraker() {
    say "Adding ${SERVICE} to Moonraker's update manager"
    local origin result
    origin="$DEFAULT_ORIGIN"
    if git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
        # Moonraker checks the checkout's remote against `origin`; use the one it was cloned from.
        origin="$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || echo "$DEFAULT_ORIGIN")"
    else
        echo "  ${REPO_DIR} isn't a git clone, so Mainsail and Fluidd can't update it. Install from"
        echo "  a clone (git clone ${DEFAULT_ORIGIN}) to get updates there."
    fi
    result="$(
        "$PYTHON" "${REPO_DIR}/scripts/install_helper.py" moonraker-add \
            --conf "$MOONRAKER_CONF" --asvc "$MOONRAKER_ASVC" --record "$RECORD" \
            --repo "$REPO_DIR" --venv "$VENV" --origin "$origin"
    )"
    if [ "$result" = changed ]; then
        say "Restarting Moonraker to load it"
        run_root "$SYSTEMCTL" restart "$MOONRAKER_SERVICE"
    fi
}

# The macros are copied rather than linked, so printer.cfg never includes a file that goes away
# with the checkout. Klipper reads them when it next restarts; this doesn't restart it, since that
# would end a print.
install_macros() {
    if [ "$MACROS" != 1 ]; then
        return
    fi
    say "Installing the PRINTLOG_SET_SPOOL macros"
    local result
    result="$(
        "$PYTHON" "${REPO_DIR}/scripts/install_helper.py" macros-add \
            --printer-cfg "$PRINTER_CFG" --source "${REPO_DIR}/klipper/printlog.cfg" \
            --record "$MACROS_RECORD"
    )"
    if [ "$result" = changed ]; then
        echo "  Added ${PRINTER_DATA}/config/printlog.cfg, included from printer.cfg."
        echo "  Restart Klipper (FIRMWARE_RESTART, when it isn't printing) to load the macros."
    fi
}

start_service() {
    say "Starting ${SERVICE}"
    run_root "$SYSTEMCTL" restart "$SERVICE"
}

finish() {
    local bin="${VENV}/bin/printlog-bridge" has_key=0 has_printer=0
    echo
    # A key in the config (an older install) still works; doctor says how to move it.
    if [ -s "$KEY_FILE" ] || grep -Eq '^api_key[[:space:]]*=[[:space:]]*[^[:space:]]' "$CONFIG"; then
        has_key=1
    fi
    if grep -Eq '^printer_id[[:space:]]*=[[:space:]]*[0-9]' "$CONFIG"; then
        has_printer=1
    fi
    if [ "$has_key" = 1 ] && [ "$has_printer" = 1 ]; then
        say "Checking the setup"
        "$bin" doctor --config "$CONFIG" || true
        echo
        say "printlog-bridge is installed and running. Its status: ${bin} status"
    else
        say "printlog-bridge is installed. To start logging prints:"
        if [ "$has_key" = 0 ]; then
            echo "  - Store an API key from ${API_KEYS_PAGE}: ${bin} store-key"
        fi
        if [ "$has_printer" = 0 ]; then
            echo "  - Set printer_id in ${CONFIG} (in Mainsail or Fluidd, under Machine)."
        fi
        echo "  Then restart printlog-bridge there, or: sudo systemctl restart ${SERVICE}"
        echo "  Check it with: ${bin} doctor"
    fi
}

main() {
    parse_args "$@"
    check_system
    make_venv
    ask_for_account
    write_config
    install_service
    add_to_moonraker
    install_macros
    start_service
    finish
}

main "$@"
