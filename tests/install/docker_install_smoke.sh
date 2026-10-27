#!/usr/bin/env bash
# Runs scripts/docker-install.sh (the one-line Docker install) against a fake `docker` and checks
# the Compose project it writes, that running it again keeps the config and state, and what it
# tells the user. Nothing needs Docker: `docker`, `sudo`, `systemctl` and `uname` are fakes that
# record their calls, and PATH holds only them and the few tools the script may use, so a tool
# the script shouldn't need fails loudly. CI runs it (docker-install-smoke) with /bin/sh as dash.
# When a real `docker compose` works on this computer, it also validates the file written.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${REPO}/scripts/docker-install.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

REAL_DOCKER="$(command -v docker || true)"

# The tools the script may use, and nothing else.
TOOLS="${WORK}/tools"
mkdir -p "$TOOLS"
for tool in sh awk cat date grep id mkdir mv readlink sed sleep tr; do
    ln -s "$(command -v "$tool")" "${TOOLS}/${tool}"
done
REAL_UNAME="$(command -v uname)"

# The fakes. State for the fake Docker lives in $FAKE (one folder per scenario).
FAKEBIN="${WORK}/fakebin"
mkdir -p "$FAKEBIN"
cat >"${FAKEBIN}/docker" <<'EOF'
#!/bin/sh
# A fake docker: records each call (with the folder it ran in) and answers from $FAKE.
echo "$(pwd): docker $*${FAKE_SUDO:+ (sudo)}" >>"$FAKE/calls.log"
case "$1" in
--version)
    if [ -n "${FAKE_PODMAN:-}" ]; then echo "podman version 4.9.3"; else echo "Docker version 27.3.1, build ce12230"; fi
    ;;
info)
    if [ -n "${FAKE_DENIED:-}" ] && [ -z "${FAKE_SUDO:-}" ]; then
        echo "permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock" >&2
        exit 1
    fi
    if [ -n "${FAKE_DOWN:-}" ]; then
        echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?" >&2
        exit 1
    fi
    echo "WARNING: No swap limit support" >&2
    echo "${FAKE_OS:-Ubuntu 24.04.1 LTS}|${FAKE_ARCH:-x86_64}|${FAKE_SECURITY:-[\"name=apparmor\",\"name=seccomp,profile=builtin\"]}"
    ;;
compose)
    shift
    case "$1" in
    version)
        if [ -n "${FAKE_NO_COMPOSE:-}" ]; then
            echo "docker: 'compose' is not a docker command." >&2
            exit 1
        fi
        echo "Docker Compose version v2.29.7"
        ;;
    up) pwd >"$FAKE/container" ;;
    down) mv -f "$FAKE/container" "$FAKE/container.removed" ;;
    esac
    ;;
inspect)
    if [ -f "$FAKE/foreign" ]; then wd=""; elif [ -f "$FAKE/container" ]; then wd="$(cat "$FAKE/container")"; else
        echo "Error: No such container: printlog-bridge" >&2
        exit 1
    fi
    case "$*" in
    *working_dir*) echo "$wd" ;;
    *State.Status*) echo "${FAKE_STATE:-running}" ;;
    esac
    ;;
logs)
    if [ -f "$FAKE/logs" ]; then cat "$FAKE/logs"; fi
    echo "a line on stderr" >&2
    ;;
exec)
    # printlog-bridge-health: healthy once $FAKE/healthy_after calls have been made.
    n=$(($(cat "$FAKE/health_calls" 2>/dev/null || echo 0) + 1))
    echo "$n" >"$FAKE/health_calls"
    [ -f "$FAKE/healthy_after" ] && [ "$n" -ge "$(cat "$FAKE/healthy_after")" ]
    ;;
