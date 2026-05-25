#!/usr/bin/env bash
# run_parallel.sh — Avvia Python ws_server + RuView Rust sensing-server in parallelo
#
# Architettura:
#   ESP32 ──UDP :5005──▶ Python ws_server  ──WS :8765──▶ Browser (nostra UI)
#                         │
#                         └──UDP relay :5006──▶ Rust sensing-server ──WS :8766──▶ Browser (RuView UI)
#                                                                   ──HTTP :8080──▶ REST API
#
# Usage:
#   ./run_parallel.sh                    # con HW reale
#   ./run_parallel.sh --simulate         # con dati simulati
#   ./run_parallel.sh --build-rust       # ricostruisce Rust prima di avviare

set -euo pipefail
cd "$(dirname "$0")"

SIMULATE=false
BUILD_RUST=false
PYTHON_UDP_PORT=5005
RUST_UDP_PORT=5006
PYTHON_WS_PORT=8765
RUST_WS_PORT=8766
RUST_HTTP_PORT=8080

for arg in "$@"; do
  case "$arg" in
    --simulate) SIMULATE=true ;;
    --build-rust) BUILD_RUST=true ;;
  esac
done

# ── Rust ──────────────────────────────────────────────────────────
if [ "$SIMULATE" = true ]; then
  RUST_SOURCE="simulate"
else
  RUST_SOURCE="esp32"
fi

if [ "$BUILD_RUST" = true ]; then
  echo "=== Build RuView sensing-server ==="
  if [ -d /tmp/ruview-build ]; then
    cargo build --release --manifest-path /tmp/ruview-build/v2/Cargo.toml -p wifi-densepose-sensing-server 2>&1
    cp /tmp/ruview-build/v2/target/release/sensing-server bin/sensing-server
  else
    echo "ERROR: /tmp/ruview-build not found. Clone RuView first."
    exit 1
  fi
fi

if [ -x bin/sensing-server ]; then
  echo "=== Avvio Rust sensing-server (UDP :${RUST_UDP_PORT}, WS :${RUST_WS_PORT}, HTTP :${RUST_HTTP_PORT}) ==="
  if [ "$SIMULATE" = true ]; then
    bin/sensing-server \
      --source simulate \
      --http-port "$RUST_HTTP_PORT" \
      --ws-port "$RUST_WS_PORT" \
      --ui-path ruview-ui &
  else
    bin/sensing-server \
      --source esp32 \
      --udp-port "$RUST_UDP_PORT" \
      --http-port "$RUST_HTTP_PORT" \
      --ws-port "$RUST_WS_PORT" \
      --ui-path ruview-ui &
  fi
  RUST_PID=$!
  echo "  Rust PID: $RUST_PID"
  sleep 1
else
  echo "WARNING: bin/sensing-server non trovato. Salto Rust."
  RUST_PID=""
fi

# ── Python ─────────────────────────────────────────────────────────
RELAY_ARG=()
if [ "$SIMULATE" = false ] && [ -n "$RUST_PID" ]; then
  RELAY_ARG=(--relay-port "$RUST_UDP_PORT")
  echo "=== Avvio Python ws_server (UDP :${PYTHON_UDP_PORT}, WS :${PYTHON_WS_PORT}, relay → :${RUST_UDP_PORT}) ==="
else
  echo "=== Avvio Python ws_server (UDP :${PYTHON_UDP_PORT}, WS :${PYTHON_WS_PORT}) ==="
fi

PYTHON_ARGS=(
  --udp-port "$PYTHON_UDP_PORT"
  "${RELAY_ARG[@]}"
  --ws-port "$PYTHON_WS_PORT"
  --room "6x5"
  --rx "0.5,0.5;5.5,0.5;3.0,4.5"
)

python3 -m csi.quadrants.ws_server "${PYTHON_ARGS[@]}" &
PYTHON_PID=$!
echo "  Python PID: $PYTHON_PID"

# ── Cleanup ───────────────────────────────────────────────────────
cleanup() {
  echo ""
  echo "=== Arresto ==="
  [ -n "$PYTHON_PID" ] && kill "$PYTHON_PID" 2>/dev/null && echo "  Python fermato"
  [ -n "$RUST_PID" ] && kill "$RUST_PID" 2>/dev/null && echo "  Rust fermato"
  wait
}
trap cleanup SIGINT SIGTERM

echo ""
echo "=== Browser ==="
echo "  UI Python: http://localhost:8000/radar_3d.html (via python3 -m http.server)"
echo "  UI Rust:   http://localhost:${RUST_HTTP_PORT}"
echo "  WS Python: ws://localhost:${PYTHON_WS_PORT}"
echo "  WS Rust:   ws://localhost:${RUST_WS_PORT}"
echo ""
echo "Premi Ctrl+C per fermare tutto."
echo ""

wait
