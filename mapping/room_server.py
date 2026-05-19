#!/usr/bin/env python3
"""
room_server.py — WebSocket bridge da ESP32 → mappa stanza live.

Collega il flusso CSI dall'ESP32 (via USB) al PositionEstimator e
trasmette coordinate live via WebSocket a room_map.html.

Avvio:
    python3 room_server.py --fingerprint fingerprint.json    # con HW ESP32
    python3 room_server.py --simulate fingerprint.json       # senza HW, dati sintetici

Dipendenze opzionali:
    pip install websockets   (per la UI browser, altrimenti solo log file)
    pip install pyserial     (per leggere da ESP32 USB)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import threading
import time
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Optional

# Dipendenze opzionali
_WS_AVAILABLE = False
try:
    import asyncio
    import websockets
    _WS_AVAILABLE = True
except ImportError:
    asyncio = None  # type: ignore
    websockets = None  # type: ignore

try:
    import serial
except ImportError:
    serial = None  # type: ignore

# Progetto
from .room_mapper import PositionEstimator, FingerprintMap

from ..csi.csi_processor import parse_csi_line

DEFAULT_BAUD = 921600
DEFAULT_PORT = 8765
POSITION_FILE = "position.json"


# ============================================================
# Position Tracker: accumula RSSI da 3 AP e stima posizione
# ============================================================

class PositionTracker:
    """
    Accumula frame CSI, raggruppa per AP, e periodicamente
    stima la posizione via k-NN.

    Parameters
    ----------
    estimator : PositionEstimator
    window_frames : int
        Quanti frame per AP tenere (default 5).
    """

    def __init__(self, estimator: PositionEstimator, window_frames: int = 5):
        self.estimator = estimator
        self.window = window_frames
        # Buffer RSSI per AP
        self._rssi: dict[int, list[float]] = {0: [], 1: [], 2: []}

    def add_frame(self, frame: dict):
        """Aggiunge frame CSI, estrae RSSI per AP."""
        ap_id = frame.get("ap_id", 0)
        rssi = frame.get("rssi")
        if ap_id not in self._rssi or rssi is None:
            return
        buf = self._rssi[ap_id]
        buf.append(float(rssi))
        if len(buf) > self.window:
            buf.pop(0)

    @property
    def ready(self) -> bool:
        """Vero se ogni AP ha almeno un campione RSSI."""
        return all(len(buf) > 0 for buf in self._rssi.values())

    def estimate(self) -> dict:
        """Stima posizione dal RSSI medio di ogni AP."""
        if not self.ready:
            return {"x": 0, "y": 0, "confidence": 0.0, "error": "dati insufficienti"}
        # Media RSSI per AP
        rssi_vec = [
            round(sum(buf) / len(buf), 1) for buf in self._rssi.values()
        ]
        return self.estimator.estimate(rssi_vec)


# ============================================================
# Simulatore (nessun HW richiesto)
# ============================================================

class Simulator:
    """Genera RSSI sintetici che si muovono nella stanza."""

    def __init__(self, fmap: FingerprintMap):
        self._points = fmap.points
        self._idx = 0
        self._t = 0.0
        # Sceglie 2 punti estremi per oscillare
        if len(self._points) >= 2:
            self._p0 = self._points[0]
            self._p1 = self._points[-1]
        else:
            self._p0 = self._points[0] if self._points else None
            self._p1 = None

    def next_rssi(self) -> list[float]:
        """Genera RSSI interpolato tra due punti con rumore."""
        self._t += 0.05
        if self._p1 is None or self._p0 is None:
            return [random.gauss(-50, 5) for _ in range(3)]

        # Oscilla tra p0 e p1
        frac = (math.sin(self._t) + 1) / 2  # 0..1
        rssi = [
            self._p0.rssi[i] + (self._p1.rssi[i] - self._p0.rssi[i]) * frac
            + random.gauss(0, 2)
            for i in range(3)
        ]
        return [round(v, 1) for v in rssi]


# ============================================================
# File writer (fallback senza WebSocket)
# ============================================================

def write_position(result: dict, path: str = POSITION_FILE):
    """Scrive posizione su file JSON (polled da room_map.html)."""
    result["_timestamp"] = time.time()
    with open(path, "w") as f:
        json.dump(result, f, indent=2)


# ============================================================
# WebSocket server (live)
# ============================================================

async def ws_handler(websocket):
    """Handler per connessione WebSocket."""
    async for message in websocket:
        # Il client puo' mandare 'ping' o comandi
        if message == "ping":
            await websocket.send(json.dumps({"type": "pong"}))


async def ws_broadcast(server, data: dict):
    """Invia dizionario JSON a tutti i client connessi."""
    if not server or not hasattr(server, "websockets"):
        return
    msg = json.dumps(data)
    websockets_to_remove = []
    for ws in server.websockets:
        try:
            await ws.send(msg)
        except websockets.exceptions.ConnectionClosed:
            websockets_to_remove.append(ws)
    for ws in websockets_to_remove:
        server.websockets.remove(ws)


# ============================================================
# HTTP server statico (per room_map.html + file JSON)
# ============================================================

def _start_http_server(http_port: int, directory: str):
    """Avvia un HTTP server in un thread separato."""
    class _Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

        def log_message(self, fmt, *args):
            # Silenzioso
            pass

    server = HTTPServer(("0.0.0.0", http_port), _Handler)
    print(f"  HTTP:      http://localhost:{http_port}/room_map.html")
    server.serve_forever()


async def run_server(args):
    """Loop principale asincrono."""
    # Avvia HTTP server per file statici
    http_dir = os.path.dirname(os.path.abspath(__file__))
    http_thread = threading.Thread(
        target=_start_http_server,
        args=(args.http_port, http_dir),
        daemon=True,
    )
    http_thread.start()

    # Carica fingerprint
    estimator = PositionEstimator(k=args.k)
    estimator.load(args.fingerprint)
    fmap = estimator._fmap
    assert fmap is not None

    tracker = PositionTracker(estimator, window_frames=args.window)

    # Simulatore o HW
    simulator = Simulator(fmap) if args.simulate else None
    ser = None

    if not args.simulate:
        if serial is None:
            print("  pyserial non installato. Usa --simulate per test.")
            sys.exit(1)
        port = args.port or _autodetect_port()
        if not port:
            print("  Porta seriale non trovata. Usa --port o --simulate.")
            sys.exit(1)
        try:
            ser = serial.Serial(port, args.baud, timeout=1)
            print(f"  Seriale: {port} @ {args.baud}")
        except serial.SerialException as e:
            print(f"  ERRORE apertura seriale: {e}")
            sys.exit(1)
    else:
        print(f"  MODALITA' SIMULAZIONE (nessun HW)")

    # WebSocket server
    ws_server = None
    if _WS_AVAILABLE:
        ws_server = await websockets.serve(
            ws_handler, "0.0.0.0", args.ws_port,
            ping_interval=30, ping_timeout=10,
        )
        print(f"  WebSocket: ws://localhost:{args.ws_port}")
    print(f"  Position file: {POSITION_FILE}")
    print(f"  Premi Ctrl+C per uscire\n")

    # Stima periodica
    last_estimate = 0.0
    estimate_interval = 1.0 / max(args.rate, 0.1)  # Hz → secondi

    try:
        while True:
            # Leggi frame
            if args.simulate:
                # Dati sintetici
                rssi_vec = simulator.next_rssi()
                # Simula frame per ogni AP
                for ap_id in range(3):
                    frame = {
                        "ap_id": ap_id,
                        "rssi": rssi_vec[ap_id],
                        "seq": 0,
                        "noise_floor": -90,
                    }
                    tracker.add_frame(frame)
                await asyncio.sleep(0.1)
            else:
                assert ser is not None
                line = ser.readline().decode("utf-8", errors="replace").strip()
                if not line:
                    await asyncio.sleep(0.01)
                    continue
                parsed = parse_csi_line(line)
                if parsed:
                    tracker.add_frame(parsed)

            # Stima periodica
            now = time.time()
            if now - last_estimate >= estimate_interval and tracker.ready:
                last_estimate = now
                result = tracker.estimate()

                # File fallback
                write_position(result, args.position_file)

                # WebSocket broadcast
                if ws_server:
                    await ws_broadcast(ws_server, {
                        "type": "position",
                        **result,
                        "room": fmap.room,
                    })

    except asyncio.CancelledError:
        pass
    finally:
        if ser:
            ser.close()
        if ws_server:
            ws_server.close()


# ============================================================
# Autodetect porta seriale (copiato da csi_mac.py)
# ============================================================

def _autodetect_port() -> Optional[str]:
    import glob
    cands: list[str] = []
    cands += sorted(glob.glob("/dev/cu.ESP32_CSI*"))
    cands += sorted(glob.glob("/dev/cu.ESP32*Bluetooth*"))
    cands += sorted(glob.glob("/dev/cu.usbserial*"))
    cands += sorted(glob.glob("/dev/cu.SLAB_USBtoUART*"))
    cands += sorted(glob.glob("/dev/cu.wchusbserial*"))
    cands += sorted(glob.glob("/dev/ttyUSB*"))
    cands += sorted(glob.glob("/dev/ttyACM*"))
    seen: set[str] = set()
    return next((c for c in cands if not (c in seen or seen.add(c))), None)


# ============================================================
# CLI simulate mode (senza asyncio)
# ============================================================

def run_simulate(args):
    """Modalita' simulazione sincrona — scrive position.json."""
    print("\n=== Room Server — Simulazione ===")
    estimator = PositionEstimator(k=args.k)
    estimator.load(args.fingerprint)
    fmap = estimator._fmap
    assert fmap is not None

    sim = Simulator(fmap)
    print(f"  Stanza: {fmap.room['name']} "
          f"({fmap.room['width']}x{fmap.room['height']}m)")
    print(f"  Punti calibrazione: {fmap.n_points}")
    print(f"  Scrivo: {args.position_file}\n")

    try:
        while True:
            rssi = sim.next_rssi()
            result = estimator.estimate(rssi)
            if "error" not in result:
                write_position(result, args.position_file)
                sys.stdout.write(
                    f"\r  Pos: ({result['x']:>5.1f}, {result['y']:>5.1f}) "
                    f"conf={result['confidence']:.2f}  "
                    f"label={result.get('nearest_label', '-'):>10}"
                )
                sys.stdout.flush()
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n  Fermato.")


