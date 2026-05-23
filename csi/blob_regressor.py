#!/usr/bin/env python3
"""
blob_regressor.py — Continuous (x, y) localization from CSI via Random Forest.

Sostituisce la classificazione grid-based (r0c0, r0c1, ...) con un regressore
2D che predice coordinate continue. Aggiunge un Kalman filter 2D a velocita'
costante per smoothing temporale, e un classificatore semplice fermo/movimento
basato sulla velocita' stimata.

Architettura:
    Frame CSI -> CSIClassifier feature extraction -> RandomForestRegressor
                                                  -> (x, y)
                                                  -> Kalman 2D
                                                  -> (x_smooth, y_smooth, vx, vy)
                                                  -> motion classifier

Uso:
    from csi.blob_regressor import BlobRegressor

    blob = BlobRegressor(window_frames=30)
    # Training: dict {(x,y) tuple: [frame_dict, ...]}
    blob.train(labeled_samples)
    blob.save()

    # Inference frame-by-frame
    blob.load()
    for frame in stream:
        blob.add_frame(frame)
        if blob.ready:
            state = blob.predict_smoothed()
            # state = {x, y, vx, vy, speed, confidence, motion}

Dipendenze: scikit-learn, joblib, numpy (tutte gia' nel progetto)
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import deque
from typing import Optional

# sklearn lazy import (coerente col resto del progetto)
_SKLEARN_AVAILABLE = False
_RFR_CLASS = None
try:
    from sklearn.ensemble import RandomForestRegressor as _RFR
    _RFR_CLASS = _RFR
    _SKLEARN_AVAILABLE = True
except ImportError:
    pass

try:
    import joblib as _joblib
except ImportError:
    _joblib = None

# Riusa feature extraction dal modulo classifier
from .csi_ml import (
    csi_window_to_vector_per_source,
    _generate_source_feature_names,
)

# ============================================================
# Paths
# ============================================================
MODEL_DIR = os.path.dirname(os.path.abspath(__file__))
BLOB_MODEL_PATH = os.path.join(MODEL_DIR, "csi_blob_model.joblib")
BLOB_META_PATH = os.path.join(MODEL_DIR, "csi_blob_meta.json")


# ============================================================
# Kalman filter 2D a velocita' costante
# ============================================================
class Kalman2D:
    """Constant-velocity 2D Kalman filter.

    Stato: [x, y, vx, vy]^T (4D).
    Misura: [x, y]^T (2D).

    Parametri tarabili:
        q_pos:   noise di processo per posizione (m^2 / s^2 ~ jerk^2 dt^3)
        q_vel:   noise di processo per velocita' (m^2 / s^4)
        r_meas:  noise di misura (m^2), tipicamente ~ accuracy^2 del regressore
        dt:      intervallo di update in secondi (auto se passi None)
    """

    def __init__(
        self,
        q_pos: float = 0.05,
        q_vel: float = 0.2,
        r_meas: float = 0.25,
        dt: float | None = None,
    ):
        # Stato
        self.x = 0.0
        self.y = 0.0
        self.vx = 0.0
        self.vy = 0.0
        # Covarianza 4x4 diagonale iniziale "molto incerta"
        self.P = [
            [10.0, 0.0, 0.0, 0.0],
            [0.0, 10.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
        self.q_pos = q_pos
        self.q_vel = q_vel
        self.r_meas = r_meas
        self.dt_fixed = dt
        self._last_t: float | None = None
        self.initialized = False

    def update(self, x_meas: float, y_meas: float, t_now: float | None = None):
        """Step di predict + update con una nuova misura (x, y)."""
        if t_now is None:
            t_now = time.time()

        # Calcolo dt
        if self.dt_fixed is not None:
            dt = self.dt_fixed
        elif self._last_t is None:
            dt = 0.1
        else:
            dt = max(0.001, t_now - self._last_t)
        self._last_t = t_now

        if not self.initialized:
            self.x = x_meas
            self.y = y_meas
            self.initialized = True
            return

        # ---- Predict ----
        # x_k = x_{k-1} + vx * dt
        self.x = self.x + self.vx * dt
        self.y = self.y + self.vy * dt
        # F = [[1,0,dt,0],[0,1,0,dt],[0,0,1,0],[0,0,0,1]]
        # P = F P F^T + Q
        # Aggiorno P (semplificato per F sparso)
        P = self.P
        new_P = [[0.0] * 4 for _ in range(4)]
        # new_P[0][0] = P[0][0] + 2*dt*P[0][2] + dt^2*P[2][2] + q_pos
        new_P[0][0] = P[0][0] + 2 * dt * P[0][2] + dt * dt * P[2][2] + self.q_pos
        new_P[1][1] = P[1][1] + 2 * dt * P[1][3] + dt * dt * P[3][3] + self.q_pos
        new_P[2][2] = P[2][2] + self.q_vel
        new_P[3][3] = P[3][3] + self.q_vel
        new_P[0][2] = P[0][2] + dt * P[2][2]
        new_P[2][0] = new_P[0][2]
        new_P[1][3] = P[1][3] + dt * P[3][3]
        new_P[3][1] = new_P[1][3]
        self.P = new_P

        # ---- Update ----
        # H = [[1,0,0,0],[0,1,0,0]]
        # y_k = z - Hx (innovation)
        ix = x_meas - self.x
        iy = y_meas - self.y
        # S = H P H^T + R = [[P[0][0]+R, P[0][1]], [P[1][0], P[1][1]+R]]
        s00 = self.P[0][0] + self.r_meas
        s11 = self.P[1][1] + self.r_meas
        s01 = self.P[0][1]
        # det(S)
        det_s = s00 * s11 - s01 * s01
        if abs(det_s) < 1e-9:
            return  # singolare, salta update
        # S^-1
        sinv00 = s11 / det_s
        sinv11 = s00 / det_s
        sinv01 = -s01 / det_s
        # K = P H^T S^-1   (K e' 4x2)
        # K[i][j] = P[i][0]*sinv0j + P[i][1]*sinv1j
        K = [[0.0, 0.0] for _ in range(4)]
        for i in range(4):
            K[i][0] = self.P[i][0] * sinv00 + self.P[i][1] * sinv01
            K[i][1] = self.P[i][0] * sinv01 + self.P[i][1] * sinv11
        # Applica correzione: stato += K * innovation
        self.x += K[0][0] * ix + K[0][1] * iy
        self.y += K[1][0] * ix + K[1][1] * iy
        self.vx += K[2][0] * ix + K[2][1] * iy
        self.vy += K[3][0] * ix + K[3][1] * iy
        # P = (I - K H) P
        # Solo gli elementi necessari
        new_P2 = [[0.0] * 4 for _ in range(4)]
        for i in range(4):
            for j in range(4):
                # row of (I - KH): KH[i][j] = K[i][0] if j==0 else (K[i][1] if j==1 else 0)
                kh_ij = K[i][0] if j == 0 else (K[i][1] if j == 1 else 0.0)
                ikh_ij = (1.0 if i == j else 0.0) - kh_ij
                # sum over k of ikh_ik * P[k][j]
                val = 0.0
                for k in range(4):
                    kh_ik = K[i][0] if k == 0 else (K[i][1] if k == 1 else 0.0)
                    ikh_ik = (1.0 if i == k else 0.0) - kh_ik
                    val += ikh_ik * self.P[k][j]
                new_P2[i][j] = val
        self.P = new_P2

    def state(self) -> dict:
        speed = math.sqrt(self.vx * self.vx + self.vy * self.vy)
        return {
            "x": self.x, "y": self.y,
            "vx": self.vx, "vy": self.vy,
            "speed": speed,
            "var_x": self.P[0][0], "var_y": self.P[1][1],
            "initialized": self.initialized,
        }


# ============================================================
# BlobRegressor — wrapper completo
# ============================================================
class BlobRegressor:
    """RandomForestRegressor sul vettore feature CSI -> (x, y) continui.

    Internamente usa la stessa feature extraction del CSIClassifier custom
    (per-source feature concatenation). L'output e' (x, y) in metri.
    """

    def __init__(
        self,
        window_frames: int = 30,
        kalman_q_pos: float = 0.05,
        kalman_q_vel: float = 0.2,
        kalman_r_meas: float = 0.25,
        motion_threshold_mps: float = 0.15,
        motion_sustain_n: int = 3,
    ):
        self.window_frames = window_frames
        self._model_x = None   # regressore per x
        self._model_y = None   # regressore per y
        self._known_sources: list[str] = []
        self._source_key: str = "mac"
        self._feature_names: list[str] = []
        self._trained = False

        # Buffer di frame per inferenza streaming
        self._buffer: deque = deque(maxlen=window_frames)

        # Kalman + motion classifier
        self.kf = Kalman2D(
            q_pos=kalman_q_pos, q_vel=kalman_q_vel, r_meas=kalman_r_meas)
        self._motion_thresh = motion_threshold_mps
        self._motion_sustain = motion_sustain_n
        self._above_n = 0
        self._below_n = 0
        self._motion_state = False

    # ---- Property ----
    @property
    def ready(self) -> bool:
        return self._trained and len(self._buffer) >= self.window_frames

    # ---- Training ----
    def train(self, samples: dict[tuple[float, float], list]) -> dict:
        """Addestra il regressore con campioni etichettati per posizione (x, y).

        Args:
            samples: dict { (x_m, y_m): [frame_dict, ...] } con coordinate
                     continue in metri. Servono almeno 5-10 punti di
                     calibrazione coprenti l'area, con >= 100 frame ognuno.

        Returns:
            dict con metriche di training: n_train, n_features, mae_x, mae_y, ...
        """
        if not _SKLEARN_AVAILABLE or _RFR_CLASS is None:
            raise RuntimeError("scikit-learn non installato")
        if len(samples) < 3:
            raise ValueError("Servono almeno 3 punti di calibrazione")

        # 1. Scansiona sorgenti uniche (stessa logica di CSIClassifier.train_custom)
        all_sources: set = set()
        has_source_id = False
        for frames in samples.values():
            for f in frames:
                sid = f.get("source_id")
                if sid is not None:
                    has_source_id = True
                    all_sources.add(str(sid))
        if has_source_id:
            self._source_key = "source_id"
        else:
            macs: set = set()
            for frames in samples.values():
                for f in frames:
                    m = f.get("mac")
                    if m and isinstance(m, str):
                        macs.add(m)
            if macs:
                self._source_key = "mac"
                all_sources = macs

        self._known_sources = sorted(all_sources)
        use_per_source = len(self._known_sources) >= 2
        if use_per_source:
            self._feature_names = _generate_source_feature_names(
                self._known_sources, self._source_key)
            print(f"  [BlobRegressor] Sorgenti ({self._source_key}): "
                  f"{len(self._known_sources)}")
        else:
            self._feature_names = []
            print(f"  [BlobRegressor] Sorgente singola, feature standard")

        # 2. Costruisci X (feature) e Y (coordinate target)
        X: list = []
        Y_x: list = []
        Y_y: list = []
        per_point_counts = {}

        for (x_pos, y_pos), frames in samples.items():
            n_frames = len(frames)
            if n_frames < self.window_frames:
                print(f"    skip ({x_pos:.2f},{y_pos:.2f}): solo {n_frames} frame")
                continue

            # Slide windows con stride per generare piu' campioni
            stride = max(1, self.window_frames // 3)
            n_windows = 0
            for start in range(0, n_frames - self.window_frames + 1, stride):
                window = frames[start:start + self.window_frames]
                if use_per_source:
                    vec = csi_window_to_vector_per_source(
                        window, self._known_sources, self._feature_names,
                        self._source_key)
                    if vec is None:
                        continue
                else:
                    vec = self._profile_to_vector(window)
                X.append(vec)
                Y_x.append(x_pos)
                Y_y.append(y_pos)
                n_windows += 1
            per_point_counts[(x_pos, y_pos)] = n_windows

        if len(X) < 20:
            raise ValueError(f"Troppi pochi training samples: {len(X)}")

        # 3. Train due regressori (uno per x, uno per y)
        print(f"  [BlobRegressor] Training {len(X)} campioni x "
              f"{len(X[0])} feature")
        for pt, cnt in per_point_counts.items():
            print(f"    ({pt[0]:.2f}, {pt[1]:.2f}): {cnt} windows")

        self._model_x = _RFR_CLASS(
            n_estimators=80, max_depth=12, random_state=42, n_jobs=1)
        self._model_y = _RFR_CLASS(
            n_estimators=80, max_depth=12, random_state=43, n_jobs=1)
        self._model_x.fit(X, Y_x)
        self._model_y.fit(X, Y_y)
        self._trained = True

        # 4. MAE sul training set (overfit ma indicativo)
        Y_x_pred = self._model_x.predict(X)
        Y_y_pred = self._model_y.predict(X)
        mae_x = sum(abs(a - b) for a, b in zip(Y_x_pred, Y_x)) / len(Y_x)
        mae_y = sum(abs(a - b) for a, b in zip(Y_y_pred, Y_y)) / len(Y_y)
        med_err = sum(
            math.sqrt((a - c) ** 2 + (b - d) ** 2)
            for a, b, c, d in zip(Y_x_pred, Y_y_pred, Y_x, Y_y)
        ) / len(Y_x)

        metrics = {
            "n_train": len(X),
            "n_features": len(X[0]),
            "n_points": len(per_point_counts),
            "mae_x_m": round(mae_x, 3),
            "mae_y_m": round(mae_y, 3),
            "mean_euclid_err_m_train": round(med_err, 3),
            "known_sources": self._known_sources,
            "source_key": self._source_key,
        }
        print(f"\n  Train MAE: x={mae_x:.3f}m  y={mae_y:.3f}m  "
              f"|err|={med_err:.3f}m (overfit-biased)")
        return metrics

    @staticmethod
    def _profile_to_vector(window: list) -> list[float]:
        """Single-source: estrai un vettore da una finestra di frame.

        csi_window_to_vector vuole una LISTA di frame, non un dict profilo.
        Lo lasciamo come fallback per setup mono-trasmettitore.
        """
        from .csi_ml import csi_window_to_vector
        vec = csi_window_to_vector(window)
        return vec if vec is not None else []

    # ---- Persistence ----
    def save(self, model_path: str = BLOB_MODEL_PATH,
             meta_path: str = BLOB_META_PATH) -> str:
        if not self._trained:
            raise RuntimeError("Modello non addestrato")
        if _joblib is None:
            raise RuntimeError("joblib non installato")
        _joblib.dump({"x": self._model_x, "y": self._model_y}, model_path)
        meta = {
            "window_frames": self.window_frames,
            "known_sources": self._known_sources,
            "source_key": self._source_key,
            "feature_names": self._feature_names,
        }
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)
        print(f"  [BlobRegressor] Modello salvato: {model_path}")
        return model_path

    def load(self, model_path: str = BLOB_MODEL_PATH,
             meta_path: str = BLOB_META_PATH) -> bool:
        if _joblib is None:
            raise RuntimeError("joblib non installato")
        if not os.path.exists(model_path) or not os.path.exists(meta_path):
            return False
        d = _joblib.load(model_path)
        self._model_x = d["x"]
        self._model_y = d["y"]
        with open(meta_path) as f:
            meta = json.load(f)
        self.window_frames = meta.get("window_frames", 30)
        self._known_sources = meta.get("known_sources", [])
        self._source_key = meta.get("source_key", "mac")
        self._feature_names = meta.get("feature_names", [])
        self._trained = True
        self._buffer = deque(maxlen=self.window_frames)
        print(f"  [BlobRegressor] Modello caricato. Sorgenti: "
              f"{len(self._known_sources)}, window={self.window_frames}")
        return True

    # ---- Inference ----
    def add_frame(self, frame: dict) -> None:
        self._buffer.append(frame)

    def predict_raw(self) -> Optional[dict]:
        """Predice (x, y) raw dal buffer corrente (senza Kalman)."""
        if not self.ready or self._model_x is None or self._model_y is None:
            return None
        window = list(self._buffer)
        if self._known_sources:
            vec = csi_window_to_vector_per_source(
                window, self._known_sources, self._feature_names,
                self._source_key)
            if vec is None:
                return None
        else:
            vec = self._profile_to_vector(window)
        x = float(self._model_x.predict([vec])[0])
        y = float(self._model_y.predict([vec])[0])
        return {"x": x, "y": y}

    def predict_smoothed(self) -> Optional[dict]:
        """Predice (x, y) e applica Kalman. Classifica fermo/movimento."""
        raw = self.predict_raw()
        if raw is None:
            return None
        self.kf.update(raw["x"], raw["y"])
        st = self.kf.state()
        speed = st["speed"]

        # Hysteresis su motion: serve N campioni sopra/sotto soglia per cambiare
        if speed > self._motion_thresh:
            self._above_n += 1
            self._below_n = 0
            if self._above_n >= self._motion_sustain:
                self._motion_state = True
        else:
            self._below_n += 1
            self._above_n = 0
            if self._below_n >= self._motion_sustain:
                self._motion_state = False

        confidence = 1.0 / (1.0 + math.sqrt(st["var_x"] + st["var_y"]))

        return {
            "x_raw": round(raw["x"], 3),
            "y_raw": round(raw["y"], 3),
            "x": round(st["x"], 3),
            "y": round(st["y"], 3),
            "vx": round(st["vx"], 3),
            "vy": round(st["vy"], 3),
            "speed": round(speed, 3),
            "motion": self._motion_state,
            "confidence": round(confidence, 3),
        }