esac
EOF
cat >"${FAKEBIN}/sudo" <<'EOF'
#!/bin/sh
echo "sudo $*" >>"$FAKE/calls.log"
FAKE_SUDO=1 exec "$@"
EOF
cat >"${FAKEBIN}/systemctl" <<'EOF'
#!/bin/sh
echo "systemctl $*" >>"$FAKE/calls.log"
echo "${FAKE_BOOT:-enabled}"
EOF
cat >"${FAKEBIN}/uname" <<EOF
#!/bin/sh
case "\$1" in
-s) echo "\${FAKE_UNAME:-Linux}" ;;
*) exec "${REAL_UNAME}" "\$@" ;;
esac
EOF
chmod +x "${FAKEBIN}"/*
# The same fakes without docker, sudo or systemctl, for the scenarios that need them missing.
BARE="${WORK}/bare"
mkdir -p "$BARE"
cp "${FAKEBIN}/uname" "${BARE}/uname"

PAIRING_LOG='2026-10-27 10:00:01,000 INFO printlog-bridge 0.2.0 starting
2026-10-27 10:00:02,000 INFO
To pair this bridge with your 3D Print Log account, scan the QR code with your phone, or open
  https://auth.3dprintlog.com/activate
and enter the code  QWER-TYUI
(or open https://auth.3dprintlog.com/activate?user_code=QWER-TYUI)
The code works for 15 minutes.

  ## QR ##'
PAIRED_LOG="${PAIRING_LOG}
2026-10-27 10:01:00,000 INFO Paired as \"printlog-bridge on pi\". The device token is in /data/state/device_token."

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
says() { grep -qF -- "$1" "${WORK}/out.txt"; }
called() { grep -qF -- "$1" "${FAKE}/calls.log"; }

# A new scenario: a fresh home and fake Docker state. Then `install [args]` runs the script.
scenario() {
    echo "== $1"
    export HOME="${WORK}/home-$2"
    export FAKE="${WORK}/fake-$2"
    mkdir -p "$HOME" "$FAKE"
    : >"${FAKE}/calls.log"
    unset FAKE_PODMAN FAKE_DENIED FAKE_DOWN FAKE_OS FAKE_ARCH FAKE_SECURITY FAKE_NO_COMPOSE \
        FAKE_STATE FAKE_BOOT FAKE_UNAME PRINTLOG_BRIDGE_DIR PRINTLOG_BRIDGE_TAG
    BIN="$FAKEBIN"
    DIR="${HOME}/printlog-bridge"
}
status=0
install() {
    set +e
    PATH="${BIN}:${TOOLS}" TZ="Europe/Berlin" PRINTLOG_BRIDGE_POLL_SECONDS=0 \
        PRINTLOG_BRIDGE_START_TIMEOUT="${START_TIMEOUT:-5}" PRINTLOG_BRIDGE_APPROVE_TIMEOUT="${APPROVE_TIMEOUT:-5}" \
        "${TOOLS}/sh" "$SCRIPT" "$@" </dev/null >"${WORK}/out.txt" 2>&1
    status=$?
    set -e
    sed 's/^/      | /' "${WORK}/out.txt"
}
refused() { test "$status" -ne 0 && ! called "compose up" && test ! -e "${DIR}/compose.yaml"; }

scenario "a new install on a Linux computer that's already paired" fresh
echo 1 >"${FAKE}/healthy_after"
printf '%s\n' "$PAIRED_LOG" >"${FAKE}/logs"
install
COMPOSE="${DIR}/compose.yaml"
check "it succeeds" test "$status" -eq 0
check "the Compose file is in ~/printlog-bridge" test -f "$COMPOSE"
check "it says the script wrote it" grep -q "^# Written by printlog-bridge's docker-install.sh" "$COMPOSE"
check "it runs the image with the script's tag" \
    grep -qx '    image: ghcr.io/hoffmanengineering/printlog-bridge:latest' "$COMPOSE"
check "on the host's network" grep -qx '    network_mode: host' "$COMPOSE"
check "restarting on boot" grep -qx '    restart: unless-stopped' "$COMPOSE"
check "with a fixed container name" grep -qx '    container_name: printlog-bridge' "$COMPOSE"
check "keeping its state in the folder" grep -qx '      - .:/data' "$COMPOSE"
check "in this computer's time zone" grep -qx '      TZ: Europe/Berlin' "$COMPOSE"
check "no API key in it" sh -c "! grep -qi 'api_key' '$COMPOSE'"
check "no ports published" sh -c "! grep -q 'ports:' '$COMPOSE'"
check "pulls the image in the folder" called "${DIR}: docker compose pull"
check "starts it in the folder" called "${DIR}: docker compose up -d"
check "checks the bridge's health" called "docker exec printlog-bridge printlog-bridge-health"
check "says it runs" says "printlog-bridge is running."
check "shows no old pairing code" sh -c "! grep -q 'QWER-TYUI' '${WORK}/out.txt'"
check "says where the status page is" says "http://127.0.0.1:7180/"
check "says how to reach it from another computer" says "ssh -L 7180:127.0.0.1:7180"
check "says how to open it to the network" says "PRINTLOG_BRIDGE_STATUS_PAGE: 0.0.0.0:7180"
check "with the warning about the pairing code" says "whoever approves it first gets the bridge"
check "says how to see the log" says "docker compose logs -f"
check "says how to uninstall" says "--uninstall"
check "never asks for an API key" sh -c "! grep -qi 'api key' '${WORK}/out.txt'"
check "doesn't use sudo when Docker works without it" sh -c "! grep -q '^sudo' '${FAKE}/calls.log'"
if [ -n "$REAL_DOCKER" ] && "$REAL_DOCKER" compose version >/dev/null 2>&1; then
    check "docker compose accepts the file" "$REAL_DOCKER" compose -f "$COMPOSE" config -q
else
    echo "skip  docker compose accepts the file (no docker compose here)"
fi

echo "== the same folder again, with config, state, an override and a new tag"
printf '[printer voron]\nprinter_id = 7\n' >"${DIR}/printlog-bridge.cfg"
mkdir -p "${DIR}/state"
printf 'secret-device-token' >"${DIR}/state/device_token"
printf 'services:\n  printlog-bridge:\n    environment:\n      PRINTLOG_BRIDGE_STATUS_PAGE: 0.0.0.0:7180\n' \
    >"${DIR}/compose.override.yaml"
cp -r "$DIR" "${WORK}/before"
: >"${FAKE}/calls.log"
install --tag 0.2.0
check "it succeeds again" test "$status" -eq 0
check "the config is kept" cmp -s "${DIR}/printlog-bridge.cfg" "${WORK}/before/printlog-bridge.cfg"
check "the device token is kept" cmp -s "${DIR}/state/device_token" "${WORK}/before/state/device_token"
check "the override is kept" cmp -s "${DIR}/compose.override.yaml" "${WORK}/before/compose.override.yaml"
check "the image tag is updated" \
    grep -qx '    image: ghcr.io/hoffmanengineering/printlog-bridge:0.2.0' "$COMPOSE"
check "the rest of the file is the same" \
    sh -c "diff '$COMPOSE' '${WORK}/before/compose.yaml' | grep -c '^[<>]' | grep -qx 2"
check "no stray files in the folder" \
    test "$(cd "$DIR" && echo .[!.]* *)" = "$(cd "${WORK}/before" && echo .[!.]* *)"
check "pulls and starts again" called "${DIR}: docker compose up -d"
check "the device token never reaches the output" sh -c "! grep -q secret-device-token '${WORK}/out.txt'"
check "notices the override" says "compose.override.yaml"

scenario "pairing, without waiting" pairing
printf '%s\n' "$PAIRING_LOG" >"${FAKE}/logs"
install --no-wait
check "it succeeds" test "$status" -eq 0
check "shows the code" says "QWER-TYUI"
check "shows the address" says "https://auth.3dprintlog.com/activate"
check "shows the link with the code" says "https://auth.3dprintlog.com/activate?user_code=QWER-TYUI"
check "says where the QR code is" says "docker compose logs"
check "says where the status page is" says "http://127.0.0.1:7180/"
check "doesn't claim it runs yet" sh -c "! grep -q 'printlog-bridge is running.' '${WORK}/out.txt'"

scenario "pairing, waiting for the approval" approve
printf '%s\n' "$PAIRING_LOG" >"${FAKE}/logs"
echo 3 >"${FAKE}/healthy_after"
install --wait
check "it succeeds" test "$status" -eq 0
check "shows the code" says "QWER-TYUI"
check "then says it runs" says "printlog-bridge is running."
check "the code comes before that" \
    sh -c "grep -n -e QWER-TYUI -e 'printlog-bridge is running.' '${WORK}/out.txt' | head -1 | grep -q QWER"

scenario "pairing, nobody approves in time" timeout
printf '%s\n' "$PAIRING_LOG" >"${FAKE}/logs"
APPROVE_TIMEOUT=0 install --wait
check "it still succeeds (the bridge keeps waiting)" test "$status" -eq 0
check "shows the code" says "QWER-TYUI"
check "says the bridge keeps waiting" says "keeps waiting"

scenario "a bridge that stops at once" crash
FAKE_STATE=exited
export FAKE_STATE
printf '2026-10-27 10:00:01,000 ERROR printlog-bridge.cfg: line 3: no section\n' >"${FAKE}/logs"
install
check "it fails" test "$status" -ne 0
check "shows the end of the log" says "line 3: no section"

scenario "another folder, with --dir and an environment variable" dir
echo 1 >"${FAKE}/healthy_after"
install --dir "${HOME}/elsewhere"
check "--dir is used" test -f "${HOME}/elsewhere/compose.yaml"
check "compose runs there" called "${HOME}/elsewhere: docker compose up -d"
install --dir "${HOME}/elsewhere" --uninstall >/dev/null
PRINTLOG_BRIDGE_DIR="${HOME}/from-env" PRINTLOG_BRIDGE_TAG=edge
export PRINTLOG_BRIDGE_DIR PRINTLOG_BRIDGE_TAG
install
check "PRINTLOG_BRIDGE_DIR is used" test -f "${HOME}/from-env/compose.yaml"
check "PRINTLOG_BRIDGE_TAG is used" \
    grep -qx '    image: ghcr.io/hoffmanengineering/printlog-bridge:edge' "${HOME}/from-env/compose.yaml"
unset PRINTLOG_BRIDGE_DIR PRINTLOG_BRIDGE_TAG
install --tag 'latest;rm -rf'
check "a tag that isn't one is refused" test "$status" -ne 0
install --frobnicate
check "an unknown option is refused" test "$status" -ne 0
install --help
check "--help works" sh -c "test $status -eq 0 && grep -q -- '--uninstall' '${WORK}/out.txt'"

scenario "a Compose file the script didn't write" foreign-compose
mkdir -p "$DIR"
printf 'services: {}\n' >"${DIR}/compose.yaml"
install
check "it refuses" sh -c "test $status -ne 0 && ! grep -q 'compose up' '${FAKE}/calls.log'"
check "the file is left alone" grep -qx 'services: {}' "${DIR}/compose.yaml"
rm "${DIR}/compose.yaml"
printf 'services: {}\n' >"${DIR}/docker-compose.yml"
install
check "a docker-compose.yml from the README is refused too" \
    sh -c "test $status -ne 0 && test ! -e '${DIR}/compose.yaml'"
check "saying how to move it aside" says "docker-compose.yml"

scenario "a printlog-bridge container from docker run" foreign-container
touch "${FAKE}/foreign"
install
check "it refuses" refused
check "says how to remove it, keeping its folder" says "docker rm -f printlog-bridge"

scenario "Docker Desktop" desktop
FAKE_OS="Docker Desktop"
export FAKE_OS
install
check "it refuses" refused
check "explains host networking there" says "can't see your network"

scenario "macOS" macos
FAKE_UNAME=Darwin
export FAKE_UNAME
install
check "it refuses" refused
check "explains why" says "can't see your network"
check "before calling Docker" sh -c "! grep -q 'docker' '${FAKE}/calls.log'"

scenario "rootless Docker" rootless
FAKE_SECURITY='[name=seccomp,profile=builtin name=rootless name=cgroupns]'
export FAKE_SECURITY
install
check "it refuses" refused
check "explains why" says "rootless"

scenario "Podman" podman
FAKE_PODMAN=1
export FAKE_PODMAN
install
check "it refuses" refused
check "explains why" says "Podman"

scenario "a 32-bit Raspberry Pi" armv7
FAKE_ARCH=armv7l
export FAKE_ARCH
echo 1 >"${FAKE}/healthy_after"
install
check "is supported" test "$status" -eq 0

scenario "a Raspberry Pi Zero (armv6)" armv6
FAKE_ARCH=armv6l
export FAKE_ARCH
install
check "it refuses" refused
check "names the platforms there are" says "linux/arm/v7"

scenario "no Docker" no-docker
BIN="$BARE"
install
check "it refuses" sh -c "test $status -ne 0 && test ! -e '$DIR'"
check "says how to install Docker" says "https://get.docker.com"

scenario "no Docker Compose" no-compose
FAKE_NO_COMPOSE=1
export FAKE_NO_COMPOSE
install
check "it refuses" refused
check "says how to install it" says "docker-compose-plugin"

scenario "Docker not running" docker-down
FAKE_DOWN=1
export FAKE_DOWN
install
check "it refuses" refused
check "says to start it" says "systemctl enable --now docker"

scenario "a user who isn't in the docker group, with sudo" sudo
if [ "$(id -u)" = 0 ]; then
    echo "skip  (running as root, which never needs sudo)"
else
    FAKE_DENIED=1
    export FAKE_DENIED
    echo 1 >"${FAKE}/healthy_after"
    install
    check "it succeeds" test "$status" -eq 0
    check "runs Docker with sudo" called "${DIR}: docker compose up -d (sudo)"
    check "says so, and how to do without" says "usermod -aG docker"
    check "the folder is still this user's" test -O "$DIR"
    check "commands it prints use sudo" says "sudo docker compose logs -f"
fi

scenario "a user who isn't in the docker group, without sudo" no-sudo
FAKE_DENIED=1
export FAKE_DENIED
BIN="${WORK}/nosudo"
mkdir -p "$BIN"
cp "${FAKEBIN}/docker" "${FAKEBIN}/uname" "$BIN/"
install
check "it refuses" refused
check "says how to get access" says "usermod -aG docker"

scenario "Docker doesn't start at boot" boot
FAKE_BOOT=disabled
export FAKE_BOOT
echo 1 >"${FAKE}/healthy_after"
install
check "it succeeds" test "$status" -eq 0
check "warns, and says how to fix it" says "sudo systemctl enable docker"

scenario "uninstall" uninstall
echo 1 >"${FAKE}/healthy_after"
install
printf '[bridge]\n' >"${DIR}/printlog-bridge.cfg"
: >"${FAKE}/calls.log"
install --uninstall
check "it succeeds" test "$status" -eq 0
check "stops and removes the container" called "${DIR}: docker compose down"
check "keeps the folder" test -f "${DIR}/printlog-bridge.cfg"
check "says how to remove the rest" says "rm -rf ${DIR}"
check "says to remove the bridge in 3D Print Log" says "Bridges"
rm -rf "$DIR"
install --uninstall
check "with nothing installed, says so" sh -c "test $status -ne 0 && grep -q 'nothing to uninstall' '${WORK}/out.txt'"

scenario "the copy attached to a release" release
# docker.yml's release job stamps the version into the script with this same sed.
sed 's/^DEFAULT_TAG=latest$/DEFAULT_TAG=0.2.0/' "$SCRIPT" >"${WORK}/stamped.sh"
check "stamping changes exactly one line" \
    test "$(diff "$SCRIPT" "${WORK}/stamped.sh" | grep -c '^>')" = 1
SCRIPT="${WORK}/stamped.sh"
echo 1 >"${FAKE}/healthy_after"
install
check "it runs that version" \
    grep -qx '    image: ghcr.io/hoffmanengineering/printlog-bridge:0.2.0' "${DIR}/compose.yaml"

echo
if [ "$failures" -ne 0 ]; then
    echo "$failures check(s) failed"
    exit 1
fi
echo "all checks passed"
