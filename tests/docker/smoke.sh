#!/usr/bin/env bash
# Smoke-tests a built image: --version, store-key, doctor, run, status and pair inside the
# container, then a new install with no config and a root-owned folder, against three fake
# Moonrakers, a fake 3D Print Log API and a fake Auth0 on this
# computer (tests/docker/serve_fakes.py), so nothing calls the real API. CI runs it (docker.yml,
# smoke) after building the image for each platform: arm64 on an Arm runner, arm/v7 under QEMU.
# Needs docker, and python3 with aiohttp for the fakes.
#
# Usage: tests/docker/smoke.sh <image> [platform]   (the platform defaults to the image's own)
set -euo pipefail

IMAGE="${1:?usage: $0 <image> [platform]}"
# linux/amd64, linux/arm64 or linux/arm/v7 (arm64 is sometimes recorded as linux/arm64/v8).
image_platform() {
    docker image inspect "$IMAGE" --format '{{.Os}}/{{.Architecture}}{{with .Variant}}/{{.}}{{end}}' \
        | sed 's#^linux/arm64/v8$#linux/arm64#'
}
PLATFORM="${2:-$(image_platform)}"
# What `uname -m` says inside the container on that platform.
case "$PLATFORM" in
    linux/amd64) MACHINE=x86_64 ;;
    linux/arm64) MACHINE=aarch64 ;;
    linux/arm/v7) MACHINE=armv7l ;;
    *)
        echo "no expected uname -m for $PLATFORM" >&2
        exit 2
        ;;
esac
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
DATA="${WORK}/data"
REPORT="${WORK}/report.json"
NAME="printlog-bridge-smoke-$$"
NEW_NAME="printlog-bridge-smoke-new-$$"
mkdir -p "$DATA"

