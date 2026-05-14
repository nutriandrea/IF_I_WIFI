#!/usr/bin/env python3
"""
CSI Receiver — portable (UNO Q Linux / macOS).

Legge il flusso CSV CSI dall'ESP32 via USB-Serial, lo parsifica,
salva su file e (opzionale) stampa statistiche live.

Formato atteso (una riga per campione):
    CSI_DATA,STA,<mac>,<rssi>,<rate>,<sig_mode>,<mcs>,<bandwidth>,
    <smoothing>,<not_sounding>,<aggregation>,<stbc>,<fec_coding>,<sgi>,
    <noise_floor>,<ampdu_cnt>,<channel>,<secondary_channel>,
    <local_ts>,<ant>,<sig_len>,<rx_state>,<real_time_set>,
    <real_ts_us>,<len>,[<i0> <q0> <i1> <q1> ...]

Uso:
    python3 csi_receiver.py                       # autodetect porta, log su csi_logs/
    python3 csi_receiver.py --port /dev/ttyUSB0
    python3 csi_receiver.py --stats               # solo stats live, no file
    python3 csi_receiver.py --out my_session.csv
    python3 csi_receiver.py --duration 60         # registra 60s e termina

Dipendenze:
    pip install pyserial
"""

from __future__ import annotations
import argparse
import glob
import re
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

try:
    import serial  # pyserial
except ImportError:
    sys.exit("Manca pyserial. Installa con:  pip install pyserial")


DEFAULT_BAUD = 921600

# ============================================================
# Autodetect porta serial — funziona su Linux (UNO Q) e macOS.
# ============================================================
def autodetect_port() -> Optional[str]:
    candidates: list[str] = []
    # Linux: ESP32 tipicamente come /dev/ttyUSB0 (CP210x/CH340) o /dev/ttyACM0
    candidates += sorted(glob.glob("/dev/ttyUSB*"))
    candidates += sorted(glob.glob("/dev/ttyACM*"))
    # macOS: /dev/cu.usbserial-* (CP210x/CH340) o /dev/cu.SLAB_USBtoUART
    candidates += sorted(glob.glob("/dev/cu.usbserial*"))
    candidates += sorted(glob.glob("/dev/cu.SLAB_USBtoUART*"))
    candidates += sorted(glob.glob("/dev/cu.wchusbserial*"))
    # Filtra duplicati mantenendo ordine
    seen: set[str] = set()
    uniq = [c for c in candidates if not (c in seen or seen.add(c))]
    return uniq[0] if uniq else None


# ============================================================
# Parser
# ============================================================
CSI_PREFIX = "CSI_DATA,"

@dataclass
class CSISample:
    mac: str
    rssi: int
    rate: int
    sig_mode: int
    mcs: int
    bandwidth: int
    channel: int
    noise_floor: int
    local_ts: int
    real_ts_us: int
    length: int
    data: list[int]      # interleaved [imag0, real0, imag1, real1, ...]
    host_ts: float       # tempo di arrivo sul receiver (epoch seconds)
    raw_line: str        # riga originale (per dump fedele su CSV)


_array_re = re.compile(r"\[([^\]]*)\]")

def parse_csi_line(line: str, now: float) -> Optional[CSISample]:
    if not line.startswith(CSI_PREFIX):
        return None
    m = _array_re.search(line)
    if not m:
        return None
    array_str = m.group(1).strip()
    head = line[: m.start()].rstrip(",")
    fields = head.split(",")
    # 25 campi prima dell'array (vedi sketch). Indici:
    # 0=CSI_DATA 1=project 2=mac 3=rssi 4=rate 5=sig_mode 6=mcs 7=bandwidth
    # 8=smoothing 9=not_sounding 10=aggregation 11=stbc 12=fec 13=sgi
    # 14=noise_floor 15=ampdu_cnt 16=channel 17=secondary_channel
    # 18=local_ts 19=ant 20=sig_len 21=rx_state 22=real_time_set
    # 23=real_ts_us 24=len
    if len(fields) < 25:
        return None
    try:
        mac        = fields[2]
        rssi       = int(fields[3])
        rate       = int(fields[4])
        sig_mode   = int(fields[5])
        mcs        = int(fields[6])
        bandwidth  = int(fields[7])
        noise      = int(fields[14])
        channel    = int(fields[16])
        local_ts   = int(fields[18])
        real_ts_us = int(fields[23])
        length     = int(fields[24])
        data       = [int(x) for x in array_str.split()] if array_str else []
    except ValueError:
        return None
    return CSISample(
        mac=mac, rssi=rssi, rate=rate, sig_mode=sig_mode, mcs=mcs,
        bandwidth=bandwidth, channel=channel, noise_floor=noise,
        local_ts=local_ts, real_ts_us=real_ts_us, length=length,
        data=data, host_ts=now, raw_line=line,
    )


