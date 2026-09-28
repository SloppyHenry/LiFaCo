#!/usr/bin/env bash
# Startet Dienst + Oberfläche mit simulierter Hardware – ohne root, ohne echte Lüfter anzufassen.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO="$(mktemp -d -t fancontrol-demo.XXXXXX)"
export FANCONTROL_HWMON_ROOT="$DEMO/hwmon"
export FANCONTROL_CONFIG_DIR="$DEMO/config"
export FANCONTROL_SOCKET="$DEMO/daemon.sock"
export FANCONTROL_NO_NVIDIA=1
export FANCONTROL_FILE_SENSOR_DIRS="$DEMO/sensors"
mkdir -p "$DEMO/sensors"
echo 42.0 > "$DEMO/sensors/beispiel.sensor"

cleanup() {
    kill "${DAEMON_PID:-}" "${SIM_PID:-}" 2>/dev/null || true
    wait 2>/dev/null || true
    rm -rf "$DEMO"
}
trap cleanup EXIT

python3 "$SRC/tools/fake_hwmon.py" "$FANCONTROL_HWMON_ROOT" &
SIM_PID=$!
sleep 1
python3 "$SRC/bin/fancontrol-linuxd" --group '' &
DAEMON_PID=$!
for _ in $(seq 50); do [[ -S "$FANCONTROL_SOCKET" ]] && break; sleep 0.1; done

if [[ "${1:-}" == "--cli" ]]; then
    python3 "$SRC/bin/fancontrol-linuxctl" status
else
    python3 "$SRC/bin/fancontrol-linux"
fi
