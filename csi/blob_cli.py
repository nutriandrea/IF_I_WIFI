#!/usr/bin/env python3
"""
blob_cli.py — CLI per training e monitoring del blob regressor.

Modalita':
    --train      Raccolta punti calibrazione (x,y in metri) + addestra
    --monitor    Carica modello, inferenza continua, broadcast WebSocket

Esempi:
    # Training con punti definiti da terminale
    python3 -m csi.blob_cli --train --udp-port 5005

    # Monitor real-time
    python3 -m csi.blob_cli --monitor --udp-port 5005 --ws-port 8765

Formato messaggio WebSocket (verso radar_3d.html):
    {
      "type": "blob",
      "t": 12.345,
      "x_raw": 2.31, "y_raw": 3.42,
      "x": 2.30, "y": 3.40,
      "vx": 0.05, "vy": -0.02,
      "speed": 0.054,
      "motion": false,
      "confidence": 0.87
    }
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import socket
import sys
import threading
import time

logger = logging.getLogger(__name__)
from collections import Counter
from typing import Iterator

from .csi_processor import (
    parse_csi_binary,
    parse_csi_line,
    parse_csi_radar3d,
    CSI_BINARY_MAGIC,
    CSI_RADAR3D_MAGIC,
)
from .quadrants.regressor import PositionRegressor, PositionEstimate
from .quadrants.regressor import DEFAULT_MODEL_PATH as BLOB_MODEL_PATH

# Optional dependencies
_HAVE_WS = False
try:
    import websockets
    _HAVE_WS = True
except ImportError:
    pass


# ============================================================
# UDP source — riusa la stessa logica di csi_mac
# ============================================================
def udp_lines(port: int) -> Iterator[tuple[str | dict, str | None]]:
    """Genera frame parsati dall'UDP. Yields dict (binario) o linea testo.

    Supporta:
      - magic 0xC5110001 (ADR-018, vecchio firmware esp32_csi_firmware)
      - magic 0xC5110003 (Radar3D cross-ping, firmware esp32_radar3d)
      - testo "CSI:..." (Serial-mode legacy)
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.5)
    text_buf = bytearray()
    print(f"  UDP in ascolto su 0.0.0.0:{port}", file=sys.stderr)

    while True:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        if len(data) >= 4:
            magic = int.from_bytes(data[:4], "little")
            if magic == CSI_RADAR3D_MAGIC:
                parsed = parse_csi_radar3d(data)
                if parsed:
                    yield (parsed, None)
                    continue
            elif magic == CSI_BINARY_MAGIC:
                parsed = parse_csi_binary(data)
                if parsed:
                    yield (parsed, None)
                    continue
        text_buf.extend(data)
        while b"\n" in text_buf:
            line, _, text_buf = text_buf.partition(b"\n")
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            if text.startswith("CSI:"):
                p = parse_csi_line(text)
                if p:
                    yield (p, None)


# ============================================================
# WebSocket broadcaster
# ============================================================
_ws_loop: asyncio.AbstractEventLoop | None = None
_ws_clients: set = set()


async def _ws_handler(ws, path=None):
    _ws_clients.add(ws)
    try:
        async for _ in ws:
            pass
    except Exception:
        logger.debug("WebSocket client disconnected")
    finally:
        _ws_clients.discard(ws)


async def _ws_broadcast(msg: dict):
    if not _ws_clients:
        return
    payload = json.dumps(msg)
    dead = []
    for ws in _ws_clients.copy():
        try:
            await ws.send(payload)
        except Exception:
            dead.append(ws)
    for w in dead:
        _ws_clients.discard(w)


def ws_send(msg: dict):
    """Thread-safe send (blob_cli main runs in main thread)."""
    if _ws_loop is None or not _ws_clients:
        return
    asyncio.run_coroutine_threadsafe(_ws_broadcast(msg), _ws_loop)


def _ws_server_thread(port: int):
    """Thread con un event loop dedicato che ospita il server WebSocket."""
    global _ws_loop
    _ws_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_ws_loop)

    async def _start():
        if not _HAVE_WS:
            print("  websockets non installato", file=sys.stderr)
            return
        async with websockets.serve(_ws_handler, "0.0.0.0", port):
            print(f"  WebSocket in ascolto su 0.0.0.0:{port}", file=sys.stderr)
            await asyncio.Future()  # keep alive

    try:
        _ws_loop.run_until_complete(_start())
    except Exception as e:
        print(f"  ERR WS server: {e}", file=sys.stderr)


