"""
monitor_cli.py — Dashboard CLI per il PresenceDetector.

Due modalità di output:
    --pretty  (default): live dashboard ANSI con barre, sparkline, stato grosso.
                         Refresh ~10 Hz. Niente dipendenze esterne (solo ANSI).
    --jsonl              : una riga JSON per ogni snapshot (machine-readable,
                          per consumarlo da WebSocket, log, pipe).

Input:
    --udp-port 5005   (default): legge frame radar3d (magic 0xC5110003) da UDP.
    --stdin                   : legge frame CSV/binari da stdin (per replay).

Hot keys (solo --pretty):
    c   ricalibra (rilascia baseline, raccoglie nuovo "vuoto" per 30s)
    q   esci

Esempi:
    python3 -m csi.presence.monitor_cli                       # default UDP + pretty
    python3 -m csi.presence.monitor_cli --jsonl > presence.log
    python3 -m csi.presence.monitor_cli --baseline-seconds 15  # calibrazione veloce
"""
from __future__ import annotations

import argparse
import json
import os
import select
import signal
import socket
import sys
import termios
import time
import tty
from collections import deque

from .detector import PresenceDetector, PresenceState, PresenceReading

# ============================================================
# Costanti UI
# ============================================================
ANSI_CLEAR = "\033[2J\033[H"
ANSI_HIDE_CURSOR = "\033[?25l"
ANSI_SHOW_CURSOR = "\033[?25h"
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"

# Color codes per stato
STATE_COLOR = {
    PresenceState.UNKNOWN:    "\033[90m",       # grigio
    PresenceState.EMPTY:      "\033[32m",       # verde
    PresenceState.STILL: "\033[33m",       # giallo
    PresenceState.MOTION:   "\033[31;1m",     # rosso bold
}
STATE_LABEL = {
    PresenceState.UNKNOWN:    "○ UNKNOWN   ",
    PresenceState.EMPTY:      "● EMPTY     ",
    PresenceState.STILL: "● STILL",
    PresenceState.MOTION:   "● MOTION  ",
}

SPARK = "▁▂▃▄▅▆▇█"


# ============================================================
# UDP source
# ============================================================
def udp_frames(port: int):
    """Generator di frame parsati radar3d/legacy. Yields dict."""
    from csi.csi_processor import parse_csi_radar3d, parse_csi_binary, parse_csi_line

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.2)
    text_buf = bytearray()

    while True:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            yield None  # heartbeat per refresh UI anche senza frame
            continue
        except OSError:
            break

        if len(data) >= 4:
            magic = int.from_bytes(data[:4], "little")
            if magic == 0xC5110003:
                p = parse_csi_radar3d(data)
                if p:
                    yield p
                    continue
            if magic == 0xC5110002:
                p = parse_csi_crossping(data)
                if p:
                    yield p
                    continue
            if magic == 0xC5110001:
                p = parse_csi_binary(data)
                if p:
                    yield p
                    continue

        # Fallback testo (legacy "CSI:...")
        text_buf.extend(data)
        while b"\n" in text_buf:
            line, _, text_buf = text_buf.partition(b"\n")
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            if text.startswith("CSI:"):
                p = parse_csi_line(text)
                if p:
                    yield p


def stdin_frames():
    """Generator di frame da stdin (replay testo CSI:...)."""
    from csi.csi_processor import parse_csi_line
    for line in sys.stdin:
        line = line.strip()
        if line.startswith("CSI:"):
            p = parse_csi_line(line)
            if p:
                yield p


# ============================================================
# Sparkline
# ============================================================
def _sparkline(values: list[float], width: int = 32) -> str:
    if not values:
        return " " * width
    # Pad/trim a width
    vals = list(values[-width:])
    while len(vals) < width:
        vals.insert(0, 0.0)
    lo = min(vals)
    hi = max(vals)
    rng = hi - lo
    if rng < 1e-9:
        return SPARK[0] * width
    out = []
    n = len(SPARK)
    for v in vals:
        idx = int((v - lo) / rng * (n - 1))
        idx = max(0, min(n - 1, idx))
        out.append(SPARK[idx])
    return "".join(out)


