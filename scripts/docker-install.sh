#!/bin/sh
# printlog-bridge, installed with Docker in one line, on a Linux computer on the same network as
# your printers (a Raspberry Pi, a mini PC, a home server, a NAS with a shell):
#
#   curl -fsSL https://github.com/HoffmanEngineering/printlog-bridge/releases/latest/download/docker-install.sh | sh
#
# Or download it, read it, then run it:
#
#   curl -fsSLO https://github.com/HoffmanEngineering/printlog-bridge/releases/latest/download/docker-install.sh
#   less docker-install.sh
#   sh docker-install.sh
#
# It writes a Docker Compose project to ~/printlog-bridge (the bridge's config and state go in its
# data/ folder, the only one the container sees), runs the bridge on the host's network (finding
# printers needs it), restarts it with Docker when the computer boots, and shows how to pair it
# with your 3D Print Log account. It never asks for or stores an API key: pairing replaces it.
# Run it as your own user, not root: it uses sudo for Docker by itself when it needs to.
#
# Running it again updates the bridge to the version this script names, and keeps your config,
# your pairing and anything in compose.override.yaml. Options (after `sh -s --` when piped):
#
#   --dir DIR     the folder (default ~/printlog-bridge; or PRINTLOG_BRIDGE_DIR)
#   --tag TAG     the image tag (default: the release this script came with; or PRINTLOG_BRIDGE_TAG)
#   --wait        wait for you to approve the pairing (the default in a terminal)
#   --no-wait     don't
#   --uninstall   stop and remove the container, keeping the folder
#   --root        run as root anyway (see below)
#
# To run the bridge on the printer's own Pi instead, use install.sh from the repository.
set -eu

# docker.yml's release job sets this to the release's version in the copy it attaches to the
# release, so that copy always installs the image it was released with.
DEFAULT_TAG=latest

IMAGE=ghcr.io/hoffmanengineering/printlog-bridge
CONTAINER=printlog-bridge
REPO_URL=https://github.com/HoffmanEngineering/printlog-bridge
INSTALL_URL="${REPO_URL}/releases/latest/download/docker-install.sh"
MARKER="# Written by printlog-bridge's docker-install.sh (${REPO_URL})."
STATUS_PAGE=http://127.0.0.1:7180/

say() { printf '%s\n' "$@"; }
fail() {
    printf '\n' >&2
    printf '%s\n' "$@" >&2
    exit 1
}

usage() {
    say "Usage: docker-install.sh [--dir DIR] [--tag TAG] [--wait | --no-wait] [--uninstall]" \
        "" \
        "Installs printlog-bridge with Docker Compose in DIR (default ~/printlog-bridge), on the" \
        "host's network, and shows how to pair it. Running it again updates it and keeps the" \
        "config and pairing. --uninstall stops and removes the container and keeps DIR." \
        "PRINTLOG_BRIDGE_DIR and PRINTLOG_BRIDGE_TAG set --dir and --tag. It refuses to run" \
        "as root unless given --root."
}

# Text from the container, without control characters (keeps tabs and newlines), so nothing it
# says can steer the terminal.
printable() { tr -d '\000-\010\013-\037\177'; }

# Docker, with sudo when this user can't use it directly.
SUDO=
dk() {
    if [ -n "$SUDO" ]; then sudo docker "$@"; else docker "$@"; fi
}
# How the commands we print should run Docker.
DK_HINT=docker

# The host's network is what makes discovery work, and only Linux's Docker gives a container
# the real one.
explain_no_lan() { # <where>
    fail "printlog-bridge: this looks like $1." \
        "" \
        "Docker there runs containers in a virtual machine, so even with host networking the" \
        "bridge can't see your network: it wouldn't find your printers (mDNS, Bambu Lab's" \
        "announcements and the other discovery need the real host network). Install it on a" \
        "Linux computer on the same network as your printers instead (a Raspberry Pi, a mini PC," \
        "a NAS), or see ${REPO_URL}#run-it-in-docker-remote-mode to run it by hand and connect" \
        "printers by their address."
}

