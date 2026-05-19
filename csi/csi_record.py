#!/usr/bin/env python3
"""
CSI Record & Replay — registrazione e riproduzione di dati CSI.

Registra:
    python3 -m csi.csi_record --record                         # autodetect port, salva in csi_capture_*.txt
    python3 -m csi.csi_record --record --rotate 30             # auto-rotate ogni 30 secondi
    python3 -m csi.csi_record --record --output data/session1   # directory custom
    python3 -m csi.csi_record --record --dry-run               # mostra statistiche senza salvare

Replay:
    python3 -m csi.csi_record --replay csi_capture.txt              # replay a stdout
    python3 -m csi.csi_record --replay csi_capture.txt --rate 100   # replay a 100 Hz
    python3 -m csi.csi_record --replay csi_capture.txt --pipe       # replay come input per altri comandi
    python3 -m csi.csi_record --replay csi_capture.txt --websocket :8765   # replay via WebSocket

Info:
    python3 -m csi.csi_record --info csi_capture.txt           # statistiche file registrato

Dipendenze: pip install pyserial (websockets opzionale per --websocket)
"""

import argparse
import glob
import gzip
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Optional

try:
    import serial
except ImportError:
    sys.exit("Manca pyserial. Installa con:  pip install pyserial")

from .csi_processor import parse_csi_line
from .csi_mac import line_source

# ──────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────
DEFAULT_BAUD = 921600
DEFAULT_DIR = "csi_captures"
DEFAULT_ROTATE = 120  # secondi


# ──────────────────────────────────────────────────────────
# Serial helpers (same as csi_mac.py)
# ──────────────────────────────────────────────────────────
def autodetect_port() -> Optional[str]:
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


def open_port(port: Optional[str], baud: int) -> serial.Serial:
    p = port or autodetect_port()
    if not p:
        sys.exit("Nessuna porta seriale trovata. Specifica con --port.")
    try:
        return serial.Serial(p, baud, timeout=0.05)
    except serial.SerialException as e:
        sys.exit(f"Impossibile aprire {p}: {e}")


# ──────────────────────────────────────────────────────────
# Recording
# ──────────────────────────────────────────────────────────
def _record_loop(it, out_dir, rotate_every, dry_run, no_rotate):
    """Core loop: leggi linee da iteratore, scrivi su file, gestisci rotate."""
    out_file: Path | None = None
    fh: Any = None
    file_start = time.time()
    total_lines = 0
    parse_ok = 0
    parse_err = 0
    last_stats = time.time()
    part = 0

    def open_new_file():
        nonlocal fh, file_start, part, out_file
        part += 1
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = out_dir / f"csi_capture_{ts}_p{part:03d}.txt"
        if fh:
            fh.close()
            if out_file:
                print(f"  → Chiuso: {out_file} ({(os.path.getsize(out_file) / 1024):.0f} KB)",
                      file=sys.stderr)
        out_file = fname
        fh = open(fname, "w")
        file_start = time.time()
        print(f"  → Nuovo file: {fname}", file=sys.stderr)

    open_new_file()

    try:
        for line in it:
            line = line.rstrip("\r\n").strip()
            if not line:
                continue

            total_lines += 1
            is_csi = line.startswith("CSI:")

            if is_csi:
                parsed = parse_csi_line(line)
                if parsed:
                    parse_ok += 1
                else:
                    parse_err += 1

            if dry_run:
                if is_csi:
                    print(f"  {line[:80]}{'…' if len(line) > 80 else ''}")
            else:
                assert fh is not None
                fh.write(line + "\n")

            if not no_rotate and time.time() - file_start >= rotate_every:
                open_new_file()

            # Periodic stats
            now = time.time()
            if now - last_stats >= 5 and total_lines > 0:
                last_stats = now
                elapsed = now - file_start
                rate = parse_ok / elapsed if elapsed > 0 else 0
                fsize = os.path.getsize(out_file) / 1024 if out_file else 0
                print(f"  {total_lines:>6} linee | {parse_ok:>6} CSI OK | "
                      f"{parse_err:>4} CSI ERR | {rate:.0f} fps | "
                      f"{fsize:.0f} KB",
                      file=sys.stderr)

    except KeyboardInterrupt:
        pass
    finally:
        if fh:
            fh.close()

    elapsed = time.time() - file_start
    print(f"\n# Recording done: {total_lines} linee totali, {parse_ok} CSI OK, "
          f"{parse_err} CSI ERR in {elapsed:.0f}s ({parse_ok/max(elapsed,1):.0f} fps)",
          file=sys.stderr)
    if parse_err > 0:
        print(f"  ⚠  {parse_err} linee CSI non parse — formato inatteso?",
              file=sys.stderr)
    return 0


