"""
ws_server.py — server unico WebSocket per presence + quadrants + (futuro) blob3d.

Sostituisce la frammentazione vista in master/feature-branch dove malios_bridge
e csi_blob_live duplicavano la logica di stream con schemi divergenti.

Schema messaggi (uno per type, broadcast a ~10 Hz):

  {"type":"hello",     "version":2}                              # all'apertura
  {"type":"presence",  "state":"EMPTY|STILL|MOTION", ...} # da PresenceReading
  {"type":"position",  "x":..,"y":..,"x_std":..,"y_std":..,...}  # da BlobEstimate
  {"type":"cells",     "rows":N,"cols":M,"probas":{"rXcY":p},
                       "predicted":"rXcY","confidence":..}       # da CellProbabilities
  {"type":"position_ml","x":..,"y":..,...,"smoothed":true,...}   # da PositionEstimate
  {"type":"diag",      "rate_hz":..,"paths_active":..,"by_rx":..,
                       "calibration_progress":..,...}            # ogni 1 s

Sources di input:
  --udp-port (default 5005)  legge radar3d / crossping / ADR-018 / testo legacy
  --inject                   debug: usa tools/inject_radar3d_frames.py inline

Strategie pipeline:
  --quadrants-mode auto|blob_live|regressor (default: auto)
      auto       : usa regressor se modello validato presente, altrimenti blob_live.
      blob_live  : forza il baseline no-ML (consigliato).
      regressor  : forza il ML (richiede modello pre-validato).

Esempio:
    python3 -m csi.quadrants.ws_server \\
        --udp-port 5005 --ws-port 8765 \\
        --room 6x5 --grid 4x4 \\
        --rx 0.5,0.5;5.5,0.5;3.0,4.5

Dipendenza: websockets (pip install websockets).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time

logger = logging.getLogger(__name__)
from collections import deque, defaultdict
from typing import Any

# Optional websockets
_HAVE_WS = False
try:
    import websockets  # type: ignore
    _HAVE_WS = True
except ImportError:
    pass

from csi.presence.detector import PresenceDetector, PresenceState
from csi.quadrants.blob_live import BlobEstimator


# ============================================================
# Argparse helpers
# ============================================================
def _parse_room(s: str) -> tuple[float, float]:
    parts = s.lower().split("x")
    if len(parts) < 2:
        raise argparse.ArgumentTypeError("--room WxL atteso (es. 6x5)")
    return float(parts[0]), float(parts[1])


def _parse_grid(s: str) -> tuple[int, int]:
    parts = s.lower().split("x")
    if len(parts) < 2:
        raise argparse.ArgumentTypeError("--grid RxC atteso (es. 4x4)")
    return int(parts[0]), int(parts[1])


def _parse_rx_positions(s: str) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for part in s.split(";"):
        part = part.strip()
        if not part:
            continue
        try:
            x, y = part.split(",")
            out.append((float(x), float(y)))
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"--rx formato non valido: '{part}' (atteso 'x,y;x,y;...')"
            )
    if not out:
        raise argparse.ArgumentTypeError("--rx vuoto")
    return out


# ============================================================
# UDP source (riusa parser)
# ============================================================
def udp_frame_loop(port: int, on_frame, stop_event: threading.Event,
                   relay_port: int = 0) -> None:
    from csi.csi_processor import (
        parse_csi_radar3d, parse_csi_crossping, parse_csi_binary, parse_csi_line,
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Buffer di ricezione UDP: 4 MB evita i drop durante i picchi di carico
    # (default macOS ~262 KB — insufficiente per 3× ESP32 a 100 Hz)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.2)
    text_buf = bytearray()
    print(f"  [ws] UDP in ascolto su :{port}", file=sys.stderr)

    # UDP relay: forward raw frames to RuView Rust sensing-server
    relay_sock = None
    if relay_port > 0 and relay_port != port:
        relay_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        print(f"  [ws] UDP relay attivo → :{relay_port}", file=sys.stderr)

    while not stop_event.is_set():
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break

        # Forward raw frame to Rust sensing-server
        if relay_sock is not None:
            try:
                relay_sock.sendto(data, ("127.0.0.1", relay_port))
            except OSError:
                pass

        if len(data) >= 4:
            magic = int.from_bytes(data[:4], "little")
            if magic == 0xC5110003:
                p = parse_csi_radar3d(data)
                if p:
                    on_frame(p); continue
            if magic == 0xC5110002:
                p = parse_csi_crossping(data)
                if p:
                    on_frame(p); continue
            if magic == 0xC5110001:
                p = parse_csi_binary(data)
                if p:
                    on_frame(p); continue

        text_buf.extend(data)
        while b"\n" in text_buf:
            line, _, text_buf = text_buf.partition(b"\n")
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            if text.startswith("CSI:"):
                p = parse_csi_line(text)
                if p:
                    on_frame(p)


# ============================================================
# Broadcaster
# ============================================================
class Broadcaster:
    """Manage ws clients + thread-safe broadcast."""

    def __init__(self):
        self._clients: set[Any] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def handler(self, ws, path=None):  # path: positional or kwarg in old websockets
        self._clients.add(ws)
        try:
            await ws.send(json.dumps({"type": "hello", "version": 2}))
            async for _ in ws:
                pass
        except Exception:
            logger.debug("WebSocket client disconnected")
        finally:
            self._clients.discard(ws)

    async def _bcast(self, msg: dict) -> None:
        if not self._clients:
            return
        payload = json.dumps(msg, separators=(",", ":"))
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    def send(self, msg: dict) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._bcast(msg), self._loop)


# ============================================================
# Server main loop
# ============================================================
def _run_ws_loop(host: str, port: int, bcast: Broadcaster,
                 stop_event: threading.Event) -> None:
    if not _HAVE_WS:
        raise RuntimeError("websockets non installato. pip install websockets")

    async def _serve():
        bcast.set_loop(asyncio.get_running_loop())
        print(f"  [ws] WebSocket server su ws://{host}:{port}", file=sys.stderr)
        async with websockets.serve(bcast.handler, host, port):
            while not stop_event.is_set():
                await asyncio.sleep(0.2)

    asyncio.run(_serve())


# ============================================================
# Main
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(
        description="WiFi Sensing — unified WebSocket server (presence + quadrants)",
    )
    ap.add_argument("--udp-port", type=int, default=5005)
    ap.add_argument("--relay-port", type=int, default=0,
                    help="Forward raw UDP frames to this port (e.g. RuView Rust sensing-server)")
    ap.add_argument("--ws-port", type=int, default=8765)
    ap.add_argument("--ws-host", type=str, default="0.0.0.0")
    ap.add_argument("--room", type=_parse_room, default=(6.0, 5.0),
                    help="Dimensioni stanza WxL in metri (default: 6x5)")
    ap.add_argument("--grid", type=_parse_grid, default=(4, 4),
                    help="Griglia celle RxC (default: 4x4)")
    ap.add_argument("--rx", type=_parse_rx_positions,
                    default=[(0.5, 0.5), (5.5, 0.5), (3.0, 4.5)],
                    help="Posizioni RX 'x,y;x,y;...' (default: 3 angoli stanza 6x5)")
    ap.add_argument("--window", type=int, default=100,
                    help="Window frames per presence + blob (default: 100 ~ 1s @ 100Hz)")
    ap.add_argument("--baseline-seconds", type=float, default=30.0,
                    help="Calibrazione baseline per presence (default: 30s)")
ap.add_argument("--empty-mult", type=float, default=1.5,
                    help="Soglia EMPTY/STILL = baseline*N (default: 1.5; abbassa se rimane EMPTY)")
ap.add_argument("--move-mult", type=float, default=3.0,
                    help="Soglia STILL/MOTION = baseline*N (default: 3.0; abbassa se non vedi MOTION)")
    ap.add_argument("--min-intensity", type=float, default=1e-3,
                    help="Soglia minima varianza per emettere blob (default: 0.001)")
    ap.add_argument("--variance-power", type=float, default=1.0,
                    help="Power per i pesi varianza (default 1.0). Alzalo a 2.0-3.0 "
                         "se il blob resta bloccato al centro (amplifica differenze tra RX).")
    ap.add_argument("--blob-baseline-seconds", type=float, default=0.0,
                    help="Calibrazione baseline per BlobEstimator (default 0 = off). "
                         "Imposta a 20-30s per sottrarre il rumore ambient per-RX.")
    ap.add_argument("--broadcast-hz", type=float, default=10.0,
                    help="Refresh rate broadcast (default: 10 Hz)")
    ap.add_argument("--quadrants-mode", choices=["auto", "blob_live", "regressor"],
                    default="blob_live",
                    help="Strategia quadranti (default: blob_live = NO ML).")
    ap.add_argument("--enable-3d", action="store_true",
                    help="Abilita Blob3DTracker (z best-effort macro-classi). "
                         "Richiede numpy. Vedi limiti in csi/blob3d/tracker.py.")
    ap.add_argument("--room-height", type=float, default=3.0,
                    help="Altezza stanza in metri (per --enable-3d, default 3.0)")
    ap.add_argument("--quiet", action="store_true",
                    help="Sopprimi log per-frame")
    args = ap.parse_args()

    if not _HAVE_WS:
        print("ERROR: 'websockets' non installato. pip install websockets", file=sys.stderr)
        return 1

    # ---- Build pipeline components ----
    presence = PresenceDetector(
        window_size=args.window,
        baseline_seconds=args.baseline_seconds,
        empty_mult=args.empty_mult,
        move_mult=args.move_mult,
    )

    blob = BlobEstimator(
        rx_positions=args.rx,
        room_size=args.room,
        grid_shape=args.grid,
        window_frames=args.window,
        min_intensity=args.min_intensity,
        variance_power=args.variance_power,
        baseline_seconds=args.blob_baseline_seconds,
        baseline_alpha=0.3 if args.blob_baseline_seconds > 0 else 0.0,
    )

    # 3D tracker (optional, opt-in)
    tracker_3d = None
    if args.enable_3d:
        try:
            from csi.blob3d.tracker import Blob3DTracker
            tracker_3d = Blob3DTracker(
                room_size=(args.room[0], args.room[1], args.room_height),
            )
            print(f"  [ws] Blob3DTracker abilitato (z best-effort, macro-classi)",
                  file=sys.stderr)
        except Exception as e:
            print(f"  [ws] Blob3DTracker non disponibile ({e}): 3D disabilitato",
                  file=sys.stderr)

    # Regressor (optional, loaded lazy)
    regressor = None
    if args.quadrants_mode in ("auto", "regressor"):
        try:
            from csi.quadrants.regressor import PositionRegressor
            reg = PositionRegressor(window_frames=30)
            if reg.load():
                regressor = reg
                print(f"  [ws] PositionRegressor caricato (validato LOO-cell)",
                      file=sys.stderr)
            elif args.quadrants_mode == "regressor":
                print("  [ws] ERROR: --quadrants-mode=regressor ma modello non caricabile",
                      file=sys.stderr)
                return 1
            else:
                print("  [ws] Modello regressor non disponibile/non validato: "
                      "uso blob_live", file=sys.stderr)
        except Exception as e:
            print(f"  [ws] Regressor non disponibile ({e}): uso blob_live",
                  file=sys.stderr)

    # ---- State + broadcaster ----
    bcast = Broadcaster()
    stop_event = threading.Event()

    # Rate stats: contatore + finestra di 1 secondo (NON EMA inter-frame)
    # NB: il vecchio metodo `1.0 / dt` esplodeva quando i frame arrivavano
    # in burst (microsecondi di distanza) — dava 3000+ fps spuri.
    rate_counter = [0]  # frame nell'attuale finestra di 1s (mutable per closure)
    rate_window_start = [time.time()]
    last_observed_rate = [0.0]

    # Per-(tx,rx) counters per il diag (rolling 1s)
    path_counter: dict[tuple[int, int], int] = defaultdict(int)
    last_path_counts: dict[tuple[int, int], int] = {}

    paths_seen: set[tuple[int, int]] = set()

    def on_frame(frame: dict[str, Any]) -> None:
        rate_counter[0] += 1

        tx = int(frame.get("tx_node", 0))
        rx = int(frame.get("rx_node", 0))
        paths_seen.add((tx, rx))
        path_counter[(tx, rx)] += 1

        presence.add_frame(frame)
        blob.add_frame(frame)
        if regressor is not None:
            regressor.add_frame(frame)
        if tracker_3d is not None:
            tracker_3d.add_frame(frame)

    # ---- Start WS server in background thread ----
    ws_thread = threading.Thread(
        target=_run_ws_loop,
        args=(args.ws_host, args.ws_port, bcast, stop_event),
        daemon=True,
    )
    ws_thread.start()
    time.sleep(0.3)  # let WS server bind before clients connect

    # ---- Start UDP receiver in background thread ----
    relay_port = getattr(args, "relay_port", 0)
    udp_thread = threading.Thread(
        target=udp_frame_loop,
        args=(args.udp_port, on_frame, stop_event, relay_port),
        daemon=True,
    )
    udp_thread.start()

    def cleanup(*_):
        stop_event.set()
        time.sleep(0.3)
        print("\n  [ws] Bye.", file=sys.stderr)
        sys.exit(0)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # ---- Broadcast loop ----
    interval = 1.0 / max(args.broadcast_hz, 1.0)
    last_diag = 0.0
    try:
        while not stop_event.is_set():
            t0 = time.time()

            # presence
            pres = presence.current_reading()
            msg_pres = {"type": "presence", **pres.to_dict()}
            bcast.send(msg_pres)

            # blob position (no-ML)
            est = blob.estimate()
            if est is not None:
                bcast.send({"type": "position", **est.to_dict()})
                cells = blob.cell_probabilities(est)
                if cells is not None:
                    bcast.send({"type": "cells", **cells.to_dict()})
                # Feed 2D into 3D tracker (se attivo)
                if tracker_3d is not None:
                    tracker_3d.update_position(
                        x=est.x, y=est.y, x_std=est.x_std, y_std=est.y_std, t=est.t,
                    )

            # regressor (opt-in)
            if regressor is not None and regressor.ready:
                p = regressor.predict()
                if p is not None:
                    # Mappa le coordinate normalizzate [0,1] a metri della stanza
                    px_m = p.x * args.room[0]
                    py_m = p.y * args.room[1]
                    bcast.send({
                        "type": "position_ml",
                        "x": round(px_m, 4),
                        "y": round(py_m, 4),
                        "x_std": round(p.x_std * args.room[0], 4),
                        "y_std": round(p.y_std * args.room[1], 4),
                        "smoothed": p.smoothed,
                        "confidence": round(p.confidence, 3),
                        "t": round(p.t, 3),
                    })
                    # Se 3D attivo e regressor è migliore di blob, usa lui
                    if tracker_3d is not None and p.confidence > 0.6:
                        tracker_3d.update_position(
                            x=px_m, y=py_m,
                            x_std=p.x_std * args.room[0],
                            y_std=p.y_std * args.room[1],
                            t=p.t,
                        )

            # 3D tracker broadcast
            if tracker_3d is not None:
                e3d = tracker_3d.current()
                if e3d is not None:
                    bcast.send({"type": "position_3d", **e3d.to_dict()})

            # diag ogni secondo — usa counter per fps reale (non EMA inter-frame)
            if t0 - last_diag >= 1.0:
                window = t0 - rate_window_start[0]
                rate_hz = rate_counter[0] / max(window, 1e-6)
                last_observed_rate[0] = rate_hz
                # snapshot per-path nell'ultimo intervallo
                fps_per_path = {
                    f"{tx},{rx}": round(c / max(window, 1e-6), 1)
                    for (tx, rx), c in path_counter.items()
                }
                # reset counter per finestra successiva
                rate_counter[0] = 0
                rate_window_start[0] = t0
                last_path_counts = dict(path_counter)
                path_counter.clear()

                by_rx: dict[int, int] = {}
                by_tx: dict[int, int] = {}
                for tx, rx in paths_seen:
                    by_rx[rx] = by_rx.get(rx, 0) + 1
                    by_tx[tx] = by_tx.get(tx, 0) + 1

                # warn se il blob è bloccato al centro geometrico dei RX
                rx_centroid_x = sum(p[0] for p in args.rx) / len(args.rx)
                rx_centroid_y = sum(p[1] for p in args.rx) / len(args.rx)
                blob_stuck_at_center = False
                if est is not None:
                    if (abs(est.x - rx_centroid_x) < 0.3 and
                            abs(est.y - rx_centroid_y) < 0.3 and
                            (est.x_std < 0.5 or est.y_std < 0.5)):
                        blob_stuck_at_center = True

                bcast.send({
                    "type": "diag",
                    "rate_hz": round(rate_hz, 1),
                    "paths_active": len(paths_seen),
                    "paths_expected": len(args.rx) * len(args.rx),
                    "by_rx": {str(k): v for k, v in by_rx.items()},
                    "by_tx": {str(k): v for k, v in by_tx.items()},
                    "fps_per_path": fps_per_path,
                    "blob_stuck_at_center": blob_stuck_at_center,
                    "calibration_progress": round(
                        presence.current_reading().calibration_progress, 3),
                    "regressor_loaded": regressor is not None,
                    "tracker_3d_enabled": tracker_3d is not None,
                    "t": round(t0, 3),
                })
                if not args.quiet:
                    warn = ""
                    expected_paths = len(args.rx) * len(args.rx)
                    if len(paths_seen) < expected_paths:
                        missing_tx = [t for t in range(len(args.rx)) if t not in by_tx]
                        missing_rx = [r for r in range(len(args.rx)) if r not in by_rx]
                        if missing_tx:
                            warn += f"  ⚠ TX mancanti: {missing_tx}"
                        if missing_rx:
                            warn += f"  ⚠ RX mancanti: {missing_rx}"
                    if blob_stuck_at_center:
                        warn += "  ⚠ blob fermo al centro geometrico"
                    print(
                        f"  [ws] {rate_hz:6.1f} fps  paths={len(paths_seen):>2}/{expected_paths}  "
                        f"state={pres.state.value:<10}  "
                        f"blob={'-' if est is None else f'({est.x:.1f},{est.y:.1f})'}"
                        f"{warn}",
                        file=sys.stderr,
                    )
                last_diag = t0

            elapsed = time.time() - t0
            sleep_t = max(0.0, interval - elapsed)
            time.sleep(sleep_t)
    except KeyboardInterrupt:
        cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