check_os() {
    case "$(uname -s)" in
    Linux) ;;
    Darwin) explain_no_lan "macOS (Docker Desktop)" ;;
    MINGW* | MSYS* | CYGWIN*) explain_no_lan "Windows (Docker Desktop)" ;;
    *) fail "printlog-bridge: this script supports Linux only (this is $(uname -s))." ;;
    esac
    if grep -qi microsoft /proc/version 2>/dev/null; then
        explain_no_lan "Windows (WSL)"
    fi
}

# Finds Docker and a way to use it; sets SUDO.
find_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        fail "printlog-bridge: Docker isn't installed. To install it (Docker's own script):" \
            "" \
            "  curl -fsSL https://get.docker.com | sh" \
            "  sudo usermod -aG docker \"\$USER\"   # then log out and back in" \
            "" \
            "Then run this again."
    fi
    case "$(docker --version 2>/dev/null || true)" in
    *[Pp]odman*)
        fail "printlog-bridge: \`docker\` here is Podman, which this script doesn't set up." \
            "" \
            "Podman can run the image with host networking, but restarting on boot and the" \
            "health check work differently there. See ${REPO_URL}#anywhere-else-any-linux-host" \
            "for the \`run\` command (use --network host), and enable podman-restart.service."
        ;;
    esac
    if docker info >/dev/null 2>&1; then
        return
    fi
    reason="$(docker info 2>&1 >/dev/null || true)"
    case "$reason" in
    *"permission denied"*)
        if [ "$(id -u)" != 0 ] && command -v sudo >/dev/null 2>&1; then
            say "Your user can't use Docker yet, so this runs Docker with sudo (it may ask for" \
                "your password). To use Docker without sudo: sudo usermod -aG docker \"\$USER\"," \
                "then log out and back in." ""
            SUDO=1
            DK_HINT="sudo docker"
            if ! dk info >/dev/null 2>&1; then
                fail "printlog-bridge: \`sudo docker info\` failed: is Docker running?" \
                    "  sudo systemctl start docker"
            fi
            return
        fi
        fail "printlog-bridge: your user can't use Docker. Add it to the docker group:" \
            "" \
            "  sudo usermod -aG docker \"\$USER\"" \
            "" \
            "then log out and back in, and run this again."
        ;;
    *)
        fail "printlog-bridge: Docker isn't running (docker info: ${reason%%
*})." \
            "Start it, and have it start on boot:" \
            "" \
            "  sudo systemctl enable --now docker"
        ;;
    esac
}

# Refuses the Docker setups where host networking doesn't reach the LAN.
check_docker() {
    info="$(dk info --format '{{.OperatingSystem}}|{{.Architecture}}|{{json .SecurityOptions}}' 2>/dev/null)" ||
        fail "printlog-bridge: \`docker info\` failed."
    os_name="${info%%|*}"
    rest="${info#*|}"
    arch="${rest%%|*}"
    security="${rest#*|}"
    case "$os_name" in
    *"Docker Desktop"*) explain_no_lan "Docker Desktop" ;;
    esac
    case "$security" in
    *rootless*)
        fail "printlog-bridge: this Docker is rootless." \
            "" \
            "Rootless Docker's host network is a private one of its own, not this computer's, so" \
            "the bridge couldn't find your printers. Use Docker as root (the default install," \
            "https://get.docker.com), or see ${REPO_URL}#run-it-in-docker-remote-mode to run it" \
            "by hand and connect printers by their address."
        ;;
    esac
    case "$arch" in
    x86_64 | amd64 | aarch64 | arm64 | armv7l | armv7 | armv8l | armhf) ;;
    *)
        fail "printlog-bridge: there's no image for this computer (${arch})." \
            "The image is for linux/amd64, linux/arm64 and linux/arm/v7 (a Raspberry Pi 2 or newer)."
        ;;
    esac
    if ! dk compose version >/dev/null 2>&1; then
        fail "printlog-bridge: Docker Compose (\`docker compose\`) isn't installed. On Debian," \
            "Ubuntu or Raspberry Pi OS with Docker's packages:" \
            "" \
            "  sudo apt-get install docker-compose-plugin" \
            "" \
            "or see https://docs.docker.com/compose/install/linux/. Then run this again."
    fi
}