# ============================================================
# Main
# ============================================================

def main():
    ap = argparse.ArgumentParser(description="Room Server — WiFi mapping")
    ap.add_argument("--fingerprint", default="fingerprint.json",
                    help="Path fingerprint JSON (default: fingerprint.json)")
    ap.add_argument("--k", type=int, default=3,
                    help="k-NN neighbors (default 3)")
    ap.add_argument("--window", type=int, default=5,
                    help="Frame per AP per media RSSI (default 5)")
    ap.add_argument("--rate", type=float, default=2.0,
                    help="Stime al secondo (default 2 Hz)")
    ap.add_argument("--ws-port", type=int, default=DEFAULT_PORT,
                    help="Porta WebSocket (default 8765)")
    ap.add_argument("--http-port", type=int, default=8080,
                    help="Porta HTTP file statici (default 8080)")
    ap.add_argument("--position-file", default=POSITION_FILE,
                    help="File JSON posizione (default: position.json)")

    # Seriale
    ap.add_argument("--port", help="Porta seriale ESP32 (default: autodetect)")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)

    # Modalita'
    ap.add_argument("--simulate", action="store_true",
                    help="Simula senza HW ESP32")

    args = ap.parse_args()

    if not os.path.exists(args.fingerprint):
        print(f"  ERRORE: fingerprint {args.fingerprint} non trovato.")
        print(f"  Crea con:  python3 room_mapper.py calibrate {args.fingerprint}")
        sys.exit(1)

    if args.simulate:
        run_simulate(args)
    elif _WS_AVAILABLE:
        asyncio.run(run_server(args))
    else:
        print("  websockets non installato. Usa --simulate per test.")
        print("  Oppure: pip install websockets")
        sys.exit(1)


if __name__ == "__main__":
    main()
