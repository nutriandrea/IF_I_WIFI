#!/usr/bin/env python3
"""
CSI Presence Detector — real-time.

Legge il flusso CSV CSI dall'ESP32 (via porta serial), calcola l'ampiezza
per sotto-portante, aggrega su una finestra mobile, e segnala presenza
quando la varianza supera una soglia adattiva.

Idea di fondo:
  - Il segnale CSI cambia molto piu' del solo RSSI quando una persona si
    muove nel canale tra ESP32 e AP (multipath fading).
  - Per ogni sample CSI estraiamo amp[k] = sqrt(I[k]^2 + Q[k]^2) per
    ogni sotto-portante k (escluse quelle a zero / pilot).
  - Costruiamo una serie temporale di "energia di variazione":
        e(t) = mean_k( std_window( amp[k] ) )
  - Baseline = mediana di e(t) sui primi B secondi (ambiente vuoto).
  - Presenza = e(t) > baseline * SOGLIA  (default 1.8x).

Uso:
    # live da ESP32
    python3 csi_presence.py

    # da file salvato con csi_receiver.py
    python3 csi_presence.py --file csi_logs/csi_20260514_140000.csv

    # taratura: 30s di baseline a stanza vuota, poi monitor
    python3 csi_presence.py --baseline 30 --threshold 1.8

Dipendenze:  pip install pyserial
"""

from __future__ import annotations
import argparse
import math
import sys
import time
from collections import deque
from typing import Iterable, Iterator, Optional

from csi_receiver import (
    CSISample,
    DEFAULT_BAUD,
    autodetect_port,
    iter_lines,
    parse_csi_line,
)

# Import condizionale pyserial: necessario solo in modalita' live
try:
    import serial  # noqa: F401
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False


# ============================================================
# Estrazione ampiezza per sotto-portante
# ============================================================
def csi_amplitudes(sample: CSISample) -> list[float]:
    """
    Il buffer CSI dell'ESP32 contiene coppie int8 (imag, real) per ogni
    sotto-portante. len e' tipicamente 128 (64 sub-carrier HT20) o 384
    (HT40 LLTF+HTLTF). Restituisce sqrt(I^2 + Q^2) per ogni coppia.
    Le sub-portanti DC/null tendono a essere ~0 e sporcano la media:
    le filtriamo a valle.
    """
    d = sample.data
    n = len(d) // 2
    amps: list[float] = []
    for k in range(n):
        im = d[2 * k]
        re = d[2 * k + 1]
        amps.append(math.sqrt(im * im + re * re))
    return amps


