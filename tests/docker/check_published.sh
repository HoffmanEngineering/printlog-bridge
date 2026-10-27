#!/usr/bin/env bash
# Checks a published image the way someone without a GitHub account sees it: it can be pulled
# without logging in, it has every platform, and each one starts. CI runs it (docker.yml, verify)
# after each publish; the arm platforms need QEMU (or an Arm host).
#
# Usage: tests/docker/check_published.sh <image:tag>
# PLATFORMS (comma-separated) overrides the platforms it must have, for the Home Assistant app's
# image (amd64 and arm64 only).
set -euo pipefail

REF="${1:?usage: $0 <image:tag>}"
IFS=, read -r -a PLATFORMS <<<"${PLATFORMS:-linux/amd64,linux/arm64,linux/arm/v7}"

# An empty Docker config, so nothing this machine has logged in to is used.
DOCKER_CONFIG="$(mktemp -d)"
export DOCKER_CONFIG
trap 'rm -rf "$DOCKER_CONFIG"' EXIT

failures=0
fail() {
    echo "FAIL  $1"
    failures=$((failures + 1))
}

echo "== $REF, without logging in"
format='{{range .Manifest.Manifests}}{{.Platform.OS}}/{{.Platform.Architecture}}{{with .Platform.Variant}}/{{.}}{{end}}{{"\n"}}{{end}}'
if ! published="$(docker buildx imagetools inspect "$REF" --format "$format" 2>&1)"; then
    echo "$published"
    echo "FAIL  $REF can't be read without logging in."
    echo "      GHCR makes a new package private. Make it public: GitHub > HoffmanEngineering >"
    package="${REF%:*}"
    echo "      Packages > ${package##*/} > Package settings > Change visibility."
    exit 1
fi
echo "ok    it can be read without logging in"
# arm64 is often listed with its variant, linux/arm64/v8.
published="${published//linux\/arm64\/v8/linux/arm64}"

for platform in "${PLATFORMS[@]}"; do
    if ! grep -qxF "$platform" <<<"$published"; then
        fail "it has no $platform image (it has: $(grep -v unknown <<<"$published" | paste -sd ' ' -))"
        continue
    fi
    if version="$(docker run --rm --pull always --platform "$platform" "$REF" --version 2>&1)" \
        && grep -q '^printlog-bridge ' <<<"$version"; then
        echo "ok    $platform pulls and starts: $version"
    else
        echo "$version"
        fail "$platform didn't start"
    fi
done

if [ "$failures" -ne 0 ]; then
    echo "$failures check(s) failed"
    exit 1
fi
echo "all checks passed"
