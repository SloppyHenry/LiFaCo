#!/usr/bin/env bash
# Starts service + user interface with simulated hardware – no root, real fans are never touched.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO="$(mktemp -d -t fancontrol-demo.XXXXXX)"
export FANCONTROL_HWMON_ROOT="$DEMO/hwmon"
export FANCONTROL_CONFIG_DIR="$DEMO/config"
export FANCONTROL_SOCKET="$DEMO/daemon.sock"
export FANCONTROL_NO_NVIDIA=1
export FANCONTROL_DEMO=1   # own application ID and a "Demo" label, so it never replaces the real app
export FANCONTROL_FILE_SENSOR_DIRS="$DEMO/sensors"
mkdir -p "$DEMO/sensors"
echo 42.0 > "$DEMO/sensors/beispiel.sensor"
# Lighting: plugins are installed into the demo folder; the catalog comes from a local copy of the plugin repository
# (../LiFaCo-plugins or $LIFACO_PLUGIN_REPO) so that searching and installing works without a network.
export FANCONTROL_PLUGINS_DIR="$DEMO/plugins"
export FANCONTROL_PLUGIN_DATA_DIR="$DEMO/plugin-data"
PLUGIN_REPO="${LIFACO_PLUGIN_REPO:-$SRC/../LiFaCo-plugins}"
if [[ -f "$PLUGIN_REPO/index.json" ]]; then
    CATALOG_PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
    python3 -m http.server "$CATALOG_PORT" --bind 127.0.0.1 --directory "$PLUGIN_REPO" >/dev/null 2>&1 &
    CATALOG_PID=$!
    export FANCONTROL_CATALOG_URL="http://127.0.0.1:$CATALOG_PORT/index.json"
fi

cleanup() {
    kill "${DAEMON_PID:-}" "${SIM_PID:-}" "${CATALOG_PID:-}" 2>/dev/null || true
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
