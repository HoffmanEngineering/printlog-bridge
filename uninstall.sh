#!/usr/bin/env bash
# Undoes install.sh: stops and removes the printlog-bridge service, puts moonraker.conf,
# moonraker.asvc and printer.cfg back as they were before the install, removes the macros it
# installed (printlog.cfg, unless it was changed since), and removes the virtualenv.
#
# The config (printlog-bridge.cfg) and the state directory (what the bridge has logged, and any
# prints still waiting to reach 3D Print Log) are kept, so a reinstall carries on where this one
# stopped. --purge removes them too.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE=printlog-bridge

PRINTER_DATA="${HOME}/printer_data"
VENV="${HOME}/printlog-bridge-env"
MOONRAKER_SERVICE=moonraker
STATE_DIR="${HOME}/.local/share/printlog-bridge"
PURGE=0
ASSUME_YES=0

# For tests only (tests/install/smoke.sh); see install.sh.
SYSTEMD_DIR="${PRINTLOG_SYSTEMD_DIR:-/etc/systemd/system}"
SYSTEMCTL="${PRINTLOG_SYSTEMCTL:-systemctl}"
PYTHON="${PRINTLOG_PYTHON:-python3}"
if [ "$(id -u)" -eq 0 ]; then SUDO_DEFAULT=""; else SUDO_DEFAULT="sudo"; fi
SUDO="${PRINTLOG_SUDO-$SUDO_DEFAULT}"

usage() {
    cat <<EOF
Usage: $0 [options]

Removes the printlog-bridge service and puts Moonraker's files back as they were.

Options:
  -d, --printer-data DIR    Moonraker's data folder (default: ~/printer_data)
  -e, --venv DIR            the virtualenv install.sh made (default: ~/printlog-bridge-env)
  -m, --moonraker-service S Moonraker's systemd service (default: moonraker)
      --purge               also remove printlog-bridge.cfg and the state directory
                            (${STATE_DIR}), including the API key and prints not
                            yet logged
  -y, --yes                 don't ask before purging
  -h, --help                show this
EOF
}

say() { printf '\033[1m%s\033[0m\n' "$*"; }
fail() {
    printf 'uninstall.sh: %s\n' "$*" >&2
    exit 1
}
run_root() { ${SUDO:+"$SUDO"} "$@"; }

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            -d | --printer-data) PRINTER_DATA="${2:?$1 needs a folder}"; shift 2 ;;
            -e | --venv) VENV="${2:?$1 needs a folder}"; shift 2 ;;
            -m | --moonraker-service) MOONRAKER_SERVICE="${2:?$1 needs a name}"; shift 2 ;;
            --purge) PURGE=1; shift ;;
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
}

remove_service() {
    local unit="${SYSTEMD_DIR}/${SERVICE}.service"
    if [ -f "$unit" ]; then
        say "Removing the ${SERVICE} service"
        # Stopping sends SIGTERM: the bridge records the stop, and its outbox keeps what it hasn't sent.
        run_root "$SYSTEMCTL" stop "$SERVICE" || true
        run_root "$SYSTEMCTL" disable "$SERVICE" || true
        run_root rm -f "$unit"
        run_root "$SYSTEMCTL" daemon-reload
    fi
}

remove_from_moonraker() {
    say "Removing ${SERVICE} from moonraker.conf and moonraker.asvc"
    local result
    result="$(
        "$PYTHON" "${REPO_DIR}/scripts/install_helper.py" moonraker-remove \
            --conf "$MOONRAKER_CONF" --asvc "$MOONRAKER_ASVC" --record "$RECORD"
    )"
    if [ "$result" = changed ]; then
        say "Restarting Moonraker"
        run_root "$SYSTEMCTL" restart "$MOONRAKER_SERVICE"
    fi
}

remove_macros() {
    local result
    result="$(
        "$PYTHON" "${REPO_DIR}/scripts/install_helper.py" macros-remove \
            --printer-cfg "$PRINTER_CFG" --record "$MACROS_RECORD"
    )"
    if [ "$result" = changed ]; then
        say "Removed the PRINTLOG_SET_SPOOL macros and their include from printer.cfg"
        echo "  Klipper drops them when it next restarts."
    fi
}

remove_venv() {
    [ -d "$VENV" ] || return 0
    # Only ever delete a virtualenv: a mistyped --venv (such as ~) must not take a folder with it.
    if [ ! -f "${VENV}/pyvenv.cfg" ]; then
        say "Not removing ${VENV}: it isn't a Python virtualenv (no pyvenv.cfg)."
        return 0
    fi
    say "Removing ${VENV}"
    rm -rf "$VENV"
}

purge() {
    if [ "$PURGE" != 1 ]; then
        echo
        say "printlog-bridge is uninstalled. Kept, for a reinstall:"
        echo "  ${CONFIG}"
        echo "  ${STATE_DIR}"
        echo "Remove them with: $0 --purge"
        return
    fi
    if [ "$ASSUME_YES" != 1 ]; then
        [ -t 0 ] || fail "--purge needs --yes when nothing can be asked"
        read -r -p "Remove ${CONFIG} and ${STATE_DIR}, including prints not yet logged? [y/N] " answer
        case "$answer" in
            y | Y | yes) ;;
            *) echo "Kept them."; return ;;
        esac
    fi
    rm -f "$CONFIG"
    rm -rf "$STATE_DIR"
    say "printlog-bridge is uninstalled, with its config and state."
}

main() {
    parse_args "$@"
    command -v "$PYTHON" >/dev/null || fail "$PYTHON not found"
    remove_service
    remove_from_moonraker
    remove_macros
    remove_venv
    purge
}

main "$@"
