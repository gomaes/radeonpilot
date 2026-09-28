#!/usr/bin/env bash
# Run the full stack (daemon + GUI) against an emulated RX 9070 XT + RX 7900 XTX.
# Nothing touches the real /sys. Usage: scripts/run-emulated.sh [--no-od]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
py="${PYTHON:-python3}"
[[ -x "$here/.venv/bin/python" ]] && py="$here/.venv/bin/python"

work="$(mktemp -d -t radeonpilot-emu.XXXXXX)"
daemon_pid=""
gui_pid=""
cleanup() {
    local pid
    for pid in $gui_pid $daemon_pid; do kill "$pid" 2>/dev/null || true; done
    wait 2>/dev/null || true
    rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

cd "$here"
"$py" -m radeonpilot.emulator build "$work/sysfs" "$@"
export RADEONPILOT_SYSFS_ROOT="$work/sysfs"
export RADEONPILOT_SOCKET="$work/radeonpilot.sock"
export RADEONPILOT_CONFIG="$work/config.json"
export XDG_CONFIG_HOME="$work/xdg-config"
export XDG_DATA_HOME="$work/xdg-data"

"$py" -m radeonpilot.daemon --emulate &
daemon_pid=$!
for _ in $(seq 50); do [[ -S "$RADEONPILOT_SOCKET" ]] && break; sleep 0.1; done

echo "emulated sysfs: $RADEONPILOT_SYSFS_ROOT"
"$py" -m radeonpilot &
gui_pid=$!
wait "$gui_pid" || true