# ============================================================
# TRAINING mode
# ============================================================
def cmd_train(args) -> int:
    """Raccoglie N punti di calibrazione interattivamente e addestra."""
    blob = PositionRegressor(window_frames=args.window)

    print("\n  === POSITION REGRESSOR TRAINING ===")
    print(f"  Window frames: {args.window}")
    print(f"  Secondi per punto: {args.seconds}")
    print(f"  Punti da raccogliere: {args.num_points}")
    print(f"\n  Per ogni punto: ti dico (x,y) in metri (origine = un angolo).")
    print(f"  Vai in quel punto, fermati, premo INVIO, raccolgo {args.seconds}s.")

    samples: dict[tuple[float, float], list] = {}

    # Suggerisco punti di default a copertura griglia, ma puoi sovrascrivere
    if args.points:
        # Formato CLI: --points "0.5,0.5;2.0,0.5;1.25,2.5;0.5,5.5;2.0,5.5"
        try:
            points = []
            for p in args.points.split(";"):
                x, y = p.split(",")
                points.append((float(x), float(y)))
        except Exception:
            print(f"  ERRORE: formato --points non valido")
            return 1
    else:
        # Default: 5 punti coprenti la stanza 2.5x6m suggerita
        points = [
            (0.5, 0.5),    # angolo SW
            (2.0, 0.5),    # angolo SE
            (1.25, 3.0),   # centro
            (0.5, 5.5),    # angolo NW
            (2.0, 5.5),    # angolo NE
        ][:args.num_points]

    print(f"\n  Punti che raccoglierai: {points}\n")

    src = udp_lines(args.udp_port)

    # 1. Vuoto baseline (opzionale, lo ignoriamo per il regressore — il
    #    regressore non ha classe "vuoto", ha solo coordinate. Saltiamo.)

    for idx, (x_target, y_target) in enumerate(points):
        print(f"\n  === Punto {idx + 1}/{len(points)}: ({x_target}, {y_target}) m ===")
        print(f"  Vai in posizione, fermati. Premi INVIO per iniziare.")
        try:
            input()
        except EOFError:
            return 1

        print(f"  Raccolgo {args.seconds}s...", flush=True)
        frames: list = []
        t0 = time.time()
        while time.time() - t0 < args.seconds:
            try:
                frame, _ = next(src)
                if isinstance(frame, dict):
                    frames.append(frame)
            except StopIteration:
                break
            elapsed = time.time() - t0
            if int(elapsed) != int(elapsed - 0.1) and elapsed > 0.5:
                rate = len(frames) / elapsed if elapsed > 0 else 0
                print(f"\r    {elapsed:>4.1f}s — {len(frames):>5d} frame "
                      f"({rate:>5.1f} Hz)", end="", flush=True)
        print(f"\n    Totale: {len(frames)} frame raccolti", flush=True)

        if len(frames) < args.window:
            print(f"    ATTENZIONE: troppi pochi frame ({len(frames)} < "
                  f"{args.window}). Skip.")
            continue

        samples[(x_target, y_target)] = frames

    if len(samples) < 3:
        print("\n  ERRORE: raccolti meno di 3 punti validi. Abort.")
        return 1

    print(f"\n  Inizio training su {len(samples)} punti...")
    metrics = blob.train_continuous(samples)

    print(f"\n  === RISULTATI ===")
    for k, v in metrics.items():
        if k != "known_sources":
            print(f"  {k}: {v}")

    blob.save()
    print(f"\n  ✓ Modello salvato in {BLOB_MODEL_PATH}")
    return 0