# ============================================================
# Pretty renderer
# ============================================================
class PrettyRenderer:
    def __init__(self):
        self.intensity_hist: deque[float] = deque(maxlen=80)

    def render(self, reading: PresenceReading, rate_hz: float, msg: str = "") -> str:
        self.intensity_hist.append(reading.intensity)

        color = STATE_COLOR.get(reading.state, "")
        label = STATE_LABEL.get(reading.state, "??")
        reset = ANSI_RESET

        # Header
        out = [ANSI_CLEAR]
        out.append(f"{ANSI_BOLD}WiFi Sensing — Presence Monitor{reset}  "
                   f"{ANSI_DIM}(c=ricalibra, q=esci){reset}\n")
        out.append("─" * 64 + "\n")

        # Stato grosso
        bg = self._state_bg(reading.state)
        out.append(f"  {bg} {label} {reset}  "
                   f"conf {self._bar(reading.confidence, 12)} {reading.confidence:.0%}\n")
        out.append(f"  {ANSI_DIM}durata{reset} {reading.duration_s:6.1f}s   "
                   f"{ANSI_DIM}rate{reset} {rate_hz:5.1f} fps   "
                   f"{ANSI_DIM}percorsi{reset} {reading.n_active_paths}/{reading.n_total_paths}\n")
        out.append("\n")

        # Calibrazione
        if not reading.calibrated:
            pct = reading.calibration_progress * 100
            out.append(f"  {ANSI_BOLD}Calibrazione baseline{reset} "
                       f"{self._bar(reading.calibration_progress, 28)} {pct:5.1f}%\n")
            out.append(f"  {ANSI_DIM}(stanza vuota, non muoverti){reset}\n\n")
        else:
            out.append(f"  {ANSI_DIM}baseline{reset} {reading.baseline:.3f}   "
                       f"{ANSI_DIM}soglie{reset} "
                       f"EMPTY<{reading.baseline*1.5:.3f}  "
                       f"MOVE>{reading.baseline*4.0:.3f}\n\n")

        # Intensity sparkline
        spark = _sparkline(list(self.intensity_hist), width=48)
        out.append(f"  {ANSI_DIM}intensità{reset} {color}{spark}{reset}  "
                   f"{reading.intensity:.3f}\n")

        # Per-RX bars
        if reading.per_rx_intensity:
            out.append(f"\n  {ANSI_DIM}per ricevitore (std max){reset}\n")
            max_v = max(reading.per_rx_intensity.values()) or 1e-6
            for rx in sorted(reading.per_rx_intensity.keys()):
                v = reading.per_rx_intensity[rx]
                bar = self._bar(v / max_v, 24)
                out.append(f"    RX{rx}  {bar}  {v:.3f}\n")

        if msg:
            out.append(f"\n  {ANSI_DIM}{msg}{reset}\n")

        return "".join(out)

    @staticmethod
    def _bar(frac: float, width: int) -> str:
        frac = max(0.0, min(1.0, frac))
        filled = int(round(frac * width))
        return "█" * filled + "░" * (width - filled)

    @staticmethod
    def _state_bg(state: PresenceState) -> str:
        return {
            PresenceState.UNKNOWN:    "\033[100;97m",  # grigio
            PresenceState.EMPTY:      "\033[42;97m",   # verde
            PresenceState.STILL: "\033[43;30m",   # giallo
            PresenceState.MOTION:   "\033[41;97m",   # rosso
        }.get(state, "\033[40;97m")


