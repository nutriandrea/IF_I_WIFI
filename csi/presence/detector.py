"""
detector.py — PresenceDetector senza ML.

Algoritmo:
    1. Per ogni percorso (tx_node, rx_node) mantengo deque di ampl_mean
       su finestra ~ 1 secondo (a 100 Hz → 100 sample).
    2. Calcolo std per ogni percorso → aggregato = max(std) tra i percorsi.
       (max è più sensibile del mean: il movimento è spesso visibile solo
       su un sottoinsieme dei percorsi.)
    3. EMA smoothing sull'aggregato per ridurre lo jitter inter-frame.
    4. Calibrazione: durante i primi `baseline_seconds` di "stanza vuota",
       memorizzo baseline_max e baseline_std.
    5. State machine con hysteresis e dwell time:
         EMPTY       ← aggregato < baseline_max * empty_mult
         STILL  ← baseline_max * empty_mult ≤ aggregato < baseline_max * move_mult
         MOTION    ← aggregato ≥ baseline_max * move_mult

Nessun modello, nessun training. Funziona con 1, 2, o 3 ESP32 (degradazione
graduale: meno percorsi = meno sensibilità, ma l'algoritmo non si rompe).

L'input è il dict prodotto da csi.csi_processor.parse_csi_radar3d (o un
qualunque parser CSI che esponga `ampl_mean`, `tx_node`, `rx_node`).
"""
from __future__ import annotations

import json
import math
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


# ============================================================
# State enum
# ============================================================
class PresenceState(str, Enum):
    UNKNOWN = "UNKNOWN"        # finestra non ancora piena o non calibrato
    EMPTY = "EMPTY"            # stanza vuota
    STILL = "STILL"  # persona ferma (in piedi/seduta/respiro)
    MOTION = "MOTION"      # movimento attivo (cammino, gesti ampi)


# ============================================================
# Reading dataclass
# ============================================================
@dataclass
class PresenceReading:
    """Snapshot atomico dello stato del detector."""
    state: PresenceState
    confidence: float            # 0..1, derivato dalla distanza dalla soglia
    intensity: float             # std aggregata corrente (EMA-smoothed)
    intensity_raw: float         # std aggregata pre-EMA
    baseline: float              # baseline_max appreso (0 se non calibrato)
    duration_s: float            # secondi nello stato attuale
    per_rx_intensity: dict[int, float]   # std max per ricevitore
    n_active_paths: int          # quanti (tx,rx) hanno la finestra piena
    n_total_paths: int           # quanti (tx,rx) abbiamo visto almeno una volta
    calibrated: bool
    calibration_progress: float  # 0..1
    t: float                     # timestamp UNIX

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        d["per_rx_intensity"] = {str(k): round(v, 4)
                                 for k, v in self.per_rx_intensity.items()}
        d["intensity"] = round(self.intensity, 4)
        d["intensity_raw"] = round(self.intensity_raw, 4)
        d["baseline"] = round(self.baseline, 4)
        d["confidence"] = round(self.confidence, 3)
        d["duration_s"] = round(self.duration_s, 2)
        d["calibration_progress"] = round(self.calibration_progress, 3)
        d["t"] = round(self.t, 3)
        return d

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


