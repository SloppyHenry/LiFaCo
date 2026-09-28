#!/usr/bin/env bash
# Baut dist/FanControl-x86_64.AppImage in einem Debian-13-Container (braucht Docker oder Podman).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-$ROOT/dist}"
ENGINE=$(command -v docker || command -v podman) || { echo "Docker oder Podman wird benötigt" >&2; exit 1; }
mkdir -p "$OUT"
"$ENGINE" run --rm -v "$ROOT":/src:ro -v "$OUT":/out -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    debian:13 bash /src/packaging/appimage/build-in-container.sh
echo "Fertig: $OUT/FanControl-x86_64.AppImage"