# This computer's time zone, so the bridge's log shows local times.
time_zone() {
    tz=""
    if [ -n "${TZ:-}" ]; then
        tz="$TZ"
    elif [ -r /etc/timezone ]; then
        tz="$(tr -d ' \n' </etc/timezone)"
    elif [ -L /etc/localtime ]; then
        tz="$(readlink /etc/localtime 2>/dev/null | sed -n 's#.*zoneinfo/##p')"
    fi
    case "$tz" in
    "" | *[!A-Za-z0-9_+/-]* | /* | */) echo UTC ;;
    *) echo "$tz" ;;
    esac
}

write_compose() {
    say "$MARKER" \
        "# Running the install command again rewrites this file (to update the bridge). Put your" \
        "# own settings in compose.override.yaml next to it: Docker Compose reads both, and the" \
        "# script never touches that one. See ${REPO_URL}#run-it-in-docker-remote-mode." \
        "services:" \
        "  printlog-bridge:" \
        "    image: ${IMAGE}:${TAG}" \
        "    container_name: ${CONTAINER}" \
        "    # Starts again after a crash and when the computer boots (with Docker)." \
        "    restart: unless-stopped" \
        "    # The host's network: finding printers (mDNS, Bambu Lab's announcements) doesn't work" \
        "    # through Docker's own network. The bridge's only TCP port is its read-only status" \
        "    # page, on 127.0.0.1:7180 (this computer only)." \
        "    network_mode: host" \
        "    environment:" \
        "      TZ: $(time_zone)" \
        "    volumes:" \
        "      # data/: printlog-bridge.cfg and state/ (the pairing, prints not sent yet). Only that" \
        "      # folder, so nothing in the container can change these Compose files." \
        "      - ./data:/data"
}

# The uid that owns a path (by number; nothing in its name is parsed).
owner_of() {
    # shellcheck disable=SC2012 # one path, its uid column only
    ls -nd "$1" | awk '{ print $3 }'
}

# Whether the folder holds only entries with the names given.
holds_only() { # <folder> <name>...
    folder="$1"
    shift
    for entry in "$folder"/* "$folder"/.[!.]* "$folder"/..?*; do
        [ -e "$entry" ] || [ -L "$entry" ] || continue
        name="${entry##*/}"
        found=no
        for allowed in "$@"; do
            if [ "$name" = "$allowed" ]; then found=yes; fi
        done
        [ "$found" = yes ] || return 1
    done
    return 0
}

# Whether DIR holds this script's own compose.yaml.
dir_is_marked() {
    [ -f "${DIR}/compose.yaml" ] && [ ! -L "${DIR}/compose.yaml" ] && grep -qxF "$MARKER" "${DIR}/compose.yaml"
}

# Whether DIR is a folder this script may use. The container takes over data/ (it hands every
# file in it to the bridge's user, as root), so data/ must be a real folder, never a link: in
# DIR this script made, or else this user's own and empty, or holding only what the README's
# move from a setup by hand puts there (printlog-bridge.cfg and state/).
dir_is_ours() {
    if [ -L "${DIR}/data" ] || [ -L "${DIR}/compose.override.yaml" ]; then
        fail "printlog-bridge: ${DIR}/data or compose.override.yaml is a symbolic link." \
            "The bridge takes over data/, so it must be a folder of its own, and the override a file."
    fi
    if dir_is_marked; then
        return 0
    fi
    holds_only "$DIR" data compose.override.yaml || return 1
    if [ -e "${DIR}/data" ]; then
        [ -d "${DIR}/data" ] || return 1
        [ "$(owner_of "${DIR}/data")" = "$(id -u)" ] || return 1
        holds_only "${DIR}/data" printlog-bridge.cfg state || return 1
    fi
    return 0
}