# ============================================================
# Detector
# ============================================================
class PresenceDetector:
    """Stateless-free detector di presenza/movimento, no-ML.

    Parametri principali:
        window_size       : numero di sample per (tx,rx) per calcolare std.
                            A 100 Hz frame rate, 100 = 1 secondo di finestra.
        ema_alpha         : peso EMA sull'aggregato (0..1, più alto = meno smoothing).
        baseline_seconds  : secondi di calibrazione "vuoto".
        empty_mult        : moltiplicatore baseline per soglia EMPTY→STILL.
        move_mult         : moltiplicatore baseline per soglia STILL→MOTION.
        min_dwell_s       : dwell time minimo prima di cambiare stato (hysteresis).
        min_paths_for_decision : almeno N percorsi pieni prima di decidere.
    """

    def __init__(
        self,
        window_size: int = 100,
        ema_alpha: float = 0.25,
        baseline_seconds: float = 30.0,
        empty_mult: float = 1.5,
        move_mult: float = 4.0,
        min_dwell_s: float = 0.3,
        min_paths_for_decision: int = 1,
    ):
        if not 0 < ema_alpha <= 1.0:
            raise ValueError("ema_alpha deve essere in (0, 1]")
        if empty_mult >= move_mult:
            raise ValueError("empty_mult deve essere < move_mult")
        if window_size < 3:
            raise ValueError("window_size deve essere >= 3")

        self.window_size = window_size
        self.ema_alpha = ema_alpha
        self.baseline_seconds = baseline_seconds
        self.empty_mult = empty_mult
        self.move_mult = move_mult
        self.min_dwell_s = min_dwell_s
        self.min_paths_for_decision = min_paths_for_decision

        # buffers per (tx, rx) di ampl_mean
        self._buffers: dict[tuple[int, int], deque[float]] = {}
        # ultimo aggregato EMA
        self._ema: float = 0.0
        self._ema_initialized: bool = False

        # calibrazione
        self._calibration_start_t: float | None = None
        self._baseline_samples: list[float] = []   # aggregati raccolti durante calibrazione
        self._baseline_max: float = 0.0            # appreso a fine calibrazione
        self._calibrated: bool = False

        # state machine
        self._state: PresenceState = PresenceState.UNKNOWN
        self._state_since: float = time.time()
        self._candidate_state: PresenceState | None = None
        self._candidate_since: float = 0.0

        # rate / timestamp dell'ultimo frame ingerito
        self._last_frame_t: float = 0.0

    # ------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------

    def add_frame(self, frame: dict[str, Any]) -> None:
        """Ingerisce un frame CSI parsato.

        Il frame DEVE esporre `ampl_mean` (float) e (per multi-RX) i campi
        `tx_node` e `rx_node`. Se non presenti, finge percorso (0, 0).
        Frame senza ampl_mean vengono ignorati silenziosamente.
        """
        ampl_mean = frame.get("ampl_mean")
        if ampl_mean is None:
            return

        tx = int(frame.get("tx_node", 0))
        rx = int(frame.get("rx_node", 0))
        key = (tx, rx)

        buf = self._buffers.get(key)
        if buf is None:
            buf = deque(maxlen=self.window_size)
            self._buffers[key] = buf
        buf.append(float(ampl_mean))

        now = time.time()
        self._last_frame_t = now

        # Calibrazione: parte al primo frame, raccoglie aggregati per `baseline_seconds`
        if self._calibration_start_t is None:
            self._calibration_start_t = now

        agg_raw = self._aggregate_raw()
        if agg_raw is not None:
            # EMA
            if not self._ema_initialized:
                self._ema = agg_raw
                self._ema_initialized = True
            else:
                self._ema = self.ema_alpha * agg_raw + (1.0 - self.ema_alpha) * self._ema

            # Raccolta sample di calibrazione
            if not self._calibrated:
                elapsed = now - self._calibration_start_t
                if elapsed < self.baseline_seconds:
                    self._baseline_samples.append(agg_raw)
                else:
                    self._finalize_calibration()

        # Aggiorna state machine (anche se non calibrato, resta in UNKNOWN)
        self._update_state(now)

    def current_reading(self) -> PresenceReading:
        """Snapshot dello stato attuale (non modifica nulla)."""
        now = time.time()
        per_rx = self._per_rx_max_std()
        active = sum(1 for buf in self._buffers.values() if len(buf) >= 3)
        agg_raw = self._aggregate_raw() or 0.0
        progress = self._calibration_progress(now)
        confidence = self._confidence()

        return PresenceReading(
            state=self._state,
            confidence=confidence,
            intensity=self._ema if self._ema_initialized else 0.0,
            intensity_raw=agg_raw,
            baseline=self._baseline_max,
            duration_s=max(0.0, now - self._state_since),
            per_rx_intensity=per_rx,
            n_active_paths=active,
            n_total_paths=len(self._buffers),
            calibrated=self._calibrated,
            calibration_progress=progress,
            t=now,
        )

    def is_calibrated(self) -> bool:
        return self._calibrated

    def reset_calibration(self) -> None:
        """Forza una nuova fase di calibrazione (es. cambio stanza)."""
        self._calibration_start_t = None
        self._baseline_samples.clear()
        self._baseline_max = 0.0
        self._calibrated = False
        self._state = PresenceState.UNKNOWN
        self._state_since = time.time()
        self._candidate_state = None
        self._candidate_since = 0.0

    # ------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------

    def _per_rx_max_std(self) -> dict[int, float]:
        """Per ogni RX, std massima tra i percorsi (tx,rx) che hanno finestra >= 3.

        Ritorno un dict {rx: max_std_over_tx} così la UI può mostrare
        l'intensità per ricevitore.
        """
        out: dict[int, float] = {}
        for (tx, rx), buf in self._buffers.items():
            if len(buf) < 3:
                continue
            s = _std(buf)
            cur = out.get(rx, 0.0)
            if s > cur:
                out[rx] = s
        return out

    def _aggregate_raw(self) -> float | None:
        """Aggregato corrente: max std tra TUTTI i percorsi con buffer pieno >= 3.

        Ritorna None se nessun percorso ha sample sufficienti.
        """
        active = 0
        max_std = 0.0
        for buf in self._buffers.values():
            if len(buf) < 3:
                continue
            s = _std(buf)
            if s > max_std:
                max_std = s
            active += 1
        if active < self.min_paths_for_decision:
            return None
        return max_std

    def _finalize_calibration(self) -> None:
        """Calcola baseline_max dai sample raccolti."""
        if not self._baseline_samples:
            # Edge case: nessun sample raccolto (problema upstream)
            self._calibrated = False
            return

        # Uso il 95° percentile invece del max per essere robusto a outlier
        # (un'apertura di porta durante la calibrazione non ci frega).
        sorted_s = sorted(self._baseline_samples)
        idx = max(0, int(len(sorted_s) * 0.95) - 1)
        self._baseline_max = max(sorted_s[idx], 1e-6)  # evita divisione per zero
        self._calibrated = True

    def _calibration_progress(self, now: float) -> float:
        if self._calibrated:
            return 1.0
        if self._calibration_start_t is None:
            return 0.0
        elapsed = now - self._calibration_start_t
        return max(0.0, min(1.0, elapsed / self.baseline_seconds))

    def _decide_state(self) -> PresenceState:
        """Decide lo stato istantaneo (senza hysteresis) dato l'EMA corrente."""
        if not self._calibrated:
            return PresenceState.UNKNOWN

        # Conta percorsi attivi
        active = sum(1 for buf in self._buffers.values() if len(buf) >= 3)
        if active < self.min_paths_for_decision:
            return PresenceState.UNKNOWN

        threshold_empty = self._baseline_max * self.empty_mult
        threshold_move = self._baseline_max * self.move_mult

        if self._ema < threshold_empty:
            return PresenceState.EMPTY
        if self._ema < threshold_move:
            return PresenceState.STILL
        return PresenceState.MOTION

    def _update_state(self, now: float) -> None:
        """Applica hysteresis: lo stato cambia solo se nuova decisione persiste
        per >= min_dwell_s."""
        instant = self._decide_state()

        if instant == self._state:
            self._candidate_state = None
            return

        if self._candidate_state != instant:
            self._candidate_state = instant
            self._candidate_since = now
            return

        # Stesso candidato di prima: controlla dwell
        if (now - self._candidate_since) >= self.min_dwell_s:
            self._state = instant
            self._state_since = now
            self._candidate_state = None

    def _confidence(self) -> float:
        """Confidence 0..1 basata sulla distanza dalla soglia più vicina."""
        if not self._calibrated:
            return 0.0
        if self._state == PresenceState.UNKNOWN:
            return 0.0

        t_empty = self._baseline_max * self.empty_mult
        t_move = self._baseline_max * self.move_mult

        if self._state == PresenceState.EMPTY:
            # Più sono sotto t_empty, più sono sicuro
            margin = t_empty - self._ema
            return float(min(1.0, max(0.0, margin / max(t_empty, 1e-6))))
        if self._state == PresenceState.STILL:
            # Sicuro al centro tra le due soglie
            mid = (t_empty + t_move) / 2.0
            spread = (t_move - t_empty) / 2.0
            if spread <= 0:
                return 0.5
            d = abs(self._ema - mid) / spread
            return float(max(0.0, 1.0 - d))
        # MOTION: più sopra t_move, più sicuro (saturo a 2× soglia)
        excess = self._ema - t_move
        denom = max(t_move, 1e-6)
        return float(min(1.0, max(0.0, excess / denom)))


# ============================================================
# Stat helper (no numpy dependency for the hot path)
# ============================================================
def _std(values) -> float:
    """Standard deviation (population) — robust to short windows."""
    n = len(values)
    if n < 2:
        return 0.0
    m = sum(values) / n
    return math.sqrt(sum((v - m) ** 2 for v in values) / n)