# ============================================================
# MONITOR mode
# ============================================================
def cmd_monitor(args) -> int:
    blob = PositionRegressor(
        window_frames=args.window,
        smooth_q_pos=args.kalman_q_pos,
        smooth_q_vel=args.kalman_q_vel,
        motion_threshold_mps=args.motion_thresh,
        motion_sustain_n=args.motion_sustain,
    )
    if not blob.load():
        print(f"  ERRORE: modello non trovato in {BLOB_MODEL_PATH}")
        print(f"  Esegui prima: python3 -m csi.blob_cli --train")
        return 1

    # Start WebSocket server in background
    if args.ws_port and _HAVE_WS:
        t = threading.Thread(target=_ws_server_thread,
                             args=(args.ws_port,), daemon=True)
        t.start()
        time.sleep(0.3)
    elif args.ws_port:
        print(f"  ATTENZIONE: --ws-port specificato ma websockets non installato")

    print(f"\n  === BLOB MONITOR ===")
    print(f"  {'t(s)':>6} {'x_raw':>6} {'y_raw':>6} {'x':>6} {'y':>6} "
          f"{'speed':>6}  Stato")
    print("  " + "-" * 56)

    src = udp_lines(args.udp_port)
    t0 = time.time()
    frame_count = 0
    last_print = 0.0
    last_motion = False
    src_counter: Counter = Counter()

    stop = {"v": False}

    def handle_sig(*_):
        stop["v"] = True
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    try:
        for frame, _ in src:
            if stop["v"]:
                break
            if not isinstance(frame, dict):
                continue
            blob.add_frame(frame)
            frame_count += 1
            mac = frame.get("mac", "?")
            src_counter[mac] += 1

            # Inferenza ogni ~5 frame nuovi
            if frame_count % 5 != 0:
                continue
            if not blob.ready:
                continue

            est = blob.predict()
            if est is None:
                continue

            now = time.time() - t0
            motion_str = "MOVIMENTO" if est.motion else "fermo"
            transition = "" if est.motion == last_motion else " <<<"
            last_motion = est.motion

            if now - last_print >= 0.3:
                last_print = now
                print(f"  {now:>6.1f} "
                      f"{est.x:>6.2f} {est.y:>6.2f} "
                      f"{est.x:>6.2f} {est.y:>6.2f} "
                      f"{est.speed:>6.3f}  "
                      f"{motion_str}{transition}",
                      flush=True)

            ws_send({
                "type": "blob",
                "t": round(now, 3),
                "x_raw": round(est.x, 3),
                "y_raw": round(est.y, 3),
                "x": round(est.x, 3),
                "y": round(est.y, 3),
                "speed": round(est.speed, 3),
                "motion": est.motion,
                "confidence": round(est.confidence, 3),
            })

        print(f"\n  Terminato. {frame_count} frame totali. "
              f"Top sorgenti:")
        for src_mac, cnt in src_counter.most_common(5):
            disp = src_mac if len(src_mac) <= 17 else f"...{src_mac[-8:]}"
            print(f"    {disp}: {cnt} frame")
    except Exception as e:
        print(f"  ERR: {e}", file=sys.stderr)
        return 1
    return 0


# ============================================================
# Main
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Blob regressor: training e monitor real-time")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--train", action="store_true",
                      help="Modalita' training (raccolta punti + fit)")
    mode.add_argument("--monitor", action="store_true",
                      help="Modalita' monitor (predizione live + WebSocket)")

    ap.add_argument("--udp-port", type=int, default=5005,
                    help="Porta UDP per ricevere frame ESP32")
    ap.add_argument("--ws-port", type=int, default=None,
                    help="Porta WebSocket broadcast (solo --monitor)")
    ap.add_argument("--window", type=int, default=30,
                    help="Frame per finestra inferenza")
    ap.add_argument("--seconds", type=int, default=30,
                    help="Secondi per ogni punto in training")
    ap.add_argument("--num-points", type=int, default=5,
                    help="Punti di default se non passi --points")
    ap.add_argument("--points", type=str, default=None,
                    help='Punti custom "x1,y1;x2,y2;..." in metri')

    # Kalman params (solo monitor)
    ap.add_argument("--kalman-q-pos", type=float, default=0.05)
    ap.add_argument("--kalman-q-vel", type=float, default=0.2)
    ap.add_argument("--kalman-r-meas", type=float, default=0.25)
    ap.add_argument("--motion-thresh", type=float, default=0.15,
                    help="Soglia velocita' per fermo/movimento (m/s)")
    ap.add_argument("--motion-sustain", type=int, default=3,
                    help="Frame consecutivi sopra/sotto soglia per transizione")

    args = ap.parse_args()

    if args.train:
        return cmd_train(args)
    if args.monitor:
        return cmd_monitor(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