# ============================================================
# Raw mode keyboard (no curses dependency)
# ============================================================
class _RawTTY:
    """Context manager per leggere tasti singoli senza enter, senza echo."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and sys.stdin.isatty()
        self._old: Any = None

    def __enter__(self):
        if not self.enabled:
            return self
        self._old = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
        return self

    def __exit__(self, *a):
        if not self.enabled or self._old is None:
            return
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old)

    def read_key(self) -> str | None:
        if not self.enabled:
            return None
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if r:
            return sys.stdin.read(1)
        return None


# placeholder for typing in older runtimes
from typing import Any


# ============================================================
# Main
# ============================================================
def main():
    ap = argparse.ArgumentParser(description="WiFi Sensing — presence monitor CLI")
    ap.add_argument("--udp-port", type=int, default=5005,
                    help="Porta UDP per ricevere frame radar3d (default: 5005)")
    ap.add_argument("--stdin", action="store_true",
                    help="Leggi frame da stdin invece che UDP")
    ap.add_argument("--jsonl", action="store_true",
                    help="Output JSON-line invece di dashboard ANSI")
    ap.add_argument("--baseline-seconds", type=float, default=30.0,
                    help="Durata calibrazione baseline (default: 30)")
    ap.add_argument("--window", type=int, default=100,
                    help="Window size in frame per il detector (default: 100)")
    ap.add_argument("--empty-mult", type=float, default=1.5,
                    help="Moltiplicatore baseline → soglia EMPTY (default: 1.5)")
    ap.add_argument("--move-mult", type=float, default=4.0,
                    help="Moltiplicatore baseline → soglia MOTION (default: 4.0)")
    ap.add_argument("--refresh-hz", type=float, default=10.0,
                    help="Refresh rate UI (default: 10 Hz)")
    args = ap.parse_args()

    detector = PresenceDetector(
        window_size=args.window,
        baseline_seconds=args.baseline_seconds,
        empty_mult=args.empty_mult,
        move_mult=args.move_mult,
    )

    if args.stdin:
        source = stdin_frames()
    else:
        source = udp_frames(args.udp_port)
        print(f"  UDP in ascolto su :{args.udp_port}", file=sys.stderr)

    pretty = not args.jsonl
    renderer = PrettyRenderer() if pretty else None
    last_render = 0.0
    refresh_interval = 1.0 / max(args.refresh_hz, 1.0)
    msg = ""
    frame_count = 0
    rate_window: deque[float] = deque(maxlen=50)
    last_frame_t = time.time()

    def cleanup(*_):
        if pretty:
            sys.stdout.write(ANSI_SHOW_CURSOR + ANSI_RESET + "\n")
            sys.stdout.flush()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    if pretty:
        sys.stdout.write(ANSI_HIDE_CURSOR)
        sys.stdout.flush()

    with _RawTTY(enabled=pretty) as tty_ctx:
        try:
            for frame in source:
                now = time.time()

                if frame is not None:
                    detector.add_frame(frame)
                    frame_count += 1
                    dt = now - last_frame_t
                    if dt > 0:
                        rate_window.append(1.0 / dt)
                    last_frame_t = now

                # Hotkeys (solo pretty)
                if pretty:
                    k = tty_ctx.read_key()
                    if k == "q":
                        cleanup()
                    elif k == "c":
                        detector.reset_calibration()
                        msg = "Calibrazione resettata."
                        renderer.intensity_hist.clear()

                # Render
                if (now - last_render) >= refresh_interval:
                    reading = detector.current_reading()
                    rate_hz = sum(rate_window) / len(rate_window) if rate_window else 0.0

                    if pretty:
                        out = renderer.render(reading, rate_hz, msg)
                        sys.stdout.write(out)
                        sys.stdout.flush()
                        msg = ""
                    else:
                        # JSON-line: emetti solo se almeno 1 frame da ultimo render
                        line = reading.to_json_line()
                        sys.stdout.write(line + "\n")
                        sys.stdout.flush()

                    last_render = now
        except KeyboardInterrupt:
            cleanup()
        except BrokenPipeError:
            cleanup()


if __name__ == "__main__":
    main()
