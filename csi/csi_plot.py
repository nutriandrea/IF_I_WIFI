#!/usr/bin/env python3
"""
CSI Live Plot — real-time visualization of Channel State Information.

Three display modes:
  waterfall (default)  — spectrogram of all subcarrier amplitudes over time
  time                 — time-series for N selected subcarriers
  bar                  — bar chart of current amplitude per subcarrier

Reads from ESP32 serial (same format as csi_mac.py) or replays a
recorded capture file.

Usage:
    python3 -m csi.csi_plot                                 # autodetect port
    python3 -m csi.csi_plot --port /dev/cu.usbserial-xxxx
    python3 -m csi.csi_plot --mode time --subcarriers 10,20,30
    python3 -m csi.csi_plot --replay csi_capture.txt
    python3 -m csi.csi_plot --port /dev/cu.usbserial-xxxx --mode bar

Dipendenze: pip install pyserial matplotlib numpy

Usage:
    python3 -m csi.csi_plot
    python3 -m csi.csi_plot --port /dev/cu.usbserial-xxxx
    python3 -m csi.csi_plot --mode time --subcarriers 10,20,30
    python3 -m csi.csi_plot --replay csi_capture.txt
"""

import argparse
import glob
import os
import sys
import time
from collections import deque
from typing import Any, Optional

import numpy as np

# matplotlib: non-interactive finché non serve
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

try:
    import serial
except ImportError:
    sys.exit("Manca pyserial. Installa con:  pip install pyserial")

from .csi_processor import parse_csi_line

# ──────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────
DEFAULT_BAUD = 921600
WATERFALL_LENGTH = 200          # righe nello waterfall
TIME_WINDOW = 10                # secondi per mode time
MAX_SUBCARRIERS = 128

# ──────────────────────────────────────────────────────────
# Serial helpers
# ──────────────────────────────────────────────────────────
def autodetect_port() -> Optional[str]:
    cands: list[str] = []
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
# Data source: serial or replay file
# ──────────────────────────────────────────────────────────
class SerialSource:
    def __init__(self, port: Optional[str], baud: int):
        self.ser = open_port(port, baud)
        self.buf = bytearray()

    def read_line(self) -> Optional[str]:
        while True:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if not chunk:
                return None
            self.buf.extend(chunk)
            if b"\n" in self.buf:
                line, _, rest = self.buf.partition(b"\n")
                self.buf = bytearray(rest)
                return line.decode("utf-8", errors="replace").rstrip("\r")
            # wait for more data
            return None

    def close(self):
        if self.ser:
            self.ser.close()


class ReplaySource:
    def __init__(self, path: str, speed: float = 1.0):
        self.lines = []
        self.idx = 0
        self.speed = speed
        self._t0: float | None = None
        self._last_ts: float | None = None
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    self.lines.append(line)
        print(f"# Replay: {len(self.lines)} linee da {path}", file=sys.stderr)

    def read_line(self) -> Optional[str]:
        if self.idx >= len(self.lines):
            time.sleep(0.5)
            return None
        line = self.lines[self.idx]
        self.idx += 1
        # rate-limit replay to approximate real-time
        if self._t0 is None:
            self._t0 = time.time()
            self._last_ts = self._t0
        else:
            if self.speed > 0:
                now = time.time()
                assert self._last_ts is not None
                elapsed = now - self._last_ts
                target_gap = 1.0 / (60 * self.speed)  # ~60 Hz target
                if elapsed < target_gap and self.speed > 0:
                    time.sleep(target_gap - elapsed)
                self._last_ts = now
        return line

    def close(self):
        pass


