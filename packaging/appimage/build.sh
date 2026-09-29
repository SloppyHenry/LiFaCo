#!/usr/bin/env bash
# Builds dist/LiFaCo-x86_64.AppImage in a Debian 13 container (needs Docker or Podman).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-$ROOT/dist}"
ENGINE=$(command -v docker || command -v podman) || { echo "Docker or Podman is required" >&2; exit 1; }
mkdir -p "$OUT"
"$ENGINE" run --rm -v "$ROOT":/src:ro -v "$OUT":/out -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    debian:13 bash /src/packaging/appimage/build-in-container.sh
echo "Done: $OUT/LiFaCo-x86_64.AppImage"
