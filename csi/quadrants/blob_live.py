"""
blob_live.py — BlobEstimator: stima posizione 2D variance-weighted, NO ML.

Algoritmo:
    1. Per ogni percorso (tx_node, rx_node) deque scorrevole di ampl_mean.
    2. Per ogni RX, varianza massima sui suoi percorsi (max su tutti i tx).
       Razionale: la varianza è proxy di "quanto il canale verso questo RX
       è perturbato in questa finestra"; il max preserva la traccia anche
       se solo un percorso è affetto.
    3. Posizione = centroide pesato delle posizioni RX, con peso = varianza
       (eventualmente residuale rispetto al baseline appreso a stanza vuota).
    4. Incertezza (σx, σy) = spread pesato delle distanze dalle posizioni RX.
    5. Mappatura su griglia di celle via Gaussian 2D → cell_probabilities.

Non c'è training, quindi non c'è overfitting. È il **baseline** della
Funzionalità 2: funziona da subito appena hai >= 2 ESP32 e ne conosci la
posizione. Con 3 ESP32 in triangolo dà già una localizzazione utile.

Limiti onesti:
    - L'accuratezza non è cm-level; tipicamente 0.5-1.5 m con 3 RX in stanza
      6×5 m. Sufficiente per "quale quadrante è occupato".
    - Se la persona è esattamente al centro geometrico dei RX, la stima è
      ambigua (massimo dell'incertezza).
    - Se la varianza globale è sotto soglia (`min_intensity`), non emette
      stima: significa "stanza vuota o persona troppo lontana".

Per Funzionalità 2 "avanzata" con apprendimento, vedi quadrants/regressor.py.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any


# ============================================================
# Output dataclasses
# ============================================================
@dataclass
class BlobEstimate:
    """Stima 2D della posizione del blob (in metri, frame della stanza)."""
    x: float                       # metri
    y: float
    x_std: float                   # incertezza (1σ)
    y_std: float
    intensity: float               # somma varianze (proxy di "quanta perturbazione")
    confidence: float              # 0..1
    n_active_rx: int               # quanti RX hanno contribuito
    t: float                       # timestamp UNIX

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "x_std": round(self.x_std, 3),
            "y_std": round(self.y_std, 3),
            "intensity": round(self.intensity, 4),
            "confidence": round(self.confidence, 3),
            "n_active_rx": self.n_active_rx,
            "t": round(self.t, 3),
        }


@dataclass
class CellProbabilities:
    """Distribuzione probabilità sulla griglia."""
    rows: int
    cols: int
    probas: dict[str, float]       # "rXcY" → prob
    predicted: str                 # cella con prob massima
    confidence: float              # = max(probas)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "cols": self.cols,
            "probas": {k: round(v, 4) for k, v in self.probas.items()},
            "predicted": self.predicted,
            "confidence": round(self.confidence, 3),
        }


# ============================================================
# Estimator
# ============================================================
class BlobEstimator:
    """Stima la posizione di una persona pesando le varianze CSI per RX noto.

    Parametri:
        rx_positions    : lista [(x, y), ...] in metri, indice = rx_node.
                          DEVE coprire tutti i rx_node che possono apparire
                          nei frame; rx_node fuori range vengono ignorati.
        room_size       : (W, L) in metri della stanza.
        grid_shape      : (rows, cols) della griglia per cell_probabilities.
        window_frames   : sample per percorso (default 100 = ~1s @ 100 Hz).
        min_intensity   : soglia varianza globale sotto cui NON emette stima
                          (filtra "stanza vuota").
        baseline_alpha  : EMA per baseline auto-appreso (0 = disabilitato).
                          Se >0, sottrae lo spettro di varianza appreso a
                          regime quiet (utile per cancellare rumore di
                          fondo).
    """

    def __init__(
        self,
        rx_positions: list[tuple[float, float]],
        room_size: tuple[float, float] = (6.0, 5.0),
        grid_shape: tuple[int, int] = (4, 4),
        window_frames: int = 100,
        min_intensity: float = 1e-3,
        baseline_alpha: float = 0.0,
        baseline_seconds: float = 0.0,
        variance_power: float = 1.0,
    ):
        if not rx_positions:
            raise ValueError("rx_positions non può essere vuoto")
        if len(rx_positions) < 2:
            # Per essere onesti: con 1 RX la stima è degenere (il centroide
            # coincide con il RX stesso). Permettiamo, ma marcato come
            # "n_active_rx==1" così il consumatore può ignorarlo.
            pass
        if room_size[0] <= 0 or room_size[1] <= 0:
            raise ValueError("room_size deve essere positivo")
        if grid_shape[0] < 1 or grid_shape[1] < 1:
            raise ValueError("grid_shape deve essere >= 1×1")
        if window_frames < 3:
            raise ValueError("window_frames deve essere >= 3")
        if not 0.0 <= baseline_alpha <= 1.0:
            raise ValueError("baseline_alpha in [0, 1]")
        if variance_power < 0.1 or variance_power > 10.0:
            raise ValueError("variance_power deve essere in [0.1, 10.0]")

        self.rx_positions = list(rx_positions)
        self.room_w, self.room_l = float(room_size[0]), float(room_size[1])
        self.rows, self.cols = int(grid_shape[0]), int(grid_shape[1])
        self.window_frames = window_frames
        self.min_intensity = min_intensity
        self.baseline_alpha = baseline_alpha
        self.baseline_seconds = baseline_seconds
        # variance_power > 1 emphasizes the most-active RX (combatte il "blob
        # bloccato al centro" quando le varianze dei 3 RX sono simili).
        # power=2 → varianza al quadrato come peso; power=3 → ancora più aggressivo.
        self.variance_power = float(variance_power)

        self._buffers: dict[tuple[int, int], deque[float]] = {}
        # baseline appreso per RX (varianza tipica a stanza vuota)
        self._baseline_var: dict[int, float] = {}
        self._baseline_initialized: dict[int, bool] = {}
        self._calibration_start_t: float | None = None
        self._calibrated: bool = baseline_seconds == 0.0  # se 0, niente calibrazione

    # ------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------
    def add_frame(self, frame: dict[str, Any]) -> None:
        ampl_mean = frame.get("ampl_mean")
        if ampl_mean is None:
            return
        tx = int(frame.get("tx_node", 0))
        rx = int(frame.get("rx_node", 0))
        if rx >= len(self.rx_positions):
            return
        key = (tx, rx)
        buf = self._buffers.get(key)
        if buf is None:
            buf = deque(maxlen=self.window_frames)
            self._buffers[key] = buf
        buf.append(float(ampl_mean))

        # Calibrazione baseline (se richiesta)
        if self.baseline_seconds > 0.0 and not self._calibrated:
            now = time.time()
            if self._calibration_start_t is None:
                self._calibration_start_t = now
            elapsed = now - self._calibration_start_t
            if elapsed < self.baseline_seconds:
                # Aggiorna baseline_var per questo RX (EMA con coeff alto)
                v = _var(buf)
                cur = self._baseline_var.get(rx, v)
                alpha = max(self.baseline_alpha, 0.2)
                self._baseline_var[rx] = alpha * v + (1 - alpha) * cur
                self._baseline_initialized[rx] = True
            else:
                self._calibrated = True

    # ------------------------------------------------------------
    # Status
    # ------------------------------------------------------------
    def is_calibrated(self) -> bool:
        return self._calibrated

    def calibration_progress(self) -> float:
        if self._calibrated:
            return 1.0
        if self._calibration_start_t is None or self.baseline_seconds <= 0:
            return 0.0
        elapsed = time.time() - self._calibration_start_t
        return max(0.0, min(1.0, elapsed / self.baseline_seconds))

    def reset_calibration(self) -> None:
        self._baseline_var.clear()
        self._baseline_initialized.clear()
        self._calibration_start_t = None
        self._calibrated = self.baseline_seconds == 0.0

    # ------------------------------------------------------------
    # Estimation
    # ------------------------------------------------------------
    def _per_rx_variance(self) -> dict[int, float]:
        """Per ogni RX, max varianza tra i percorsi tx → rx attivi.

        Sottrae il baseline_var appreso (clamp a 0).
        """
        out: dict[int, float] = {}
        for (tx, rx), buf in self._buffers.items():
            if len(buf) < 3:
                continue
            v = _var(buf)
            base = self._baseline_var.get(rx, 0.0) if self._calibrated else 0.0
            residual = max(0.0, v - base)
            if residual > out.get(rx, 0.0):
                out[rx] = residual
        return out

    def estimate(self) -> BlobEstimate | None:
        """Ritorna stima blob 2D oppure None se segnale insufficiente."""
        per_rx_raw = self._per_rx_variance()
        if not per_rx_raw:
            return None
        # Filtra RX con index valido
        per_rx_raw = {rx: v for rx, v in per_rx_raw.items() if rx < len(self.rx_positions)}
        if not per_rx_raw:
            return None

        total_raw = sum(per_rx_raw.values())
        if total_raw < self.min_intensity:
            return None

        # Applica variance_power per amplificare le differenze tra RX.
        # NB: ai fini della soglia min_intensity usiamo la varianza raw,
        # ma il peso del centroide usa varianza^power.
        if self.variance_power == 1.0:
            per_rx = per_rx_raw
        else:
            per_rx = {rx: v ** self.variance_power for rx, v in per_rx_raw.items()}

        total = sum(per_rx.values())
        if total <= 0:
            return None

        # Centroide pesato
        x_est = 0.0
        y_est = 0.0
        for rx, w in per_rx.items():
            rx_x, rx_y = self.rx_positions[rx]
            x_est += rx_x * w
            y_est += rx_y * w
        x_est /= total
        y_est /= total

        # Spread (incertezza 1σ) calcolato come std pesata delle distanze
        x_var = 0.0
        y_var = 0.0
        for rx, w in per_rx.items():
            rx_x, rx_y = self.rx_positions[rx]
            x_var += w * (rx_x - x_est) ** 2
            y_var += w * (rx_y - y_est) ** 2
        x_std = math.sqrt(x_var / total) if total > 0 else self.room_w / 4
        y_std = math.sqrt(y_var / total) if total > 0 else self.room_l / 4
        # Clamp incertezza in range ragionevole
        x_std = max(0.1, min(self.room_w / 2, x_std))
        y_std = max(0.1, min(self.room_l / 2, y_std))

        # Clamp posizione dentro la stanza
        x_est = max(0.0, min(self.room_w, x_est))
        y_est = max(0.0, min(self.room_l, y_est))

        # Confidence: ratio segnale / min_intensity, saturato (su raw varianza)
        confidence = min(1.0, total_raw / (self.min_intensity * 10.0))

        return BlobEstimate(
            x=x_est, y=y_est,
            x_std=x_std, y_std=y_std,
            intensity=total_raw,
            confidence=confidence,
            n_active_rx=len(per_rx),
            t=time.time(),
        )

    def cell_probabilities(self, estimate: BlobEstimate | None = None
                           ) -> CellProbabilities | None:
        """Converte una stima blob in distribuzione di probabilità per cella.

        Usa Gaussian 2D centrata su (x,y) con (σx, σy) della stima.
        """
        if estimate is None:
            estimate = self.estimate()
        if estimate is None:
            return None

        probas: dict[str, float] = {}
        total = 0.0
        for r in range(self.rows):
            for c in range(self.cols):
                cx, cy = self._cell_center(r, c)
                dx = (cx - estimate.x) / max(estimate.x_std, 1e-6)
                dy = (cy - estimate.y) / max(estimate.y_std, 1e-6)
                p = math.exp(-0.5 * (dx * dx + dy * dy))
                probas[f"r{r}c{c}"] = p
                total += p

        if total <= 0:
            return None
        for k in probas:
            probas[k] /= total

        predicted = max(probas, key=probas.get)
        return CellProbabilities(
            rows=self.rows,
            cols=self.cols,
            probas=probas,
            predicted=predicted,
            confidence=probas[predicted],
        )

    def _cell_center(self, r: int, c: int) -> tuple[float, float]:
        """Centro cella in metri (frame della stanza, Cartesian).

        Convenzione griglia: r=0 è la riga IN ALTO della stanza (vista top-down),
        coerente con `csi.quadrants.regressor.grid_label_to_xy`.
        Per esempio in stanza 6×5 m con griglia 4×4:
            r0c0 = top-left  → (0.75, 4.375) m
            r3c0 = bot-left  → (0.75, 0.625) m
        """
        x = (c + 0.5) * self.room_w / self.cols
        y = (self.rows - r - 0.5) * self.room_l / self.rows
        return x, y


# ============================================================
# Stat helpers
# ============================================================
def _mean(values) -> float:
    n = len(values)
    if n == 0:
        return 0.0
    return sum(values) / n


def _var(values) -> float:
    """Population variance — robust to short windows."""
    n = len(values)
    if n < 2:
        return 0.0
    m = sum(values) / n
    return sum((v - m) ** 2 for v in values) / n