# ──────────────────────────────────────────────────────────
# Plot modes
# ──────────────────────────────────────────────────────────
class WaterfallPlot:
    """Spectrogram-style waterfall of all subcarrier amplitudes."""

    def __init__(self, ax, subcarrier_count: int):
        self.ax = ax
        self.n_sub = subcarrier_count
        self.buffer: deque = deque(maxlen=WATERFALL_LENGTH)
        self.last_seq = 0
        self.last_rssi = 0
        self.last_rate = 0
        self.lost = 0
        self.image: Any = None
        self.rssi_text: Any = None
        self.rate_text: Any = None
        self.seq_text: Any = None

        ax.set_xlabel("Subcarrier")
        ax.set_ylabel("Time (frames)")
        ax.set_title("CSI Waterfall — Amplitude per Subcarrier")
        self._init_image()

    def _init_image(self):
        empty = np.zeros((WATERFALL_LENGTH, self.n_sub))
        self.image = self.ax.imshow(empty, aspect="auto", cmap="viridis",
                                     interpolation="nearest",
                                     vmin=0, vmax=40)
        plt.colorbar(self.image, ax=self.ax, label="Amplitude")
        self.rssi_text = self.ax.text(0.02, 0.98, "", transform=self.ax.transAxes,
                                       va="top", fontsize=10,
                                       bbox=dict(boxstyle="round", fc="wheat", alpha=0.7))
        self.rate_text = self.ax.text(0.98, 0.98, "", transform=self.ax.transAxes,
                                       va="top", ha="right", fontsize=10,
                                       bbox=dict(boxstyle="round", fc="lightcyan", alpha=0.7))
        self.seq_text = self.ax.text(0.98, 0.02, "", transform=self.ax.transAxes,
                                      va="bottom", ha="right", fontsize=9,
                                      color="gray")

    def update(self, parsed: dict):
        n_sub = len(parsed.get("csi", []))
        if n_sub < 2:
            return
        amps = [c["ampl"] for c in parsed["csi"]]
        # Track RSSI on side
        rssi = parsed.get("rssi", 0)
        seq = parsed.get("seq", 0)
        rate = parsed.get("rate", 0)

        if self.last_seq > 0:
            gap = seq - self.last_seq
            if gap > 1:
                self.lost += gap - 1
        self.last_seq = seq
        self.last_rssi = rssi
        self.last_rate = rate

        self.buffer.append(amps)

        # If subcarrier count changed, resize
        arr = np.array(self.buffer)
        if arr.shape[1] != self.image.get_array().shape[1]:
            self.image.set_data(arr)
            self.image.autoscale()
        else:
            # Pad if fewer rows than waterfall
            if arr.shape[0] < WATERFALL_LENGTH:
                padded = np.zeros((WATERFALL_LENGTH, arr.shape[1]))
                padded[-arr.shape[0]:] = arr
                self.image.set_data(padded)
            else:
                self.image.set_data(arr)
            self.image.autoscale()

        # Stats overlay
        fps = self._estimate_fps()
        self.rssi_text.set_text(f"RSSI: {rssi} dBm  |  Seq: {seq}  |  "
                                f"Lost: {self.lost}")
        self.rate_text.set_text(f"Sub: {n_sub}  |  Rate: {rate} Mbps  |  "
                                f"{fps:.0f} fps")

    def _estimate_fps(self) -> float:
        if len(self.buffer) < 3:
            return 0
        return len(self.buffer) / (self.buffer[-1][0] - self.buffer[0][0] + 1e-9) \
            if hasattr(self.buffer[0], '__len__') else 0


class TimePlot:
    """Time-series for up to N selected subcarriers."""

    def __init__(self, ax, subcarriers: list[int], window: int = TIME_WINDOW):
        self.ax = ax
        self.subs = subcarriers
        self.window = window
        self.data: dict[int, deque] = {s: deque(maxlen=200) for s in subcarriers}
        self.times: deque = deque(maxlen=200)
        self.lines = {}
        self.start_time = time.time()
        self.rssi_text: Any = None

        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Amplitude")
        ax.set_title(f"CSI Time Series — Subcarrier(s) {','.join(map(str, subcarriers))}")
        ax.grid(True, alpha=0.3)
        import matplotlib as mpl
        colors = mpl.colormaps["tab10"](np.linspace(0, 1, len(subcarriers)))
        for i, s in enumerate(subcarriers):
            line, = ax.plot([], [], color=colors[i], label=f"Sub #{s}")
            self.lines[s] = line
        ax.legend(loc="upper right")
        self.rssi_text = ax.text(0.02, 0.95, "", transform=ax.transAxes,
                                  va="top", fontsize=9,
                                  bbox=dict(boxstyle="round", fc="wheat", alpha=0.7))

    def update(self, parsed: dict):
        now = time.time() - self.start_time
        self.times.append(now)
        for sub in parsed.get("csi", []):
            s = sub["subcarrier"]
            if s in self.data:
                self.data[s].append(sub["ampl"])

        # Update lines
        all_t = list(self.times)
        for s, line in self.lines.items():
            d = list(self.data[s])
            if len(d) == len(all_t):
                line.set_data(all_t, d)
            elif len(d) < len(all_t):
                line.set_data(all_t[-len(d):], d)

        rssi = parsed.get("rssi", "")
        self.rssi_text.set_text(f"RSSI: {rssi} dBm")

        # Auto-scale
        self.ax.relim()
        self.ax.autoscale_view(scalex=False)

    @property
    def needs_redraw(self):
        return True


