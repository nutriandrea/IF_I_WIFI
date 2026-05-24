#!/usr/bin/env python3
"""
inject_radar3d_frames.py — Inietta frame CSI radar3d (0xC5110003) via UDP
=======================================================================

SIMULA 3 ESP32 con cross-ping per testare la pipeline SENZA hardware reale.
Invia 9 percorsi TX→RX con timestamp progressivi.

Usage:
    # Invia frame con pattern casuali (default)
    python3 tools/inject_radar3d_frames.py

    # Invia verso porta diversa
    python3 tools/inject_radar3d_frames.py --port 5005

    # Numero frame per burst (9 percorsi per burst)
    python3 tools/inject_radar3d_frames.py --burst 100

    # Invia su un percorso specifico invece di tutti e 9
    python3 tools/inject_radar3d_frames.py --tx 0 --rx 1

    # Aggiungi pattern progressivo per simulare movimento
    python3 tools/inject_radar3d_frames.py --moving
"""

import argparse
import socket
import struct
import time
import math
import random

MAGIC_RADAR3D = 0xC5110003
HEADER_SIZE = 24
N_SUBCARRIER = 64


def build_frame(tx_node: int, rx_node: int, seq: int,
                rssi: int = -45, noise: int = -90,
                timestamp_us: int = 0,
                pattern: str = "random",
                phase_offset: float = 0.0) -> bytes:
    """Costruisce un frame radar3d finto (64 subcarrier, 2 byte I/Q).

    pattern:
        'random'  — I/Q casuali (-128..127)
        'sine'    — I=sin(freq*i), Q=cos(freq*i) (più realistico)
        'flat'    — tutte le subcarrier uguali (test baseline)
    """
    buf = bytearray()
    # Header (24 byte)
    buf += struct.pack("<I", MAGIC_RADAR3D)           # magic
    buf += bytes([tx_node, rx_node])                  # tx, rx
    buf += struct.pack("<H", N_SUBCARRIER)            # n_sub
    buf += struct.pack("<I", seq)                     # seq
    buf += struct.pack("<bbH", rssi, noise, 0)        # rssi, noise, reserved
    buf += struct.pack("<q", timestamp_us)            # timestamp_us

    # I/Q pairs (2 byte per subcarrier)
    for i in range(N_SUBCARRIER):
        if pattern == "random":
            i_val = random.randint(-128, 127)
            q_val = random.randint(-128, 127)
        elif pattern == "sine":
            freq = 2.0 * math.pi * 4.0 / N_SUBCARRIER
            i_val = int(80 * math.sin(freq * i + phase_offset))
            q_val = int(80 * math.cos(freq * i + phase_offset))
        elif pattern == "flat":
            i_val = 50
            q_val = 50
        else:
            i_val = 0
            q_val = 0
        buf += struct.pack("<bb",
                           max(-128, min(127, i_val)),
                           max(-128, min(127, q_val)))
    return bytes(buf)


def main():
    ap = argparse.ArgumentParser(description="Inietta frame radar3d via UDP")
    ap.add_argument("--port", type=int, default=5005,
                    help="Porta UDP target (default 5005)")
    ap.add_argument("--ip", type=str, default="127.0.0.1",
                    help="IP target (default 127.0.0.1)")
    ap.add_argument("--burst", type=int, default=50,
                    help="Frame per burst — ogni burst = 9 frame "
                         "(default 50, 0 = infinito)")
    ap.add_argument("--rate", type=float, default=100.0,
                    help="Frame rate in Hz (default 100)")
    ap.add_argument("--tx", type=int, default=None,
                    help="Solo TX node specifico (default: tutti)")
    ap.add_argument("--rx", type=int, default=None,
                    help="Solo RX node specifico (default: tutti)")
    ap.add_argument("--moving", action="store_true",
                    help="Simula movimento: phase_offset e rssi "
                         "cambiano gradualmente")
    ap.add_argument("--pattern", type=str, default="sine",
                    choices=["random", "sine", "flat"],
                    help="Pattern I/Q (default sine)")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    interval = 1.0 / args.rate

    # Filtra percorsi
    paths = [(tx, rx)
             for tx in (range(3) if args.tx is None else [args.tx])
             for rx in (range(3) if args.rx is None else [args.rx])]

    print(f"Iniettore radar3d → {args.ip}:{args.port}")
    print(f"  Percorsi: {len(paths)} ({paths})")
    print(f"  Pattern:  {args.pattern}")
    print(f"  Rate:     {args.rate} Hz ({interval*1000:.1f}ms)")
    print(f"  Movimento: {'SI' if args.moving else 'NO'}")
    print(f"  Burst:    {'infinito' if args.burst == 0 else args.burst}")
    print()

    seq = 0
    burst = 0
    phase = 0.0
    start_ts = time.time_ns() // 1000  # µs

    try:
        while args.burst == 0 or burst < args.burst:
            for tx_node, rx_node in paths:
                # Simula rssi diverso per percorso
                base_rssi = -35 - abs(tx_node - rx_node) * 5
                if args.moving:
                    rssi_var = int(10 * math.sin(phase + tx_node + rx_node))
                    rssi = base_rssi + rssi_var
                    ts_us = start_ts + seq * int(interval * 1_000_000)
                else:
                    rssi = base_rssi
                    ts_us = start_ts + seq * 10000  # 10ms cad

                frame = build_frame(
                    tx_node=tx_node, rx_node=rx_node,
                    seq=seq, rssi=rssi,
                    timestamp_us=ts_us,
                    pattern=args.pattern,
                    phase_offset=phase + tx_node * 0.5 + rx_node * 0.3,
                )
                sock.sendto(frame, (args.ip, args.port))
                seq += 1

            burst += 1
            phase += 0.1  # fase evolve gradualmente

            if burst % 10 == 0:
                print(f"  Inviati {seq} frame ({burst} burst) "
                      f"ts={ts_us}", end="\r")

            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n\n  Fermato. {seq} frame inviati.")

    sock.close()


if __name__ == "__main__":
    main()
