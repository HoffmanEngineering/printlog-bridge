#!/usr/bin/env bash
# Smoke-tests the Home Assistant app's image (the Dockerfile's addon stage) the way the
# Supervisor runs it: as root, on the host's network, with a root-owned /data holding the app's
# options in options.json. Against the fakes on this computer (tests/docker/serve_fakes.py), so
# nothing calls the real API. Without credentials the bridge pairs first (the fake Auth0
# approves at once), then logs a print on each of three printers. CI runs it (docker.yml, smoke)
# on linux/amd64 and linux/arm64. Needs docker, and python3 with aiohttp for the fakes.
#
# Usage: tests/docker/addon_smoke.sh <image> [platform]
set -euo pipefail

IMAGE="${1:?usage: $0 <image> [platform]}"
PLATFORM="${2:-linux/amd64}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
DATA="${WORK}/data"
BAD="${WORK}/bad"
REPORT="${WORK}/report.json"
NAME="printlog-bridge-addon-smoke-$$"
mkdir -p "$DATA" "$BAD"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    if [ -n "${FAKES:-}" ]; then kill "$FAKES" 2>/dev/null || true; fi
    # The app gave /data to uid 1000; remove it as root, from a container.
    docker run --rm --platform "$PLATFORM" --entrypoint rm -v "${WORK}:/work" "$IMAGE" \
        -rf /work/data /work/bad >/dev/null 2>&1 || true
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
report() { python3 -c "import json,sys; r=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))" "$REPORT" "$1"; }
wait_for() { # <seconds> <command...>
    local deadline=$((SECONDS + $1))
    shift
    until "$@"; do
        [ "$SECONDS" -lt "$deadline" ] || return 1
        sleep 1
    done
}
in_container() { docker exec "$NAME" "$@"; }

echo "== image ($PLATFORM)"
check "starts as root, as the Supervisor needs" \
    test "$(docker run --rm --platform "$PLATFORM" --entrypoint id "$IMAGE" -u)" = 0
version="$(docker run --rm --platform "$PLATFORM" "$IMAGE" --version)"
echo "$version"
check "--version names the bridge" grep -q '^printlog-bridge .*(Home Assistant app)$' <<<"$version"

echo "== options that can't work"
echo '{"printers": [], "trace": false}' >"${BAD}/options.json"
set +e
out="$(docker run --rm --platform "$PLATFORM" -v "${BAD}:/data" "$IMAGE" 2>&1)"
code=$?
set -e
echo "$out"
check "no printers: exits 2" test "$code" -eq 2
check "and says to add printers" grep -q 'Add your printers' <<<"$out"

echo "== fakes"
python3 "${REPO}/tests/docker/serve_fakes.py" "$DATA" "$REPORT" &
FAKES=$!
check "the fakes wrote the options" wait_for 20 test -f "${DATA}/options.json"
rm -f "${DATA}/printlog-bridge.cfg" # the app writes its own, from the options
give_data_to_root() { docker run --rm --platform "$PLATFORM" --entrypoint chown -v "${DATA}:/data" "$IMAGE" -R 0:0 /data; }
check "/data is root's, as the Supervisor makes it" give_data_to_root

echo "== run"
docker run -d --platform "$PLATFORM" --name "$NAME" --network host -v "${DATA}:/data" \
    --health-interval 3s --health-start-period 5s "$IMAGE" >/dev/null
healthy() { test "$(docker inspect -f '{{.State.Health.Status}}' "$NAME")" = healthy; }
paired() { docker logs "$NAME" 2>&1 | grep -q 'Paired'; }
subscribed_three() { test "$(report 'len(r["subscribed"])' 2>/dev/null)" = 3; }
logged_three() { test "$(report 'sum(p["status"] == 3 for p in r["prints"])' 2>/dev/null)" = 3; }
check "pairs on its first start, in the log" wait_for 60 paired
check "the log shows the code to approve" grep -q 'enter the code' <<<"$(docker logs "$NAME" 2>&1)"
check "the container becomes healthy" wait_for 60 healthy
check "it watches the three printers from the options" wait_for 30 subscribed_three
check "a print on each printer is logged and completed" wait_for 60 logged_three
report 'r["prints"]'

echo "== files"
check "the bridge runs as uid 1000, not root" \
    test "$(in_container python -c 'import os; print(os.stat("/proc/1").st_uid)')" = 1000
check "the config from the options is the bridge user's, mode 600" \
    test "$(in_container stat -c '%a %u' /data/printlog-bridge.cfg)" = "600 1000"
check "the device token is in /data/state, mode 600" \
    test "$(in_container stat -c '%a %u' /data/state/device_token)" = "600 1000"
check "the config has the printers" \
    test "$(in_container grep -c '^\[printer ' /data/printlog-bridge.cfg)" -eq 3

echo "== status"
set +e
in_container printlog-bridge status >"${WORK}/status.txt"
code=$?
set -e
cat "${WORK}/status.txt"
check "status says it runs" test "$code" -eq 0
check "status says it is paired" grep -q 'Pairing: paired' "${WORK}/status.txt"

echo "== stop"
docker stop -t 20 "$NAME" >/dev/null
check "the bridge exits 0 on SIGTERM" test "$(docker inspect -f '{{.State.ExitCode}}' "$NAME")" -eq 0

if [ "$failures" -ne 0 ]; then
    echo "== app log"
    docker logs "$NAME" 2>&1 | tail -80 || true
    echo "$failures check(s) failed"
    exit 1
fi
echo "all checks passed"