class BarPlot:
    """Bar chart of amplitude per subcarrier."""

    def __init__(self, ax):
        self.ax = ax
        self.bars = None
        self.n_sub = MAX_SUBCARRIERS
        self.rssi_text = None
        self.stats_text = None

        ax.set_xlabel("Subcarrier")
        ax.set_ylabel("Amplitude")
        ax.set_title("CSI Amplitude — Per Subcarrier")
        ax.set_xlim(-0.5, MAX_SUBCARRIERS - 0.5)
        ax.grid(True, alpha=0.3, axis="y")

    def update(self, parsed: dict):
        amps = [c["ampl"] for c in parsed.get("csi", [])]
        if not amps:
            return

        n = len(amps)
        if self.bars is None or len(self.bars) != n:
            if self.bars is not None:
                for b in self.bars:
                    b.remove()
            self.bars = self.ax.bar(range(n), amps, color="steelblue", width=0.8)
        else:
            for i, b in enumerate(self.bars):
                b.set_height(amps[i])

        rssi = parsed.get("rssi", "")
        ampl_mean = parsed.get("ampl_mean", 0)
        ampl_std = parsed.get("ampl_std", 0)
        self.ax.set_title(f"CSI Amplitude — {n} subcarriers | "
                          f"RSSI: {rssi} dBm")

    @property
    def needs_redraw(self):
        return True


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="CSI Live Plot — real-time visualization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("--port", "-p", help="Porta seriale (default: autodetect)")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD,
                   help=f"Baud rate (default: {DEFAULT_BAUD})")
    p.add_argument("--replay", "-r", metavar="FILE",
                   help="Replay da file di cattura")
    p.add_argument("--speed", type=float, default=1.0,
                   help="Velocita replay (default: 1.0)")
    p.add_argument("--mode", choices=["waterfall", "time", "bar"],
                   default="waterfall",
                   help="Modalità visualizzazione (default: waterfall)")
    p.add_argument("--subcarriers", "-s", default="10,20,30",
                   help="Subcarrier da plottare in mode time (default: 10,20,30)")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args()

    # Source
    if args.replay:
        source = ReplaySource(args.replay, args.speed)
    else:
        source = SerialSource(args.port, args.baud)

    # Parse subcarrier list for time mode
    sub_list = [int(x.strip()) for x in args.subcarriers.split(",")]

    # Setup figure
    fig, ax = plt.subplots(figsize=(12, 6))
    m = fig.canvas.manager
    if m is not None:
        m.set_window_title("CSI Live Plot")

    if args.mode == "time":
        viewer = TimePlot(ax, sub_list)
    elif args.mode == "bar":
        viewer = BarPlot(ax)
    else:
        viewer = WaterfallPlot(ax, MAX_SUBCARRIERS)

    packet_count = 0
    parse_errors = 0
    last_stats = time.time()

    print("# CSI Live Plot — in esecuzione. Premi Ctrl+C per uscire.", file=sys.stderr)

    def animate(_frame):
        nonlocal packet_count, parse_errors, last_stats
        for _ in range(100):  # process up to 100 lines per frame
            line = source.read_line()
            if line is None:
                break
            parsed = parse_csi_line(line)
            if parsed is None:
                parse_errors += 1
                continue
            packet_count += 1

            # Skip meta lines (AP context, switch)
            if parsed.get("type") in ("ap_context", "ap_switch"):
                continue

            viewer.update(parsed)

        # Periodic stats
        now = time.time()
        if now - last_stats >= 2:
            last_stats = now
            print(f"  {packet_count} frame | {parse_errors} parse errs",
                  file=sys.stderr)

        return []

    ani = FuncAnimation(fig, animate, interval=50, cache_frame_data=False, blit=False)
    try:
        plt.show()
    except KeyboardInterrupt:
        pass
    finally:
        source.close()

    print(f"\n# Done: {packet_count} frame processati, {parse_errors} errori",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