cleanup() {
    docker rm -f "$NAME" "$NEW_NAME" >/dev/null 2>&1 || true
    if [ -n "${FAKES:-}" ]; then kill "$FAKES" 2>/dev/null || true; fi
    # The new install's folders belong to other users now; remove them as root, from a container.
    docker run --rm --platform "$PLATFORM" --entrypoint rm -v "${WORK}:/work" "$IMAGE" \
        -rf /work/new /work/other >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

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
# Runs the image as this user, the way installs from before #99 do with `user: "<uid>:<gid>"`,
# on the host's network so it reaches the fakes on 127.0.0.1.
bridge() { docker run --rm -i --platform "$PLATFORM" --network host --user "$(id -u):$(id -g)" -v "${DATA}:/data" "$IMAGE" "$@"; }
inside() { docker run --rm --platform "$PLATFORM" "$@"; }
report() { python3 -c "import json,sys; r=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))" "$REPORT" "$1"; }
wait_for() { # <seconds> <command...>
    local deadline=$((SECONDS + $1))
    shift
    until "$@"; do
        [ "$SECONDS" -lt "$deadline" ] || return 1
        sleep 1
    done
}

echo "== image ($PLATFORM)"
docker image inspect "$IMAGE" --format 'size: {{.Size}} bytes, user: {{.Config.User}}'
check "is built for $PLATFORM" test "$(image_platform)" = "$PLATFORM"
check "runs on a $MACHINE CPU" test "$(inside --entrypoint uname "$IMAGE" -m)" = "$MACHINE"
check "starts as root, only to hand /data to the bridge's user" \
    test "$(inside --entrypoint id "$IMAGE" -u)" = 0
check "the image's own user is 1000" \
    test "$(inside --entrypoint id "$IMAGE" -u printlog)" = 1000
check "labels the source repository" \
    test "$(docker image inspect "$IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.source"}}')" \
    = https://github.com/HoffmanEngineering/printlog-bridge
check "TZ sets the time zone (tzdata is in the image)" \
    test "$(inside -e TZ=America/New_York --entrypoint python "$IMAGE" -c 'import time; print(time.tzname[0])')" = EST

echo "== --version"
version="$(bridge --version)"
echo "$version"
check "--version names the bridge" grep -q '^printlog-bridge ' <<<"$version"
check "cryptography is installed (sealed credentials)" \
    inside --entrypoint python "$IMAGE" -c \
    'from printlog_bridge.core import credentials; assert credentials.available()'

echo "== fakes"
python3 "${REPO}/tests/docker/serve_fakes.py" "$DATA" "$REPORT" &
FAKES=$!
check "the fakes wrote the config" wait_for 20 test -f "${DATA}/printlog-bridge.cfg"
cat "${DATA}/printlog-bridge.cfg"

echo "== store-key"
echo "key-123" | bridge store-key
check "the key file is in the volume" test -f "${DATA}/state/api_key"
check "only its owner can read it" test "$(stat -c %a "${DATA}/state/api_key")" = 600

echo "== doctor"
set +e
bridge doctor >"${WORK}/doctor.txt"
code=$?
set -e
cat "${WORK}/doctor.txt"
check "doctor passes" test "$code" -eq 0
check "doctor reached all three printers" \
    test "$(grep -c '^  ok    Printer .* in 3D Print Log' "${WORK}/doctor.txt")" -eq 3
check "the support bundle is in the volume" test -f "${DATA}/state/printlog-bridge-support.zip"

echo "== run"
docker run -d --platform "$PLATFORM" --name "$NAME" --network host --user "$(id -u):$(id -g)" -v "${DATA}:/data" \
    --health-interval 3s --health-start-period 5s "$IMAGE" >/dev/null
healthy() { test "$(docker inspect -f '{{.State.Health.Status}}' "$NAME")" = healthy; }
subscribed_three() { test "$(report 'len(r["subscribed"])' 2>/dev/null)" = 3; }
logged_three() { test "$(report 'sum(p["status"] == 3 for p in r["prints"])' 2>/dev/null)" = 3; }
check "the container becomes healthy" wait_for 60 healthy
check "one container watches all three printers" wait_for 30 subscribed_three
check "a print on each printer is logged and completed" wait_for 60 logged_three
report 'r["prints"]'

echo "== status"
set +e
docker exec "$NAME" printlog-bridge status >"${WORK}/status.txt"
code=$?
set -e
cat "${WORK}/status.txt"
check "status says it runs" test "$code" -eq 0
check "status shows three connected printers" \
    test "$(grep -c 'Connection: connected' "${WORK}/status.txt")" -eq 3
check "status reads the key from the volume" grep -q 'API key from /data/state/api_key' "${WORK}/status.txt"

echo "== local page"
check "the local page answers on this computer" grep -q 'Printer voron (printer 41 in 3D Print Log)' <<<"$(curl -fsS http://127.0.0.1:7180/)"
check "the local page changes nothing" test "$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:7180/)" = 405

echo "== stop"
docker stop -t 20 "$NAME" >/dev/null
check "the bridge exits 0 on SIGTERM" test "$(docker inspect -f '{{.State.ExitCode}}' "$NAME")" -eq 0
set +e
bridge status >"${WORK}/status.txt"
code=$?
set -e
cat "${WORK}/status.txt"
check "status then says it stopped" grep -q 'not running: stopped at' "${WORK}/status.txt"
check "and exits 1, which marks the container unhealthy" test "$code" -eq 1

echo "== pair"
set +e
bridge pair --yes --name "smoke test" >"${WORK}/pair.txt" 2>&1
code=$?
set -e
cat "${WORK}/pair.txt"
check "pair works without a terminal" test "$code" -eq 0
check "pair shows the code to approve" grep -q 'enter the code' "${WORK}/pair.txt"
check "the device token is in the volume" test -f "${DATA}/state/device_token"
check "only its owner can read it" test "$(stat -c %a "${DATA}/state/device_token")" = 600
set +e
bridge doctor >"${WORK}/doctor.txt"
set -e
check "doctor signs in with the device token" grep -q 'with the device token' "${WORK}/doctor.txt"
check "pair shows the encryption key" grep -q '^Encryption key: [0-9a-f]\{4\} ' "${WORK}/pair.txt"
check "only its owner can read the key" test "$(stat -c %a "${DATA}/state/credential-key.pem")" = 600
check "doctor shows the key" grep -q 'ok    Credential key: fingerprint' "${WORK}/doctor.txt"

# A new install as the NAS docs now have it: no `user:`, no config file, and a data folder that
# belongs to root (Docker makes a missing bind-mount folder as root). The container starts as
# root, gives the folder to the image's user and runs as it; with no printers it writes a
# starter config and waits, healthy, until printers are added. PRINTLOG_API_KEY keeps it from
# pairing against the real 3D Print Log until the config (pointing at the fakes) is in place.
echo "== new install: no config, a root-owned folder, no user:"
NEW="${WORK}/new"
mkdir -p "$NEW"
docker run --rm --platform "$PLATFORM" --entrypoint chown -v "${NEW}:/data" "$IMAGE" 0:0 /data
check "the folder starts out root's" test "$(stat -c %u "$NEW")" = 0
docker run -d --platform "$PLATFORM" --name "$NEW_NAME" --network host -v "${NEW}:/data" \
    -e PRINTLOG_API_KEY=key-123 --health-interval 3s --health-start-period 5s "$IMAGE" >/dev/null
new_healthy() { test "$(docker inspect -f '{{.State.Health.Status}}' "$NEW_NAME")" = healthy; }
new_log_has() { docker logs "$NEW_NAME" 2>&1 | grep -q "$1"; }
check "it says there are no printers yet" wait_for 60 new_log_has 'No printers to watch yet'
check "and stays up, healthy, instead of restarting" wait_for 60 new_healthy
check "it wrote a starter config" test -f "${NEW}/printlog-bridge.cfg"
check "the folder now belongs to the image's user, 1000" test "$(stat -c %u "$NEW")" = 1000
check "the bridge runs as 1000, not root" \
    grep -Eq '^Uid:[[:space:]]+1000[[:space:]]' <(docker exec "$NEW_NAME" cat /proc/1/status)
set +e
docker exec "$NEW_NAME" printlog-bridge status >"${WORK}/new-status.txt"
code=$?
set -e
cat "${WORK}/new-status.txt"
check "status (run as root by docker exec) says it waits for printers" \
    grep -q '^Setup: waiting for printers' "${WORK}/new-status.txt"
check "status exits 0: the bridge runs" test "$code" -eq 0
check "docker exec left nothing root's in the folder" \
    test -z "$(docker run --rm --platform "$PLATFORM" --entrypoint find -v "${NEW}:/data" "$IMAGE" /data -not -uid 1000)"
# Add the printers, as someone editing the file over the network (as its owner) would.
docker exec -i --user 1000:1000 "$NEW_NAME" sh -c 'cat > /data/printlog-bridge.cfg' <"${DATA}/printlog-bridge.cfg"
check "it starts watching once printers are added, with no restart" \
    wait_for 30 new_log_has 'starting to watch'
check "and connects to them" wait_for 30 new_log_has 'connected'
check "the container never restarted" \
    test "$(docker inspect -f '{{.RestartCount}}' "$NEW_NAME")" -eq 0
docker stop -t 20 "$NEW_NAME" >/dev/null

echo "== PUID and PGID"
OTHER="${WORK}/other"
mkdir -p "$OTHER"
docker run --rm --platform "$PLATFORM" -e PUID=1234 -e PGID=4321 -v "${OTHER}:/data" "$IMAGE" status \
    >/dev/null 2>&1 || true
check "PUID/PGID choose who gets the folder" test "$(stat -c %u:%g "$OTHER")" = 1234:4321

if [ "$failures" -ne 0 ]; then
    echo "== bridge log"
    docker logs "$NAME" 2>&1 | tail -50 || true
    echo "== new install's log"
    docker logs "$NEW_NAME" 2>&1 | tail -50 || true
    echo "$failures check(s) failed"
    exit 1
fi
echo "all checks passed"