# ============================================================
# Detector adattivo
# ============================================================
class CSIPresenceDetector:
    """
    Per ogni sotto-portante mantiene una deque di lunghezza window_size.
    Calcola std per ciascuna, fa la media sulle sotto-portanti attive
    (non-DC, non-null), e confronta con baseline.
    """

    def __init__(
        self,
        window_size: int = 30,         # numero di sample (=> ~0.3s @100Hz, ~3s @10Hz)
        baseline_seconds: float = 30,  # primi N secondi = ambiente vuoto
        threshold_ratio: float = 1.8,  # presenza se e(t) > baseline * ratio
        min_amp: float = 2.0,          # soglia per scartare sub-portanti null
    ) -> None:
        self.window_size = window_size
        self.baseline_seconds = baseline_seconds
        self.threshold_ratio = threshold_ratio
        self.min_amp = min_amp

        self.per_sc: dict[int, deque[float]] = {}      # sub-carrier index -> deque amp
        self.energy_history: deque[tuple[float, float]] = deque(maxlen=4096)
        self.t0: Optional[float] = None
        self.baseline_value: Optional[float] = None

    @staticmethod
    def _stdev(buf: Iterable[float]) -> float:
        xs = list(buf)
        n = len(xs)
        if n < 2:
            return 0.0
        m = sum(xs) / n
        var = sum((x - m) * (x - m) for x in xs) / (n - 1)
        return math.sqrt(var)

    def update(self, sample: CSISample) -> tuple[float, bool]:
        """Ritorna (energia_variazione_corrente, presenza)."""
        if self.t0 is None:
            self.t0 = sample.host_ts

        amps = csi_amplitudes(sample)
        active_stds: list[float] = []

        for k, a in enumerate(amps):
            buf = self.per_sc.setdefault(k, deque(maxlen=self.window_size))
            buf.append(a)
            # ignora sub-portanti che restano sempre ~0 (null / DC)
            if len(buf) < self.window_size:
                continue
            mean_a = sum(buf) / len(buf)
            if mean_a < self.min_amp:
                continue
            active_stds.append(self._stdev(buf))

        if not active_stds:
            return 0.0, False

        energy = sum(active_stds) / len(active_stds)
        self.energy_history.append((sample.host_ts, energy))

        # Stima baseline alla fine della fase calibrazione
        elapsed = sample.host_ts - self.t0
        if self.baseline_value is None and elapsed >= self.baseline_seconds:
            vals = sorted(e for _, e in self.energy_history if e > 0)
            if vals:
                self.baseline_value = vals[len(vals) // 2]   # mediana

        if self.baseline_value is None:
            return energy, False

        return energy, energy > self.baseline_value * self.threshold_ratio


# ============================================================
# Sorgenti CSI: serial (live) o file CSV (replay)
# ============================================================
def iter_samples_live(port: Optional[str], baud: int) -> Iterator[CSISample]:
    if not HAS_SERIAL:
        sys.exit("Live mode richiede pyserial:  pip install pyserial")
    import serial as _serial
    p = port or autodetect_port()
    if not p:
        sys.exit("Nessuna porta serial trovata. Specifica con --port.")
    print(f"# live: {p} @ {baud}", file=sys.stderr)
    ser = _serial.Serial(p, baud, timeout=0.1)
    try:
        for line in iter_lines(ser):
            if not line or line.startswith("#"):
                continue
            s = parse_csi_line(line, time.time())
            if s:
                yield s
    finally:
        ser.close()


def iter_samples_file(path: str) -> Iterator[CSISample]:
    """
    Legge un file salvato da csi_receiver.py.
    Formato: "<host_ts>,<raw_line>"  oppure semplicemente "<raw_line>".
    """
    with open(path) as fh:
        for raw in fh:
            raw = raw.rstrip("\r\n")
            if not raw or raw.startswith("#"):
                continue
            host_ts = time.time()
            line = raw
            # Se ha il prefisso "<float>,CSI_DATA,...", separa il timestamp
            if not raw.startswith("CSI_DATA,"):
                head, sep, rest = raw.partition(",")
                if sep and rest.startswith("CSI_DATA,"):
                    try:
                        host_ts = float(head)
                    except ValueError:
                        pass
                    line = rest
            s = parse_csi_line(line, host_ts)
            if s:
                yield s


# ============================================================
# Main
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="CSI presence detector")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--port", help="Porta serial (default: autodetect)")
    src.add_argument("--file", help="CSV registrato con csi_receiver.py")

    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--window", type=int, default=30,
                    help="Sample per finestra std (default 30)")
    ap.add_argument("--baseline", type=float, default=30.0,
                    help="Secondi di calibrazione baseline (default 30)")
    ap.add_argument("--threshold", type=float, default=1.8,
                    help="Ratio energia/baseline per presenza (default 1.8)")
    ap.add_argument("--print-every", type=int, default=5,
                    help="Stampa ogni N sample (default 5)")
    args = ap.parse_args()

    detector = CSIPresenceDetector(
        window_size=args.window,
        baseline_seconds=args.baseline,
        threshold_ratio=args.threshold,
    )

    src_iter: Iterable[CSISample]
    if args.file:
        src_iter = iter_samples_file(args.file)
    else:
        src_iter = iter_samples_live(args.port, args.baud)

    n = 0
    n_present = 0
    last_state = False
    t_start = time.time()

    try:
        for sample in src_iter:
            energy, present = detector.update(sample)
            n += 1
            if present:
                n_present += 1

            # transizioni
            if present != last_state:
                ts = time.strftime("%H:%M:%S")
                what = "PRESENZA" if present else "vuoto"
                print(f"[{ts}] -> {what}  (e={energy:.2f} "
                      f"baseline={detector.baseline_value or 0:.2f})", flush=True)
                last_state = present

            # progress
            if n % args.print_every == 0:
                elapsed = sample.host_ts - (detector.t0 or sample.host_ts)
                phase = "calibrazione" if detector.baseline_value is None else "monitor"
                bar = "##" if present else "  "
                print(f"[{elapsed:6.1f}s {phase:12s}] {bar}  "
                      f"energy={energy:6.2f}  rssi={sample.rssi:+4d}  "
                      f"n={n}", flush=True)
    except KeyboardInterrupt:
        pass

    total = time.time() - t_start
    pct = (100.0 * n_present / n) if n else 0.0
    print(f"\n# total samples={n}  presenza={pct:.1f}%  "
          f"baseline={detector.baseline_value}  durata={total:.1f}s",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
