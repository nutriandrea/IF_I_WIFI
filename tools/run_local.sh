#!/usr/bin/env bash
# run_local.sh — One-shot demo locale (server + simulator + UI).
#
# Lancia:
#   1. ws_server in background con --enable-3d
#   2. inject_radar3d_frames in background (movimento simulato)
#   3. Apre mapping/ui.html nel browser (Mac/Linux)
#
# Quando premi Ctrl-C, killa entrambi.
#
# Uso:
#   ./tools/run_local.sh
#   ./tools/run_local.sh --no-3d
#   ./tools/run_local.sh --no-browser

set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"

ENABLE_3D=1
OPEN_BROWSER=1
INJECTOR_ARGS="--moving"

for arg in "$@"; do
  case "$arg" in
    --no-3d)       ENABLE_3D=0 ;;
    --no-browser)  OPEN_BROWSER=0 ;;
    --static)      INJECTOR_ARGS="" ;;
    --help|-h)
      sed -n '2,15p' "$0"; exit 0 ;;
  esac
done

# Cleanup all background processes on exit
PIDS=()
cleanup() {
  echo
  echo "==> Stop sub-processi: ${PIDS[*]:-none}"
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

echo "==> Avvio ws_server (UDP :5005, WS :8765)"
WS_ARGS=(--udp-port 5005 --ws-port 8765 --room 6x5 --grid 4x4 \
         --rx "0.5,0.5;5.5,0.5;3.0,4.5")
if [ "$ENABLE_3D" = "1" ]; then
  WS_ARGS+=(--enable-3d --room-height 3.0)
fi
python3 -m csi.quadrants.ws_server "${WS_ARGS[@]}" &
PIDS+=($!)
sleep 1.5

echo "==> Avvio inject_radar3d_frames (simulazione 3 ESP32 + movimento)"
python3 tools/inject_radar3d_frames.py --port 5005 $INJECTOR_ARGS &
PIDS+=($!)
sleep 0.5

UI_URL="file://$PWD/mapping/ui.html?ws=ws://localhost:8765&room=6x5&grid=4x4"
if [ "$ENABLE_3D" = "0" ]; then
  UI_URL="${UI_URL}&3d=0"
fi

if [ "$OPEN_BROWSER" = "1" ]; then
  if command -v open >/dev/null 2>&1; then
    echo "==> Apertura UI nel browser (open): $UI_URL"
    open "$UI_URL"
  elif command -v xdg-open >/dev/null 2>&1; then
    echo "==> Apertura UI nel browser (xdg-open): $UI_URL"
    xdg-open "$UI_URL" &
  else
    echo "==> Apri manualmente: $UI_URL"
  fi
else
  echo "==> UI: $UI_URL"
fi

echo
echo "==> Ctrl-C per fermare tutto."
wait
