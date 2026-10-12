#!/usr/bin/env bash
# Smoke-tests a built image: --version, store-key, doctor, run and status inside the container,
# against three fake Moonrakers and a fake 3D Print Log API on this computer (tests/docker/
# serve_fakes.py), so nothing calls the real API. CI runs it (docker-smoke) after building the
# image for linux/amd64. Needs docker, and python3 with aiohttp for the fakes.
#
# Usage: tests/docker/smoke.sh <image>
set -euo pipefail

IMAGE="${1:?usage: $0 <image>}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
DATA="${WORK}/data"
REPORT="${WORK}/report.json"
NAME="printlog-bridge-smoke-$$"
mkdir -p "$DATA"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    [ -n "${FAKES:-}" ] && kill "$FAKES" 2>/dev/null || true
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
# Runs the image as this user, the way Synology's `user: "<uid>:<gid>"` does, on the host's
# network so it reaches the fakes on 127.0.0.1.
bridge() { docker run --rm -i --network host --user "$(id -u):$(id -g)" -v "${DATA}:/data" "$IMAGE" "$@"; }
report() { python3 -c "import json,sys; r=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))" "$REPORT" "$1"; }
wait_for() { # <seconds> <command...>
    local deadline=$((SECONDS + $1))
    shift
    until "$@"; do
        [ "$SECONDS" -lt "$deadline" ] || return 1
        sleep 1
    done
}

echo "== image"
docker image inspect "$IMAGE" --format 'size: {{.Size}} bytes, user: {{.Config.User}}'
check "runs as a non-root user by default" \
    test "$(docker run --rm --entrypoint id "$IMAGE" -u)" = 1000
check "labels the source repository" \
    test "$(docker image inspect "$IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.source"}}')" \
    = https://github.com/HoffmanEngineering/printlog-bridge
check "TZ sets the time zone (tzdata is in the image)" \
    test "$(docker run --rm -e TZ=America/New_York --entrypoint python "$IMAGE" -c 'import time; print(time.tzname[0])')" = EST

echo "== --version"
version="$(bridge --version)"
echo "$version"
check "--version names the bridge" grep -q '^printlog-bridge ' <<<"$version"

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
docker run -d --name "$NAME" --network host --user "$(id -u):$(id -g)" -v "${DATA}:/data" \
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

if [ "$failures" -ne 0 ]; then
    echo "== bridge log"
    docker logs "$NAME" 2>&1 | tail -50 || true
    echo "$failures check(s) failed"
    exit 1
fi
echo "all checks passed"
