"""
regressor.py — PositionRegressor + KalmanFilter2D (opt-in ML path).

Versione pulita ed estratta da csi_ml.py, con UNA DIFFERENZA CRITICA:
    aggiunge `cross_validate_loo_cell()` che valida il modello con
    leave-one-cell-out CV. Se l'errore mediano per cella esclusa è > soglia,
    il modello è rifiutato e il loader ricade sul baseline no-ML
    (csi.quadrants.blob_live.BlobEstimator).

Razionale: la classification per posizione discreta (vecchio `CSIClassifier`)
overfittava perché la cross-validation random non testa la generalizzazione
SPAZIALE. Il LOO-cell rimuove TUTTI i frame di una cella dal training e
verifica se il modello sa interpolarla — se non sa, il modello vale zero.

Questo è il **gate anti-problema-3** del plan.

Architettura:
    Frame → feature extraction (riusa csi_ml: per-source o standard)
          → RandomForestRegressor → (x, y) ∈ [0, 1]²
          → KalmanFilter2D → (x, y, x_std, y_std) smoothed

L'orientazione di (x, y):
    x = col / cols (sinistra → destra)
    y = 1 - row / rows (alto → basso, r0 = top)
    Coerente con i mapping in csi/csi_ml.py:_grid_label_to_xy.

Dipendenze opzionali: scikit-learn + joblib + numpy. Se mancano,
l'import del modulo non fallisce ma l'istanziazione raises.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from collections import deque
from dataclasses import dataclass, asdict
from statistics import mean, median
from typing import Any

# Optional dependencies — graceful degradation
_SKLEARN_OK = False
_RF_REGRESSOR = None
try:
    from sklearn.ensemble import RandomForestRegressor as _RF_REGRESSOR  # type: ignore
    _SKLEARN_OK = True
except ImportError:
    _RF_REGRESSOR = None

_NUMPY_OK = False
try:
    import numpy as _np  # type: ignore
    _NUMPY_OK = True
except ImportError:
    _np = None  # type: ignore

_JOBLIB_OK = False
try:
    import joblib as _joblib  # type: ignore
    _JOBLIB_OK = True
except ImportError:
    _joblib = None


# Riusa l'estrazione feature dal modulo esistente (per non duplicare 100+ LOC).
# In futuro questi helper andranno migrati in un csi/features.py dedicato.
from csi.csi_ml import (
    csi_window_to_vector,
    csi_window_to_vector_per_source,
    _generate_source_feature_names,
    _prefix_from_value,
)


# ============================================================
# Paths default
# ============================================================
_MODEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MODEL_PATH = os.path.join(_MODEL_DIR, "csi_quadrants_regressor.joblib")
DEFAULT_CONFIG_PATH = os.path.join(_MODEL_DIR, "csi_quadrants_regressor.json")


# ============================================================
# Grid label helpers
# ============================================================
_GRID_LABEL_RE = re.compile(r"^r(\d+)c(\d+)$")


def parse_grid_labels(labels: list[str]) -> tuple[int, int] | None:
    """Trova (rows, cols) dalla collezione di label 'rXcY'."""
    rows, cols = -1, -1
    found_any = False
    for lbl in labels:
        m = _GRID_LABEL_RE.match(lbl)
        if m:
            found_any = True
            r, c = int(m.group(1)), int(m.group(2))
            rows = max(rows, r + 1)
            cols = max(cols, c + 1)
    if not found_any:
        return None
    return rows, cols


def grid_label_to_xy(label: str, rows: int, cols: int) -> tuple[float, float] | None:
    """rXcY → (x, y) ∈ [0, 1]². y=1 in alto (r0 = top)."""
    m = _GRID_LABEL_RE.match(label)
    if not m:
        return None
    r, c = int(m.group(1)), int(m.group(2))
    if not (0 <= r < rows and 0 <= c < cols):
        return None
    x = (c + 0.5) / cols
    y = 1.0 - (r + 0.5) / rows
    return x, y


def grid_labels_to_xy_map(labels: list[str]) -> dict[str, tuple[float, float]]:
    dims = parse_grid_labels(labels)
    if dims is None:
        return {}
    rows, cols = dims
    out: dict[str, tuple[float, float]] = {}
    for lbl in labels:
        xy = grid_label_to_xy(lbl, rows, cols)
        if xy:
            out[lbl] = xy
    return out


# ============================================================
# KalmanFilter2D
# ============================================================
class KalmanFilter2D:
    """Filtro Kalman 2D constant-velocity per smoothing posizione.

    Stato: [x, y, vx, vy]^T. Misura: [x, y]^T.
    dt adattivo: si autoregola in base a `t` passato a `update()`.

    Parametri principali:
        q_pos : rumore di processo per posizione (più piccolo = più smooth)
        q_vel : rumore di processo per velocità  (più alto = più reattivo)
    """

    def __init__(self, q_pos: float = 1e-4, q_vel: float = 1e-3,
                 std_clip: tuple[float, float] = (0.02, 0.5)):
        if not _NUMPY_OK:
            raise RuntimeError("KalmanFilter2D richiede numpy")

        self._q_pos = q_pos
        self._q_vel = q_vel
        self._std_min, self._std_max = std_clip

        # F: transizione constant-velocity (dt impostato dinamicamente)
        self.F = _np.eye(4, dtype=_np.float64)
        self._dt = 1.0
        self.F[0, 2] = self._dt
        self.F[1, 3] = self._dt

        # H: osservo solo posizione
        self.H = _np.zeros((2, 4), dtype=_np.float64)
        self.H[0, 0] = 1.0
        self.H[1, 1] = 1.0

        self.x = None  # stato
        self.P = None  # covarianza
        self._last_t: float | None = None
        self._initialized = False
        self._rebuild_Q()

    def _rebuild_Q(self) -> None:
        dt = self._dt
        dt2, dt3, dt4 = dt * dt, dt ** 3, dt ** 4
        self.Q = _np.array([
            [dt4 * self._q_pos, 0,                 dt3 * self._q_pos, 0               ],
            [0,                 dt4 * self._q_pos, 0,                 dt3 * self._q_pos],
            [dt3 * self._q_pos, 0,                 dt2 * self._q_pos, 0               ],
            [0,                 dt3 * self._q_pos, 0,                 dt2 * self._q_pos],
        ], dtype=_np.float64)
        self.Q[2, 2] += dt2 * self._q_vel
        self.Q[3, 3] += dt2 * self._q_vel

    def init(self, x0: float, y0: float, P0: float = 0.1) -> None:
        self.x = _np.array([x0, y0, 0.0, 0.0], dtype=_np.float64)
        self.P = _np.eye(4, dtype=_np.float64) * P0
        self._initialized = True
        self._last_t = None

    def update(self, z: tuple[float, float],
               R: tuple[float, float] | None = None,
               t: float | None = None) -> tuple[float, float, float, float]:
        """Predict + update con nuova misura `z=(x,y)`. Ritorna (x, y, σx, σy)."""
        if not self._initialized:
            self.init(z[0], z[1])
            return (z[0], z[1], self._std_min, self._std_min)

        # dt adattivo
        if t is not None and self._last_t is not None:
            dt = max(0.01, min(5.0, t - self._last_t))
            if abs(dt - self._dt) > 0.005:
                self._dt = dt
                self.F[0, 2] = dt
                self.F[1, 3] = dt
                self._rebuild_Q()
        if t is not None:
            self._last_t = t

        # PREDICT
        self.x = self.F @ self.x
        self.P = self.F @ self.P @ self.F.T + self.Q

        # UPDATE
        z_vec = _np.array([z[0], z[1]], dtype=_np.float64)
        if R is not None:
            R_mat = _np.diag([max(R[0], 1e-6), max(R[1], 1e-6)])
        else:
            R_mat = _np.eye(2) * self._q_pos * 100

        y_res = z_vec - self.H @ self.x
        S = self.H @ self.P @ self.H.T + R_mat
        K = self.P @ self.H.T @ _np.linalg.inv(S)
        self.x = self.x + K @ y_res
        self.P = self.P - K @ self.H @ self.P

        x_out = float(self.x[0])
        y_out = float(self.x[1])
        x_std = float(_np.sqrt(self.P[0, 0]))
        y_std = float(_np.sqrt(self.P[1, 1]))
        x_std = min(self._std_max, max(self._std_min, x_std))
        y_std = min(self._std_max, max(self._std_min, y_std))
        return (x_out, y_out, x_std, y_std)

    def reset(self) -> None:
        self.x = None
        self.P = None
        self._initialized = False
        self._last_t = None


# ============================================================
# Estimate dataclass
# ============================================================
@dataclass
class PositionEstimate:
    x: float                 # ∈ [0, 1]
    y: float                 # ∈ [0, 1]
    x_std: float
    y_std: float
    smoothed: bool
    confidence: float
    t: float


# ============================================================
# Position regressor
# ============================================================
class PositionRegressor:
    """RandomForestRegressor (x, y) + Kalman + LOO-cell validator.

    Workflow corretto:
        reg = PositionRegressor(window_frames=30)
        metrics = reg.train(labeled_frames)
        cv_report = reg.cross_validate_loo_cell(labeled_frames, max_mae=0.20)
        if cv_report["accepted"]:
            reg.save()
        # ALTRIMENTI: usa BlobEstimator (baseline no-ML).

    Workflow scorretto (causa di overfitting nel master):
        reg.train(labeled_frames); reg.save()   # niente CV spaziale = trappola
    """

    def __init__(self,
                 window_frames: int = 30,
                 smooth_q_pos: float = 1e-4,
                 smooth_q_vel: float = 1e-3,
                 smoothing: bool = True):
        self.window_frames = window_frames
        self.frame_hist: deque = deque(maxlen=window_frames)
        self._model = None
        self._trained = False

        self._class_labels: list[str] = []
        self._known_sources: list[str] = []
        self._source_key: str = "mac"
        self._custom_feature_names: list[str] = []
        self._xy_map: dict[str, tuple[float, float]] = {}
        self._rows = 1
        self._cols = 1
        self._train_metrics: dict[str, Any] = {}
        self._cv_report: dict[str, Any] = {}

        self._smoother = KalmanFilter2D(q_pos=smooth_q_pos, q_vel=smooth_q_vel) if smoothing else None
        self._last_t = 0.0

    # ----------- Status -----------
    @property
    def trained(self) -> bool:
        return self._trained

    @property
    def ready(self) -> bool:
        return self._trained and len(self.frame_hist) == self.window_frames

    @property
    def grid_dims(self) -> tuple[int, int]:
        return self._rows, self._cols

    @property
    def train_metrics(self) -> dict[str, Any]:
        return dict(self._train_metrics)

    @property
    def cv_report(self) -> dict[str, Any]:
        return dict(self._cv_report)

    # ----------- Training -----------
    def _check_deps(self) -> None:
        if not _SKLEARN_OK or _RF_REGRESSOR is None:
            raise RuntimeError("scikit-learn non installato")
        if not _NUMPY_OK:
            raise RuntimeError("numpy non installato")

    def _detect_sources(self, labeled_frames: dict[str, list]) -> tuple[str, list[str]]:
        """Stessa logica di csi_ml.PositionRegressor: source_id ha precedenza."""
        macs: set[str] = set()
        sids: set[str] = set()
        for frames in labeled_frames.values():
            for f in frames:
                sid = f.get("source_id")
                if sid is not None:
                    sids.add(str(sid))
                mac = f.get("mac")
                if mac and isinstance(mac, str):
                    macs.add(mac)
        if sids:
            return "source_id", sorted(sids)
        if macs:
            return "mac", sorted(macs)
        return "mac", []

    def _frames_to_X(self, frames: list, use_per_source: bool) -> list:
        """Sliding-window feature extraction su `frames`. Ritorna lista di vettori."""
        X: list = []
        win = self.window_frames
        for i in range(len(frames) - win + 1):
            window = frames[i:i + win]
            if use_per_source:
                vec = csi_window_to_vector_per_source(
                    window, self._known_sources,
                    self._custom_feature_names, self._source_key,
                )
            else:
                vec = csi_window_to_vector(window)
            if vec is not None:
                X.append(vec)
        return X

    def train(self, labeled_frames: dict[str, list]) -> dict[str, Any]:
        """Addestra il regressore con etichette griglia (rXcY)."""
        self._check_deps()
        if len(labeled_frames) < 2:
            raise ValueError("Servono almeno 2 classi")

        self._class_labels = list(labeled_frames.keys())
        self._xy_map = grid_labels_to_xy_map(self._class_labels)
        dims = parse_grid_labels(self._class_labels)
        if dims is None or not self._xy_map:
            raise ValueError("Nessuna label griglia (rXcY) trovata")
        self._rows, self._cols = dims

        self._source_key, self._known_sources = self._detect_sources(labeled_frames)
        use_per_source = len(self._known_sources) >= 2
        if use_per_source:
            self._custom_feature_names = _generate_source_feature_names(
                self._known_sources, self._source_key,
            )
        else:
            self._known_sources = []
            self._custom_feature_names = []

        X, y_x, y_y = [], [], []
        class_counts: dict[str, int] = {}
        for label, frames in labeled_frames.items():
            if not frames or label not in self._xy_map:
                continue
            tx, ty = self._xy_map[label]
            Xi = self._frames_to_X(frames, use_per_source)
            X.extend(Xi)
            y_x.extend([tx] * len(Xi))
            y_y.extend([ty] * len(Xi))
            class_counts[label] = len(Xi)

        if len(X) < 10:
            raise ValueError(f"Troppi pochi campioni: {len(X)} (servono >= 10)")

        # Class balance warning
        counts = list(class_counts.values())
        if counts and (max(counts) > min(counts) * 1.3):
            print(f"  [Regressor] WARN: classi sbilanciate "
                  f"(min={min(counts)}, max={max(counts)}). Considera undersampling.")

        y_multi = list(zip(y_x, y_y))
        self._model = _RF_REGRESSOR(
            n_estimators=100,
            max_depth=10,
            min_samples_leaf=3,
            random_state=42,
            n_jobs=1,
        )
        self._model.fit(X, y_multi)
        self._trained = True

        # In-sample MAE (puramente diagnostico, NON è validation)
        y_pred = self._model.predict(X)
        mae_x = float(mean(abs(y_pred[i][0] - y_x[i]) for i in range(len(X))))
        mae_y = float(mean(abs(y_pred[i][1] - y_y[i]) for i in range(len(X))))

        self._train_metrics = {
            "n_train": len(X),
            "n_features": len(X[0]),
            "n_classes": len(self._xy_map),
            "class_distribution": class_counts,
            "in_sample_mae_x": mae_x,
            "in_sample_mae_y": mae_y,
            "trained_at": time.time(),
        }
        return self._train_metrics

    # ----------- LOO-Cell Cross-Validation (the anti-overfitting gate) -----------
    def cross_validate_loo_cell(self, labeled_frames: dict[str, list],
                                max_mae_normalized: float = 0.20,
                                min_r2: float = 0.0) -> dict[str, Any]:
        """Leave-One-Cell-Out cross-validation: per ogni cella, training su
        tutte le altre + test sui frame della cella esclusa.

        Args:
            labeled_frames: stesso input di train()
            max_mae_normalized: MAE massimo accettabile in [0,1]² (default 0.20
                ≈ 1 metro in stanza 5 m). Sopra questa soglia il modello è
                rifiutato.
            min_r2: R² minimo accettabile (default 0.0 = "fa meglio di una
                predizione costante della media").

        Returns:
            dict con `accepted`, `mae`, `r2`, `per_cell_mae`, motivazione.
            Anche se rifiutato, il modello allenato resta in self._model
            (ma save() ne registrerà lo stato).
        """
        self._check_deps()
        cell_labels = [l for l in labeled_frames if _GRID_LABEL_RE.match(l)]
        if len(cell_labels) < 2:
            return {
                "accepted": False,
                "reason": "Servono >= 2 celle per LOO-cell",
                "mae": float("inf"),
                "r2": float("-inf"),
                "per_cell_mae": {},
            }

        per_cell_mae: dict[str, float] = {}
        all_errors: list[float] = []
        all_pred_x: list[float] = []
        all_true_x: list[float] = []
        all_pred_y: list[float] = []
        all_true_y: list[float] = []

        rows, cols = parse_grid_labels(cell_labels) or (1, 1)
        xy_map = grid_labels_to_xy_map(cell_labels)

        # Decide if per-source features
        source_key, known_sources = self._detect_sources(labeled_frames)
        use_per_source = len(known_sources) >= 2
        feature_names = []
        if use_per_source:
            feature_names = _generate_source_feature_names(known_sources, source_key)

        # Save current model state, restore at end
        saved_model = self._model
        saved_known = self._known_sources
        saved_key = self._source_key
        saved_names = self._custom_feature_names

        try:
            for hold_out in cell_labels:
                # Set internal state for feature extraction
                self._known_sources = known_sources if use_per_source else []
                self._source_key = source_key
                self._custom_feature_names = feature_names if use_per_source else []

                X_tr, yx_tr, yy_tr = [], [], []
                for label, frames in labeled_frames.items():
                    if label == hold_out or label not in xy_map:
                        continue
                    Xi = self._frames_to_X(frames, use_per_source)
                    if not Xi:
                        continue
                    tx, ty = xy_map[label]
                    X_tr.extend(Xi)
                    yx_tr.extend([tx] * len(Xi))
                    yy_tr.extend([ty] * len(Xi))

                X_te = self._frames_to_X(labeled_frames[hold_out], use_per_source)
                if not X_tr or not X_te:
                    per_cell_mae[hold_out] = float("nan")
                    continue
                tx, ty = xy_map[hold_out]

                fold_model = _RF_REGRESSOR(
                    n_estimators=60, max_depth=10, min_samples_leaf=3,
                    random_state=42, n_jobs=1,
                )
                fold_model.fit(X_tr, list(zip(yx_tr, yy_tr)))
                preds = fold_model.predict(X_te)

                errs = []
                for p in preds:
                    e = math.hypot(p[0] - tx, p[1] - ty)
                    errs.append(e)
                    all_errors.append(e)
                    all_pred_x.append(p[0]); all_true_x.append(tx)
                    all_pred_y.append(p[1]); all_true_y.append(ty)
                per_cell_mae[hold_out] = float(mean(errs))
        finally:
            self._model = saved_model
            self._known_sources = saved_known
            self._source_key = saved_key
            self._custom_feature_names = saved_names

        if not all_errors:
            return {
                "accepted": False,
                "reason": "Nessun fold valido (probabilmente dati troppo scarsi)",
                "mae": float("inf"),
                "r2": float("-inf"),
                "per_cell_mae": per_cell_mae,
            }

        mae_overall = float(mean(all_errors))
        # R² combinato su x e y (su tutti i fold)
        r2 = _r2_2d(all_true_x, all_pred_x, all_true_y, all_pred_y)

        accepted = mae_overall <= max_mae_normalized and r2 >= min_r2
        reason_parts = []
        if mae_overall > max_mae_normalized:
            reason_parts.append(f"MAE LOO {mae_overall:.3f} > soglia {max_mae_normalized}")
        if r2 < min_r2:
            reason_parts.append(f"R² {r2:.3f} < soglia {min_r2}")
        reason = "OK" if accepted else "; ".join(reason_parts)

        report = {
            "accepted": accepted,
            "reason": reason,
            "mae": mae_overall,
            "r2": r2,
            "per_cell_mae": per_cell_mae,
            "max_mae_normalized": max_mae_normalized,
            "min_r2": min_r2,
            "n_folds": len(per_cell_mae),
        }
        self._cv_report = report
        return report

    # ----------- Inference -----------
    def add_frame(self, frame: dict) -> None:
        self.frame_hist.append(frame)
        self._last_t = time.time()

    def predict(self) -> PositionEstimate | None:
        if not self.ready or self._model is None:
            return None
        frames = list(self.frame_hist)
        use_per_source = len(self._known_sources) >= 2
        if use_per_source:
            vec = csi_window_to_vector_per_source(
                frames, self._known_sources,
                self._custom_feature_names, self._source_key,
            )
        else:
            vec = csi_window_to_vector(frames)
        if vec is None:
            return None

        # Predizione + incertezza dalla varianza tra gli alberi
        xy = self._model.predict([vec])[0]
        tree_preds = [tree.predict([vec])[0] for tree in self._model.estimators_]
        if len(tree_preds) > 1 and _NUMPY_OK:
            x_std = float(_np.std([p[0] for p in tree_preds]))
            y_std = float(_np.std([p[1] for p in tree_preds]))
        else:
            x_std, y_std = 0.05, 0.05
        x_std = max(0.02, min(0.5, x_std))
        y_std = max(0.02, min(0.5, y_std))

        x_raw, y_raw = float(xy[0]), float(xy[1])
        if self._smoother is not None:
            x_out, y_out, x_std_o, y_std_o = self._smoother.update(
                z=(x_raw, y_raw), R=(x_std ** 2, y_std ** 2), t=self._last_t,
            )
            smoothed = True
        else:
            x_out, y_out, x_std_o, y_std_o = x_raw, y_raw, x_std, y_std
            smoothed = False

        # Confidence: inversa di un'incertezza normalizzata
        unc = (x_std_o + y_std_o) / 2.0  # in [0, 0.5]
        confidence = max(0.0, 1.0 - unc * 2.0)

        return PositionEstimate(
            x=x_out, y=y_out,
            x_std=x_std_o, y_std=y_std_o,
            smoothed=smoothed,
            confidence=confidence,
            t=self._last_t,
        )

    def reset_smoother(self) -> None:
        if self._smoother is not None:
            self._smoother.reset()

    # ----------- Persistence -----------
    def save(self, path: str = DEFAULT_MODEL_PATH,
             config_path: str = DEFAULT_CONFIG_PATH) -> str:
        """Salva modello + config. Salva ANCHE il cv_report nel JSON, così
        il loader sa che il modello è stato validato (o no).

        Loader rifiuta modelli con cv_report.accepted=False UNLESS l'utente
        passa allow_unvalidated=True esplicitamente.
        """
        if not self._trained or self._model is None:
            raise RuntimeError("Modello non addestrato")
        if not _JOBLIB_OK:
            raise RuntimeError("joblib non installato")

        _joblib.dump(self._model, path)
        config = {
            "version": 1,
            "class_labels": self._class_labels,
            "known_sources": self._known_sources,
            "source_key": self._source_key,
            "xy_map": {k: list(v) for k, v in self._xy_map.items()},
            "rows": self._rows,
            "cols": self._cols,
            "window_frames": self.window_frames,
            "n_features": getattr(self._model, "n_features_in_", None),
            "train_metrics": self._train_metrics,
            "cv_report": self._cv_report,  # ← chiave: persistiamo la validazione
        }
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        return path

    def load(self, path: str = DEFAULT_MODEL_PATH,
             config_path: str = DEFAULT_CONFIG_PATH,
             allow_unvalidated: bool = False) -> bool:
        """Carica modello. Rifiuta se cv_report.accepted=False salvo override."""
        if not _JOBLIB_OK:
            raise RuntimeError("joblib non installato")
        if not os.path.exists(path) or not os.path.exists(config_path):
            return False

        with open(config_path) as f:
            config = json.load(f)

        cv = config.get("cv_report") or {}
        if cv and not cv.get("accepted", False) and not allow_unvalidated:
            print(f"  [Regressor] RIFIUTATO modello non validato: {cv.get('reason')}")
            print(f"  [Regressor] Usa BlobEstimator (no-ML) o riallena.")
            return False

        self._model = _joblib.load(path)
        self._class_labels = list(config.get("class_labels", []))
        self._known_sources = list(config.get("known_sources", []))
        self._source_key = config.get("source_key", "mac")
        self._custom_feature_names = []
        if self._known_sources:
            self._custom_feature_names = _generate_source_feature_names(
                self._known_sources, self._source_key,
            )
        self._xy_map = {k: tuple(v) for k, v in config.get("xy_map", {}).items()}
        self._rows = int(config.get("rows", 1))
        self._cols = int(config.get("cols", 1))
        self.window_frames = int(config.get("window_frames", self.window_frames))
        self.frame_hist = deque(maxlen=self.window_frames)
        self._train_metrics = dict(config.get("train_metrics", {}))
        self._cv_report = dict(config.get("cv_report", {}))
        self._trained = True
        return True


# ============================================================
# R² helper (no scipy)
# ============================================================
def _r2_2d(t_x: list[float], p_x: list[float],
           t_y: list[float], p_y: list[float]) -> float:
    """R² combinato su x e y (concatenati). 1.0 = perfetto, 0.0 = come predire
    la media, < 0 = peggio della media."""
    if not t_x:
        return float("-inf")
    all_true = t_x + t_y
    all_pred = p_x + p_y
    m = sum(all_true) / len(all_true)
    ss_tot = sum((v - m) ** 2 for v in all_true)
    if ss_tot < 1e-12:
        return 0.0  # degenere: tutti uguali
    ss_res = sum((all_true[i] - all_pred[i]) ** 2 for i in range(len(all_true)))
    return 1.0 - ss_res / ss_tot