# ============================================================
# Iter principale: legge serial line-by-line
# ============================================================
def iter_lines(ser: serial.Serial) -> Iterator[str]:
    buf = bytearray()
    while True:
        chunk = ser.read(ser.in_waiting or 1)
        if not chunk:
            continue
        buf.extend(chunk)
        while b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            buf = bytearray(rest)
            yield line.decode("utf-8", errors="replace").rstrip("\r")


# ============================================================
# Statistiche live (ogni 2s)
# ============================================================
class LiveStats:
    def __init__(self) -> None:
        self.csi = 0
        self.bytes = 0
        self.errors = 0
        self.start = time.time()
        self.last_report = self.start
        self.rssi_window: deque[int] = deque(maxlen=200)
        self.rate_window: deque[float] = deque(maxlen=200)

    def add(self, sample: CSISample, raw_len: int) -> None:
        self.csi += 1
        self.bytes += raw_len
        self.rssi_window.append(sample.rssi)
        self.rate_window.append(sample.host_ts)

    def maybe_report(self) -> None:
        now = time.time()
        if now - self.last_report < 2.0:
            return
        dt_total = now - self.start
        hz_now = 0.0
        if len(self.rate_window) >= 2:
            window_dt = self.rate_window[-1] - self.rate_window[0]
            if window_dt > 0:
                hz_now = (len(self.rate_window) - 1) / window_dt
        mean_rssi = (sum(self.rssi_window) / len(self.rssi_window)) if self.rssi_window else 0.0
        print(
            f"[{dt_total:6.1f}s] csi={self.csi:6d}  hz={hz_now:5.1f}  "
            f"rssi_avg={mean_rssi:+.1f} dBm  kB={self.bytes/1024:.1f}  err={self.errors}",
            file=sys.stderr, flush=True,
        )
        self.last_report = now


# ============================================================
# Main
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="ESP32 CSI receiver")
    ap.add_argument("--port", help="Porta serial (default: autodetect)")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--out", help="File CSV di output (default: csi_logs/csi_<ts>.csv)")
    ap.add_argument("--stats", action="store_true",
                    help="Non scrivere file, solo statistiche live")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="Termina dopo N secondi (default: infinito)")
    ap.add_argument("--quiet", action="store_true",
                    help="Niente statistiche live (utile per pipe)")
    args = ap.parse_args()

    port = args.port or autodetect_port()
    if not port:
        sys.exit("Nessuna porta serial trovata. Specifica con --port /dev/...")
    print(f"# port={port} baud={args.baud}", file=sys.stderr)

    try:
        ser = serial.Serial(port, args.baud, timeout=0.1)
    except serial.SerialException as e:
        sys.exit(f"Impossibile aprire {port}: {e}")

    # Output file
    out_path: Optional[Path] = None
    out_fh = None
    if not args.stats:
        if args.out:
            out_path = Path(args.out)
        else:
            log_dir = Path("csi_logs")
            log_dir.mkdir(exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = log_dir / f"csi_{ts}.csv"
        out_fh = open(out_path, "w", buffering=1)
        out_fh.write("# host_ts,raw_line\n")
        print(f"# logging to {out_path}", file=sys.stderr)

    stats = LiveStats()
    stop = {"v": False}

    def handle_sig(_signum, _frame):
        stop["v"] = True
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    t_start = time.time()
    try:
        for line in iter_lines(ser):
            if stop["v"]:
                break
            if args.duration > 0 and (time.time() - t_start) >= args.duration:
                break

            if not line:
                continue

            # Le righe di debug dello sketch iniziano con '#'
            if line.startswith("#"):
                if not args.quiet:
                    print(line, file=sys.stderr, flush=True)
                continue

            now = time.time()
            sample = parse_csi_line(line, now)
            if not sample:
                stats.errors += 1
                continue

            if out_fh is not None:
                out_fh.write(f"{now:.6f},{line}\n")

            stats.add(sample, len(line))
            if not args.quiet:
                stats.maybe_report()
    finally:
        ser.close()
        if out_fh is not None:
            out_fh.close()
            print(f"# saved {stats.csi} samples to {out_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