def cmd_record(args) -> int:
    out_dir = Path(args.output or DEFAULT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    rotate_every = args.rotate or DEFAULT_ROTATE

    if args.ble:
        print(f"# Recording via BLE to {out_dir}/ — Ctrl+C per fermare", file=sys.stderr)
        it = line_source(args)
        return _record_loop(it, out_dir, rotate_every, args.dry_run, args.no_rotate)

    # Modalità seriale (legacy byte-level)
    ser = open_port(args.port, args.baud)
    time.sleep(1)
    ser.reset_input_buffer()

    rotate_every = args.rotate or DEFAULT_ROTATE
    out_file: Path | None = None
    fh: Any = None
    file_start = 0
    total_lines = 0
    parse_ok = 0
    parse_err = 0
    last_stats = time.time()
    part = 0

    def open_new_file():
        nonlocal fh, file_start, part, out_file
        part += 1
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = out_dir / f"csi_capture_{ts}_p{part:03d}.txt"
        if fh:
            fh.close()
            if out_file:
                print(f"  → Chiuso: {out_file} ({(os.path.getsize(out_file) / 1024):.0f} KB)",
                      file=sys.stderr)
        out_file = fname
        fh = open(fname, "w")
        file_start = time.time()
        print(f"  → Nuovo file: {fname}", file=sys.stderr)

    open_new_file()
    bufs = {}  # per reassembly linee incomplete

    print(f"# Recording to {out_dir}/ — Ctrl+C per fermare", file=sys.stderr)

    try:
        while True:
            chunk = ser.read(ser.in_waiting or 1)
            if not chunk:
                time.sleep(0.001)
                continue

            for byte in chunk:
                ch = chr(byte)
                if ch == "\n":
                    buf_key = "default"
                    line_data = bufs.pop(buf_key, "")
                    if not line_data.strip():
                        continue

                    total_lines += 1
                    line = line_data.rstrip("\r")
                    is_csi = line.startswith("CSI:")

                    if is_csi:
                        parsed = parse_csi_line(line)
                        if parsed:
                            parse_ok += 1
                        else:
                            parse_err += 1

                    if args.dry_run:
                        if is_csi:
                            print(f"  {line[:80]}{'…' if len(line) > 80 else ''}")
                    else:
                        assert fh is not None, "File handle should be open"
                        fh.write(line + "\n")

                    if not args.no_rotate and time.time() - file_start >= rotate_every:
                        open_new_file()

                elif ch == "\r":
                    continue
                else:
                    bufs["default"] = bufs.get("default", "") + ch

            now = time.time()
            if now - last_stats >= 5 and total_lines > 0:
                last_stats = now
                elapsed = now - file_start
                rate = parse_ok / elapsed if elapsed > 0 else 0
                fsize = os.path.getsize(out_file) / 1024 if out_file else 0
                print(f"  {total_lines:>6} linee | {parse_ok:>6} CSI OK | "
                      f"{parse_err:>4} CSI ERR | {rate:.0f} fps | "
                      f"{fsize:.0f} KB",
                      file=sys.stderr)

    except KeyboardInterrupt:
        pass
    finally:
        if fh:
            fh.close()
        ser.close()

    elapsed = time.time() - file_start
    print(f"\n# Recording done: {total_lines} linee totali, {parse_ok} CSI OK, "
          f"{parse_err} CSI ERR in {elapsed:.0f}s ({parse_ok/max(elapsed,1):.0f} fps)",
          file=sys.stderr)
    if parse_err > 0:
        print(f"  ⚠  {parse_err} linee CSI non parse — formato inatteso?",
              file=sys.stderr)
    return 0


# ──────────────────────────────────────────────────────────
# Replay: stdout
# ──────────────────────────────────────────────────────────
def cmd_replay_stdout(args) -> int:
    """Replay CSI file to stdout (for piping to other tools)."""
    path = args.replay
    if not os.path.exists(path):
        sys.exit(f"File non trovato: {path}")

    lines = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                lines.append(line)

    target_rate = args.rate or 0  # 0 = il piu' veloce possibile
    interval = 1.0 / target_rate if target_rate > 0 else 0
    total = len(lines)
    csi_count = 0
    t0 = time.time()

    print(f"# Replaying {total} linee{' at ' + str(args.rate) + 'Hz' if args.rate else ''}",
          file=sys.stderr)
    print(f"# Pipe in: python3 csi_record.py --replay {path} | python3 csi_mac.py --stdin",
          file=sys.stderr)

    for i, line in enumerate(lines):
        if line.startswith("CSI:"):
            csi_count += 1
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        if interval > 0:
            time.sleep(interval)
        if (i + 1) % 1000 == 0:
            print(f"  {i+1}/{total} linee ({csi_count} CSI)", file=sys.stderr)

    elapsed = time.time() - t0
    print(f"# Replay done: {csi_count} CSI frames in {elapsed:.1f}s "
          f"({csi_count/max(elapsed,0.001):.0f} fps)", file=sys.stderr)
    return 0


# ──────────────────────────────────────────────────────────
# Replay: pipe subprocess
# ──────────────────────────────────────────────────────────
def cmd_replay_pipe(args) -> int:
    """Replay file and pipe to another command (e.g. csi_mac.py)."""
    path = args.replay
    cmd = args.pipe  # e.g. ["python3", "csi_mac.py", "--stdin"]
    if not cmd:
        sys.exit("Specifica comando dopo --pipe (es: --pipe 'python3 csi_mac.py --stdin')")

    # Start the subprocess with stdin as pipe
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    assert proc.stdin is not None

    if not os.path.exists(path):
        sys.exit(f"File non trovato: {path}")

    target_rate = args.rate or 0
    interval = 1.0 / target_rate if target_rate > 0 else 0

    with open(path) as f:
        t0 = time.time()
        count = 0
        for line in f:
            line = line.strip()
            if not line:
                continue
            proc.stdin.write((line + "\n").encode())
            proc.stdin.flush()
            count += 1
            if interval > 0:
                time.sleep(interval)

    proc.stdin.close()
    proc.wait()

    elapsed = time.time() - t0
    print(f"# Pipe done: {count} linee in {elapsed:.1f}s", file=sys.stderr)
    return proc.returncode


# ──────────────────────────────────────────────────────────
# Replay: WebSocket (for room_server.py / UI testing)
# ──────────────────────────────────────────────────────────
def cmd_replay_websocket(args) -> int:
    """Replay CSI file over WebSocket — tests room_map UI without ESP32."""
    path = args.replay
    ws_addr = args.websocket  # e.g. ":8765"

    try:
        import asyncio
        import websockets
        import json as _json
    except ImportError:
        sys.exit("Per --websocket serve: pip install websockets")

    if not os.path.exists(path):
        sys.exit(f"File non trovato: {path}")

    # Read all lines
    lines = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                lines.append(line)

    target_rate = args.rate or 10  # default 10 Hz per UI
    interval = 1.0 / target_rate

    print(f"# WebSocket replay: {len(lines)} linee a {target_rate} Hz -> {ws_addr}",
          file=sys.stderr)

    async def serve(websocket):
        t0 = time.time()
        count = 0
        # Send frame count first
        await websocket.send(_json.dumps({"type": "meta", "total_lines": len(lines),
                                          "rate": target_rate}))

        # Convert and stream
        for line in lines:
            parsed = parse_csi_line(line)
            if parsed is None:
                continue

            # Build position-like message for the UI
            elapsed = round(time.time() - t0, 3)
            msg = {
                "type": "csi",
                "seq": parsed.get("seq", 0),
                "rssi": parsed.get("rssi", 0),
                "noise_floor": parsed.get("noise_floor", 0),
                "num_subcarriers": parsed.get("num_subcarriers", 0),
                "ampl_mean": parsed.get("ampl_mean", 0),
                "ampl_std": parsed.get("ampl_std", 0),
                "ampl_max": parsed.get("ampl_max", 0),
                "ampl_min": parsed.get("ampl_min", 0),
                "ap_id": parsed.get("ap_id", 0),
                "elapsed": elapsed,
            }
            await websocket.send(_json.dumps(msg))
            count += 1
            await asyncio.sleep(interval)

        # Send done
        await websocket.send(_json.dumps({"type": "done", "frames": count,
                                          "elapsed": round(time.time() - t0, 3)}))

    async def main():
        host, _, port_str = ws_addr.partition(":")
        port = int(port_str) if port_str else 8765
        host = host or "0.0.0.0"
        print(f"  WebSocket server on ws://{host}:{port}", file=sys.stderr)
        async with websockets.serve(serve, host, port):
            await asyncio.Future()  # run forever

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    return 0


# ──────────────────────────────────────────────────────────
# Info
# ──────────────────────────────────────────────────────────
def cmd_info(args) -> int:
    """Show stats about a recorded CSI file."""
    path = args.info
    if not os.path.exists(path):
        sys.exit(f"File non trovato: {path}")

    ext = Path(path).suffix
    if ext == ".gz":
        f = gzip.open(path, "rt")
    else:
        f = open(path)

    total = 0
    csi_count = 0
    parse_err = 0
    seq_nums = []
    rssi_vals = []
    ampl_means = []
    ampl_stds = []
    ap_ids = []
    timestamps = []
    line_lengths = []

    t0 = None
    for line in f:
        line = line.strip()
        if not line:
            continue
        total += 1
        line_lengths.append(len(line))

        if not line.startswith("CSI:"):
            continue

        csi_count += 1
        parsed = parse_csi_line(line)
        if parsed is None:
            parse_err += 1
            continue
        if not parsed.get("csi"):
            continue

        t = parsed.get("seq", 0)
        if t0 is None:
            t0 = t
        seq_nums.append(t)

        rssi_vals.append(parsed.get("rssi", 0))
        m = parsed.get("ampl_mean", 0)
        s = parsed.get("ampl_std", 0)
        if m:
            ampl_means.append(m)
        if s:
            ampl_stds.append(s)
        ap_ids.append(parsed.get("ap_id", 0))

    f.close()

    duration_s = (seq_nums[-1] - seq_nums[0]) / 60.0 if len(seq_nums) > 1 else 0

    print(f"\n{'='*60}")
    print(f"  CSI Capture Info: {path}")
    print(f"{'='*60}")
    print(f"  File size:        {os.path.getsize(path) / 1024:.0f} KB")
    print(f"  Total lines:      {total}")
    print(f"  CSI frames:       {csi_count} ({csi_count/max(total,1)*100:.0f}%)")
    if parse_err:
        print(f"  Parse errors:     {parse_err}")
    print(f"  Avg line len:     {mean(line_lengths):.0f} B" if line_lengths else "")
    if len(seq_nums) >= 2:
        print(f"  Duration:         {duration_s:.0f}s ({duration_s/60:.1f} min)")
        print(f"  Avg frame rate:   {len(seq_nums)/max(duration_s,1):.0f} fps")

    if seq_nums:
        print(f"\n  --- Sequece ---")
        print(f"  First seq:        {seq_nums[0]}")
        print(f"  Last seq:         {seq_nums[-1]}")
        gaps = [seq_nums[i+1] - seq_nums[i] for i in range(len(seq_nums)-1)]
        avg_gap = mean(gaps) if gaps else 0
        lost = sum(max(0, g - 1) for g in gaps)
        print(f"  Avg gap:          {avg_gap:.1f}")
        print(f"  Lost frames:      {lost}")

    if rssi_vals:
        print(f"\n  --- RSSI ---")
        print(f"  Mean:             {mean(rssi_vals):.0f} dBm")
        print(f"  Min:              {min(rssi_vals)} dBm")
        print(f"  Max:              {max(rssi_vals)} dBm")
        print(f"  Std:              {stdev(rssi_vals):.1f} dB" if len(rssi_vals) >= 2 else "")

    if ampl_means:
        print(f"\n  --- Amplitude (mean per frame) ---")
        print(f"  Mean:             {mean(ampl_means):.1f}")
        print(f"  Min:              {min(ampl_means):.1f}")
        print(f"  Max:              {max(ampl_means):.1f}")
        print(f"  Std:              {stdev(ampl_means):.1f}" if len(ampl_means) >= 2 else "")

    if len(ap_ids) > 0:
        unique_aps = set(ap_ids)
        print(f"\n  --- APs ---")
        print(f"  Unique AP IDs:    {len(unique_aps)} {sorted(unique_aps)}")
        for ap in sorted(unique_aps):
            n = sum(1 for a in ap_ids if a == ap)
            print(f"    AP {ap}: {n} frames ({n/len(ap_ids)*100:.0f}%)")

    return 0


# ──────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(
        description="CSI Record & Replay",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)

    # Record
    p.add_argument("--record", action="store_true", help="Registra da seriale/BLE")
    p.add_argument("--port", "-p", help="Porta seriale (default: autodetect)")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    p.add_argument("--ble", action="store_true",
                   help="Usa BLE invece di seriale (ESP32 BLE firmware)")
    p.add_argument("--output", "-o", help="Directory output (default: csi_captures/)")
    p.add_argument("--rotate", type=int, metavar="SEC", default=DEFAULT_ROTATE,
                   help=f"Auto-rotate file ogni N sec (default: {DEFAULT_ROTATE})")
    p.add_argument("--no-rotate", action="store_true", help="Disabilita auto-rotate")
    p.add_argument("--dry-run", action="store_true", help="Solo statistiche, non salvare")

    # Replay
    p.add_argument("--replay", metavar="FILE", help="File da riprodurre")
    p.add_argument("--rate", type=float, metavar="HZ",
                   help="Frame rate replay (default: massima velocita)")
    p.add_argument("--pipe", nargs="+", metavar="CMD",
                   help="Replay pipe verso comando (es: --pipe python3 csi_mac.py --stdin)")
    p.add_argument("--websocket", metavar="ADDR",
                   help="Replay via WebSocket (es: :8765)")

    # Info
    p.add_argument("--info", metavar="FILE", help="Statistiche file registrato")

    args = p.parse_args()

    if args.info:
        return cmd_info(args)
    elif args.record:
        return cmd_record(args)
    elif args.replay:
        if args.pipe:
            return cmd_replay_pipe(args)
        elif args.websocket:
            return cmd_replay_websocket(args)
        else:
            return cmd_replay_stdout(args)
    else:
        p.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
