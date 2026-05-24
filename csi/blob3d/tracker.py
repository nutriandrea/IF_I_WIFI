"""
tracker.py — Tracker 3D best-effort per blob della persona.

NOTA REALISTICA (ribadita per onestà tecnica)
============================================================================
Con 3 ESP32 piazzati alla STESSA quota (es. 1 m da terra), la coordinata Z
del corpo è osservabile solo in modo MOLTO LIMITATO. Il segnale CSI non
contiene direttamente informazione sull'altezza; ricaviamo z da una
heuristic sul rapporto di energia tra subcarrier alti (~lunghezze d'onda
corte, più sensibili a torso/testa in piedi) e bassi (più sensibili a
movimenti ampi a terra).

Quindi questa pipeline produce **3 macro-classi di altezza** (LOW / MID /
HIGH ≈ a terra / seduto / in piedi), NON una stima in centimetri.

Se vuoi vera pose estimation 3D devi avere:
  - >= 3 RX a quote DIVERSE (es. uno a 50 cm, uno a 1 m, uno a 1.8 m), oppure
  - hardware MIMO multi-antenna per RX (es. Intel 5300, Atheros AR9580), oppure
  - dataset MM-Fi pre-registrato + un transformer (csi2pointcloud).

Quanto sopra è esplicitamente FUORI SCOPE di questo progetto.
============================================================================

Architettura:
    (x, y)  ←  da csi.quadrants.blob_live.BlobEstimator
               o csi.quadrants.regressor.PositionRegressor (se validato)
    z_class ←  csi.blob3d.HeightHeuristic
    z_meters ← mapping discreto su 3 livelli con dwell hysteresis
    (x, y, z) → KalmanFilter3D constant-velocity per smoothing

API:
    tracker = Blob3DTracker(room_size=(6,5,3), height_levels=(0.5,1.2,1.8))
    tracker.add_frame(frame)                  # ingerisce CSI per height heuristic
    tracker.update_position(x, y, x_std, y_std)  # da pipeline 2D
    estimate = tracker.current()              # Blob3DEstimate o None

Output:
    Blob3DEstimate{x, y, z, x_std, y_std, z_std, z_class, confidence, t}
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any

# numpy opzionale (usato per Kalman matriciale 3D)
_NUMPY_OK = False
try:
    import numpy as _np  # type: ignore
    _NUMPY_OK = True
except ImportError:
    _np = None  # type: ignore


# ============================================================
# Height class enum
# ============================================================
class HeightClass(str, Enum):
    UNKNOWN = "UNKNOWN"
    LOW = "LOW"      # ≈ a terra / sdraiato
    MID = "MID"      # ≈ seduto
    HIGH = "HIGH"    # ≈ in piedi


# ============================================================
# Output dataclass
# ============================================================
@dataclass
class Blob3DEstimate:
    x: float
    y: float
    z: float
    x_std: float
    y_std: float
    z_std: float
    z_class: HeightClass
    confidence: float       # confidenza combinata 2D + height
    smoothed: bool
    t: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "z": round(self.z, 3),
            "x_std": round(self.x_std, 3),
            "y_std": round(self.y_std, 3),
            "z_std": round(self.z_std, 3),
            "z_class": self.z_class.value,
            "confidence": round(self.confidence, 3),
            "smoothed": self.smoothed,
            "t": round(self.t, 3),
        }


# ============================================================
# Height heuristic
# ============================================================
class HeightHeuristic:
    """Stima classe altezza dal rapporto energia sub-banda alta / bassa.

    Razionale (semplificato, fisica-aware):
      Le subcarrier WiFi 802.11n a 2.4 GHz occupano ~20 MHz attorno a 2412 MHz.
      Le subcarrier alte (bordo banda) hanno lunghezza d'onda leggermente
      minore e tendono ad essere più sensibili a perturbazioni "fini"
      (testa, mani). Le basse a perturbazioni "ampie".

      Su 64 subcarrier OFDM: split in low_band [4..28] e high_band [36..60]
      (saltiamo DC e i guard bins).

    Output:
      - ratio = mean_var(high) / mean_var(low) (con baseline subtract opzionale)
      - low/mid/high con due soglie configurabili
      - hysteresis via dwell time

    Limiti onesti:
      - Questa heuristic è approssimata. Funziona MEGLIO quando si confronta
        la stessa persona in posizioni diverse (relativo), che in absoluto.
      - Su single-RX è quasi sempre rumorosa; con 3 RX migliorerebbe ma resta
        macro-classe.
    """

    def __init__(
        self,
        n_subcarriers_expected: int = 64,
        low_band: tuple[int, int] = (4, 28),     # inclusivi
        high_band: tuple[int, int] = (36, 60),
        window_frames: int = 100,
        threshold_low: float = 0.7,    # ratio < → LOW
        threshold_high: float = 1.4,   # ratio > → HIGH
        min_dwell_s: float = 0.5,
        min_frames: int = 30,
    ):
        if low_band[1] >= high_band[0]:
            raise ValueError("low_band e high_band non devono sovrapporsi")
        self.n_sub = n_subcarriers_expected
        self.low_band = low_band
        self.high_band = high_band
        self.window_frames = window_frames
        self.threshold_low = threshold_low
        self.threshold_high = threshold_high
        self.min_dwell_s = min_dwell_s
        self.min_frames = min_frames

        # Buffer per subcarrier indipendentemente — usiamo deque parallele
        self._low_amps: deque[float] = deque(maxlen=window_frames)
        self._high_amps: deque[float] = deque(maxlen=window_frames)

        self._state: HeightClass = HeightClass.UNKNOWN
        self._state_since: float = time.time()
        self._candidate: HeightClass | None = None
        self._candidate_since: float = 0.0

    def add_frame(self, frame: dict[str, Any]) -> None:
        """Ingerisce frame CSI. Cerca campo 'csi' (lista di dict {ampl,...})."""
        csi = frame.get("csi")
        if not isinstance(csi, list) or not csi:
            return
        # Estrae ampiezze per subcarrier; tollerante a diversi schemi
        amps = []
        for c in csi:
            if isinstance(c, dict):
                amp = c.get("ampl")
                if amp is None:
                    # fallback: derive da real/imag
                    r = c.get("real", 0); im = c.get("imag", 0)
                    amp = math.hypot(r, im)
                amps.append(float(amp))
            else:
                # già numero
                amps.append(float(c))
        if not amps:
            return
        n = len(amps)
        lo0, lo1 = self.low_band
        hi0, hi1 = self.high_band
        if hi1 >= n:
            # frame con meno subcarrier del previsto → riscaliamo le bande
            scale = n / self.n_sub
            lo0 = int(lo0 * scale); lo1 = max(lo0 + 1, int(lo1 * scale))
            hi0 = max(lo1 + 1, int(hi0 * scale)); hi1 = max(hi0 + 1, int(hi1 * scale))
            if hi1 >= n:
                hi1 = n - 1
            if hi0 >= hi1 or lo0 >= lo1:
                return

        low_mean = sum(amps[lo0:lo1 + 1]) / max(1, lo1 - lo0 + 1)
        high_mean = sum(amps[hi0:hi1 + 1]) / max(1, hi1 - hi0 + 1)
        self._low_amps.append(low_mean)
        self._high_amps.append(high_mean)

        self._update_state()

    def _ratio_high_over_low(self) -> float | None:
        if len(self._low_amps) < self.min_frames or len(self._high_amps) < self.min_frames:
            return None
        low_var = _variance(self._low_amps)
        high_var = _variance(self._high_amps)
        if low_var < 1e-6:
            return None
        return high_var / low_var

    def _decide_state(self) -> HeightClass:
        ratio = self._ratio_high_over_low()
        if ratio is None:
            return HeightClass.UNKNOWN
        if ratio < self.threshold_low:
            return HeightClass.LOW
        if ratio > self.threshold_high:
            return HeightClass.HIGH
        return HeightClass.MID

    def _update_state(self) -> None:
        now = time.time()
        instant = self._decide_state()
        if instant == self._state:
            self._candidate = None
            return
        if self._candidate != instant:
            self._candidate = instant
            self._candidate_since = now
            return
        if (now - self._candidate_since) >= self.min_dwell_s:
            self._state = instant
            self._state_since = now
            self._candidate = None

    def current_class(self) -> HeightClass:
        return self._state

    def current_ratio(self) -> float | None:
        return self._ratio_high_over_low()


# ============================================================
# Kalman 3D (constant-velocity)
# ============================================================
class KalmanFilter3D:
    """Filtro Kalman 3D constant-velocity per smoothing (x, y, z).

    Stato: [x, y, z, vx, vy, vz]^T (6D). Misura: [x, y, z]^T (3D).
    dt adattivo.
    """

    def __init__(self, q_pos: float = 1e-4, q_vel: float = 1e-3,
                 std_clip: tuple[float, float] = (0.02, 0.7)):
        if not _NUMPY_OK:
            raise RuntimeError("KalmanFilter3D richiede numpy")
        self._q_pos = q_pos
        self._q_vel = q_vel
        self._std_min, self._std_max = std_clip
        self._dt = 1.0

        # F (6x6)
        self.F = _np.eye(6, dtype=_np.float64)
        self.F[0, 3] = self._dt
        self.F[1, 4] = self._dt
        self.F[2, 5] = self._dt

        # H (3x6): osservo posizione
        self.H = _np.zeros((3, 6), dtype=_np.float64)
        self.H[0, 0] = 1.0
        self.H[1, 1] = 1.0
        self.H[2, 2] = 1.0

        self.x = None
        self.P = None
        self._last_t: float | None = None
        self._initialized = False
        self._rebuild_Q()

    def _rebuild_Q(self) -> None:
        dt = self._dt
        dt2, dt3, dt4 = dt * dt, dt ** 3, dt ** 4
        qp, qv = self._q_pos, self._q_vel
        Q = _np.zeros((6, 6), dtype=_np.float64)
        for i in range(3):
            Q[i, i] = dt4 * qp
            Q[i + 3, i + 3] = dt2 * qp + dt2 * qv
            Q[i, i + 3] = dt3 * qp
            Q[i + 3, i] = dt3 * qp
        self.Q = Q

    def init(self, x0: float, y0: float, z0: float, P0: float = 0.1) -> None:
        self.x = _np.array([x0, y0, z0, 0.0, 0.0, 0.0], dtype=_np.float64)
        self.P = _np.eye(6, dtype=_np.float64) * P0
        self._initialized = True
        self._last_t = None

    def update(self, z: tuple[float, float, float],
               R: tuple[float, float, float] | None = None,
               t: float | None = None
               ) -> tuple[float, float, float, float, float, float]:
        """Predict + update con nuova misura (x, y, z).

        Ritorna (x, y, z, σx, σy, σz).
        """
        if not self._initialized:
            self.init(z[0], z[1], z[2])
            return (z[0], z[1], z[2], self._std_min, self._std_min, self._std_min)

        if t is not None and self._last_t is not None:
            dt = max(0.01, min(5.0, t - self._last_t))
            if abs(dt - self._dt) > 0.005:
                self._dt = dt
                self.F[0, 3] = dt; self.F[1, 4] = dt; self.F[2, 5] = dt
                self._rebuild_Q()
        if t is not None:
            self._last_t = t

        # predict
        self.x = self.F @ self.x
        self.P = self.F @ self.P @ self.F.T + self.Q

        # update
        z_vec = _np.array([z[0], z[1], z[2]], dtype=_np.float64)
        if R is not None:
            R_mat = _np.diag([max(R[0], 1e-6), max(R[1], 1e-6), max(R[2], 1e-6)])
        else:
            R_mat = _np.eye(3) * self._q_pos * 100
        y_res = z_vec - self.H @ self.x
        S = self.H @ self.P @ self.H.T + R_mat
        K = self.P @ self.H.T @ _np.linalg.inv(S)
        self.x = self.x + K @ y_res
        self.P = self.P - K @ self.H @ self.P

        return (
            float(self.x[0]), float(self.x[1]), float(self.x[2]),
            self._clip_std(float(_np.sqrt(self.P[0, 0]))),
            self._clip_std(float(_np.sqrt(self.P[1, 1]))),
            self._clip_std(float(_np.sqrt(self.P[2, 2]))),
        )

    def _clip_std(self, s: float) -> float:
        return min(self._std_max, max(self._std_min, s))

    def reset(self) -> None:
        self.x = None
        self.P = None
        self._initialized = False
        self._last_t = None


# ============================================================
# Blob3DTracker
# ============================================================
class Blob3DTracker:
    """Compone HeightHeuristic + KalmanFilter3D + mapping z_class → z_meters.

    Uso tipico (chiamato dal ws_server):
        tracker = Blob3DTracker(room_size=(6,5,3))
        tracker.add_frame(csi_frame)                          # ad ogni frame
        tracker.update_position(x, y, x_std, y_std)           # dalla pipeline 2D
        est = tracker.current()
        if est is not None:
            ws.send({"type":"position_3d", **est.to_dict()})
    """

    DEFAULT_LEVELS_METERS = {
        HeightClass.LOW: 0.4,
        HeightClass.MID: 1.0,
        HeightClass.HIGH: 1.7,
    }
    DEFAULT_LEVEL_STD = 0.3  # incertezza intrinseca dell'heuristic

    def __init__(
        self,
        room_size: tuple[float, float, float] = (6.0, 5.0, 3.0),
        height_levels: dict[HeightClass, float] | None = None,
        smoother: KalmanFilter3D | None = None,
        height_heuristic: HeightHeuristic | None = None,
        smoothing: bool = True,
    ):
        self.room_w, self.room_l, self.room_h = (
            float(room_size[0]), float(room_size[1]), float(room_size[2]),
        )
        self.height_levels = dict(height_levels or self.DEFAULT_LEVELS_METERS)
        self.heuristic = height_heuristic or HeightHeuristic()
        if smoothing:
            self.smoother = smoother or KalmanFilter3D() if _NUMPY_OK else None
        else:
            self.smoother = None
        self._last_xy: tuple[float, float, float, float] | None = None
        self._last_xy_t: float = 0.0

    def add_frame(self, frame: dict[str, Any]) -> None:
        """Solo per la height heuristic (richiede campo CSI per-subcarrier)."""
        self.heuristic.add_frame(frame)

    def update_position(self, x: float, y: float,
                        x_std: float, y_std: float,
                        t: float | None = None) -> None:
        """Aggiorna la misura 2D dalla pipeline (blob_live o regressor)."""
        if t is None:
            t = time.time()
        self._last_xy = (x, y, x_std, y_std)
        self._last_xy_t = t

    def current(self) -> Blob3DEstimate | None:
        if self._last_xy is None:
            return None
        x, y, x_std, y_std = self._last_xy
        z_class = self.heuristic.current_class()
        z_meters = self.height_levels.get(z_class)
        if z_meters is None:
            # UNKNOWN: prendi mezzo della stanza, std alta
            z_meters = self.room_h * 0.5
            z_std_base = self.room_h * 0.4
        else:
            z_std_base = self.DEFAULT_LEVEL_STD

        # Apply smoothing
        if self.smoother is not None:
            sx, sy, sz, ssx, ssy, ssz = self.smoother.update(
                z=(x, y, z_meters),
                R=(x_std ** 2, y_std ** 2, z_std_base ** 2),
                t=self._last_xy_t,
            )
            smoothed = True
        else:
            sx, sy, sz = x, y, z_meters
            ssx, ssy, ssz = x_std, y_std, z_std_base
            smoothed = False

        # Confidence combinata: ridotta da incertezza
        avg_std = (ssx / max(self.room_w, 1e-6) +
                   ssy / max(self.room_l, 1e-6) +
                   ssz / max(self.room_h, 1e-6)) / 3.0
        confidence = max(0.0, 1.0 - avg_std * 4.0)

        return Blob3DEstimate(
            x=sx, y=sy, z=sz,
            x_std=ssx, y_std=ssy, z_std=ssz,
            z_class=z_class,
            confidence=confidence,
            smoothed=smoothed,
            t=self._last_xy_t,
        )

    def reset(self) -> None:
        self._last_xy = None
        if self.smoother is not None:
            self.smoother.reset()


# ============================================================
# Helpers
# ============================================================
def _variance(values) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = sum(values) / n
    return sum((v - m) ** 2 for v in values) / n