check_dir() {
    case "$DIR" in
    /*/*) ;;
    *) fail "printlog-bridge: ${DIR} is a system folder: choose a folder of its own, such as ~/printlog-bridge." ;;
    esac
    home="$(cd "${HOME:-/}" 2>/dev/null && pwd -P || echo "${HOME:-/}")"
    if [ "$DIR" = "$home" ]; then
        fail "printlog-bridge: ${DIR} is your home folder: choose a folder of its own, such as ~/printlog-bridge."
    fi
    if [ -e "$DIR" ] || [ -L "$DIR" ]; then
        [ -d "$DIR" ] || fail "printlog-bridge: ${DIR} isn't a folder."
        # Root, with --root, may use any folder.
        if [ "$(id -u)" != 0 ] && [ "$(owner_of "$DIR")" != "$(id -u)" ]; then
            fail "printlog-bridge: ${DIR} isn't yours, so this script won't hand it to the bridge." \
                "Choose a folder of your own with --dir."
        fi
    fi
}

prepare_dir() {
    for other in docker-compose.yml docker-compose.yaml compose.yml; do
        if [ -e "${DIR}/${other}" ]; then
            fail "printlog-bridge: ${DIR} already has a ${other}." \
                "" \
                "Keep it, and update with \`${DK_HINT} compose pull && ${DK_HINT} compose up -d\` there." \
                "Or, to switch to this script, which keeps the bridge's files in a data/ folder:" \
                "\`${DK_HINT} compose down\` there, make a new folder with a data/ folder in it, move" \
                "printlog-bridge.cfg and state/ into that data/, and run this with --dir and the new folder."
        fi
    done
    if [ -e "${DIR}/compose.yaml" ] && ! grep -qxF "$MARKER" "${DIR}/compose.yaml"; then
        fail "printlog-bridge: ${DIR}/compose.yaml isn't one this script wrote, so it's left alone." \
            "Choose another folder with --dir."
    fi
    if ! dir_is_ours; then
        fail "printlog-bridge: ${DIR} isn't empty, and this script didn't make it." \
            "The bridge takes over the folder it's given, so choose a new one with --dir. (A data/" \
            "folder of yours holding only printlog-bridge.cfg and state/, moved from a setup by" \
            "hand, is fine.)"
    fi
    if wd="$(dk inspect --type container --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$CONTAINER" 2>/dev/null)" &&
        [ "$wd" != "$DIR" ]; then
        fail "printlog-bridge: there's already a container called ${CONTAINER}${wd:+ (from ${wd})}." \
            "" \
            "Remove it (its config and state stay in the folder it used), then run this again:" \
            "" \
            "  ${DK_HINT} rm -f ${CONTAINER}" \
            "" \
            "To keep using its folder, add --dir with that folder (after \`sh -s --\` when piped)."
    fi
}

# What the bridge's own state says about pairing (`printlog-bridge status --pairing`): "pending
# <code> <address> [<link>]" while it waits for approval, else paired, api-key or waiting; nothing
# from an image without it. Never its log, which a device on the network can put text into.
pairing_state() {
    dk exec "$CONTAINER" printlog-bridge status --pairing 2>/dev/null | printable | sed -n 1p || true
}

# "<code> <address> [<link>]" from a pending state, when it's a code and https addresses.
pending_code() { # <state>; runs in a subshell, so set -f stays here
    set -f
    # shellcheck disable=SC2086 # split into its words
    set -- $1
    [ "${1:-}" = pending ] || return 0
    case "${2:-}" in "" | *[!A-Za-z0-9-]*) return 0 ;; esac
    case "${3:-}" in https://?*) ;; *) return 0 ;; esac
    case "${4:-}" in https://?*) echo "$2 $3 $4" ;; *) echo "$2 $3" ;; esac
}

show_pairing() { # <code> <address> [<link>]
    say "" \
        "Pair the bridge with your 3D Print Log account: open" \
        "" \
        "  $2" \
        "" \
        "and enter the code  $1"
    if [ -n "${3:-}" ]; then say "(or open $3)"; fi
    say "" \
        "The QR code to scan is in the log (\`${DK_HINT} compose logs -f\` in ${DIR}) and on the" \
        "status page, ${STATUS_PAGE} on this computer. A new code replaces this one when it expires."
}

show_log_tail() {
    say "" "The end of its log:" ""
    dk logs --tail 20 "$CONTAINER" 2>&1 | printable | sed 's/^/  /' || true
}

# Waits until the bridge runs, or shows its pairing code (and, with --wait, until it's approved).
wait_for_bridge() {
    poll="${PRINTLOG_BRIDGE_POLL_SECONDS:-2}"
    start_deadline=$(($(date +%s) + ${PRINTLOG_BRIDGE_START_TIMEOUT:-120}))
    approve_deadline=$(($(date +%s) + ${PRINTLOG_BRIDGE_APPROVE_TIMEOUT:-900}))
    shown=""
    say "" "Waiting for the bridge to start..."
    while :; do
        state="$(dk inspect --type container --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo missing)"
        case "$state" in
        exited | dead | restarting | missing)
            show_log_tail
            fail "printlog-bridge: the bridge stopped (${state}). Its log above says why."
            ;;
        esac
        # A code waiting for approval comes first: the bridge is healthy while it pairs, and a
        # paired bridge can need pairing again.
        pairing="$(pairing_state)"
        code="$(pending_code "$pairing")"
        if [ -z "$code" ] && [ "$pairing" != waiting ] &&
            dk exec "$CONTAINER" printlog-bridge-health >/dev/null 2>&1; then
            if [ -n "$shown" ]; then say "" "Approved."; fi
            say "" "printlog-bridge is running."
            if [ -z "$pairing" ]; then
                # An image from before `status --pairing` (an older --tag).
                say "If it still needs pairing, its log shows the code: ${DK_HINT} compose logs -f"
            fi
            return 0
        fi
        if [ -n "$code" ] && [ "$code" != "$shown" ]; then
            # Three words: code, address, link. With globbing off, so a * stays a *.
            set -f
            # shellcheck disable=SC2086
            show_pairing $code
            set +f
            shown="$code"
            if [ "$WAIT" = no ]; then return 0; fi
            say "" "Waiting for you to approve it (Ctrl-C stops waiting; the bridge keeps running)..."
        fi
        now="$(date +%s)"
        if [ -n "$shown" ] && [ -n "$code" ] && [ "$now" -ge "$approve_deadline" ]; then
            say "" "Not approved yet. The bridge keeps waiting, with a new code in its log when this one expires."
            return 0
        fi
        if [ -z "$code" ] && [ "$now" -ge "$start_deadline" ]; then
            show_log_tail
            say "" "The bridge is still starting. It keeps going: check its log in a minute."
            return 0
        fi
        sleep "$poll"
    done
}

show_summary() {
    say "" \
        "Folder:      ${DIR}" \
        "             data/printlog-bridge.cfg (printers you add by hand) and data/state/ (the pairing," \
        "             prints not sent yet)" \
        "Printers:    3D Print Log's Bridges page lists the printers the bridge finds on your network:" \
        "             connect them there." \
        "Log:         cd ${DIR} && ${DK_HINT} compose logs -f" \
        "Status page: ${STATUS_PAGE} in a browser on this computer. From another computer:" \
        "             ssh -L 7180:127.0.0.1:7180 $(id -un)@$(uname -n), then ${STATUS_PAGE} there." \
        "             To open it to your whole network, put this in ${DIR}/compose.override.yaml" \
        "             and run this script again:" \
        "" \
        "               services:" \
        "                 printlog-bridge:" \
        "                   environment:" \
        "                     PRINTLOG_BRIDGE_STATUS_PAGE: 0.0.0.0:7180" \
        "" \
        "             Anyone on your network can then see it, including the pairing code while the" \
        "             bridge pairs, and whoever approves it first gets the bridge: don't on a shared" \
        "             network (a dorm, a makerspace, an office)."
    if [ -e "${DIR}/compose.override.yaml" ]; then
        say "             (${DIR}/compose.override.yaml is there and in use.)"
    fi
    say "Update:      run the install command again: it keeps your config and pairing." \
        "Uninstall:   curl -fsSL ${INSTALL_URL} | sh -s -- --uninstall${DIR_ARG}"
    if command -v systemctl >/dev/null 2>&1 && [ "$(systemctl is-enabled docker 2>/dev/null || true)" = disabled ]; then
        say "" "Warning: Docker doesn't start when this computer boots, so neither does the bridge. To fix it:" \
            "" "  sudo systemctl enable docker"
    fi
}

install() {
    check_os
    if [ "$(id -u)" = 0 ] && [ "$ROOT" != yes ]; then
        fail "printlog-bridge: this is running as root." \
            "" \
            "Run it as your own user instead: it uses sudo for Docker by itself when it needs to, and" \
            "the folder is then yours. As root, the folder is root's, and everything this script" \
            "and Docker Compose do runs as root. Where root is the only user (some NAS shells), add" \
            "--root (after \`sh -s --\` when piped)."
    fi
    find_docker
    check_docker
    check_dir
    mkdir -p "$DIR" || fail "printlog-bridge: can't make ${DIR}."
    DIR="$(cd "$DIR" && pwd -P)"
    check_dir
    prepare_dir
    mkdir -p "${DIR}/data" || fail "printlog-bridge: can't make ${DIR}/data."
    tmp="${DIR}/.compose.yaml.$$"
    write_compose >"$tmp"
    mv -f "$tmp" "${DIR}/compose.yaml"
    say "Wrote ${DIR}/compose.yaml (${IMAGE}:${TAG}). Pulling the image and starting the bridge..."
    (cd "$DIR" && dk compose pull --quiet && dk compose up -d) ||
        fail "printlog-bridge: Docker Compose failed (above)."
    wait_for_bridge
    show_summary
}

uninstall() {
    find_docker
    if [ ! -f "${DIR}/compose.yaml" ] || ! grep -qxF "$MARKER" "${DIR}/compose.yaml"; then
        fail "printlog-bridge: nothing to uninstall in ${DIR} (no compose.yaml from this script there; use --dir)."
    fi
    (cd "$DIR" && dk compose down) || fail "printlog-bridge: Docker Compose failed (above)."
    say "" \
        "Stopped and removed the printlog-bridge container. Its folder is still there, with your" \
        "config and pairing in data/, for a reinstall. To remove it too:" \
        "" \
        "  rm -rf ${DIR}" \
        "" \
        "and remove the bridge on 3D Print Log's Bridges page. \`${DK_HINT} image prune -a\`" \
        "removes the image."
}

main() {
    DIR="${PRINTLOG_BRIDGE_DIR:-}"
    TAG="${PRINTLOG_BRIDGE_TAG:-$DEFAULT_TAG}"
    WAIT=""
    ACTION=install
    ROOT=no
    DIR_ARG=""
    while [ $# -gt 0 ]; do
        case "$1" in
        --dir)
            [ $# -ge 2 ] || fail "printlog-bridge: --dir needs a folder."
            DIR="$2"
            shift 2
            ;;
        --dir=*)
            DIR="${1#--dir=}"
            shift
            ;;
        --tag)
            [ $# -ge 2 ] || fail "printlog-bridge: --tag needs a tag."
            TAG="$2"
            shift 2
            ;;
        --tag=*)
            TAG="${1#--tag=}"
            shift
            ;;
        --wait) WAIT=yes && shift ;;
        --no-wait) WAIT=no && shift ;;
        --uninstall) ACTION=uninstall && shift ;;
        --root) ROOT=yes && shift ;;
        -h | --help)
            usage
            exit 0
            ;;
        *) fail "printlog-bridge: unknown option $1 (see --help)." ;;
        esac
    done
    case "$TAG" in
    "" | [!A-Za-z0-9_]* | *[!A-Za-z0-9_.-]*) fail "printlog-bridge: \"${TAG}\" isn't an image tag." ;;
    esac
    # Without slashes at its end, so `/newdir/` is checked as the top-level folder it is.
    while :; do
        case "$DIR" in
        ?*/) DIR="${DIR%/}" ;;
        *) break ;;
        esac
    done
    if [ -z "$DIR" ]; then
        [ -n "${HOME:-}" ] || fail "printlog-bridge: HOME isn't set; choose a folder with --dir."
        DIR="${HOME}/printlog-bridge"
    else
        case "$DIR" in
        /*) ;;
        *) DIR="$(pwd)/${DIR}" ;;
        esac
        DIR_ARG=" --dir ${DIR}"
    fi
    if [ -z "$WAIT" ]; then
        if [ -t 1 ]; then WAIT=yes; else WAIT=no; fi
    fi
    "$ACTION"
}

# Everything runs from here, so a download cut short runs nothing.
main "$@"
