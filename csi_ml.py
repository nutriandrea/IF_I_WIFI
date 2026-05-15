#!/usr/bin/env python3
"""
CSI ML Classifier — Random Forest per classificazione attività da CSI.

Sostituisce CSIDetector con un classificatore Random Forest multi-classe che:
  - Usa il profilo di ampiezza per-subcarrier come firma spettrale
  - Classifica: EMPTY (vuoto), STATIONARY (fermo/respiro), MOVEMENT (movimento)
  - Produce probabilità per ogni classe

Feature da finestra di frame CSI:
  - Per-subcarrier: ampl_mean e ampl_std (prime 32 subcarrier)
  - Globali: variance across subcarriers, max_var_subcarrier, temporal_variance
  - RSSI mean/std, noise_floor mean

Uso:
  from csi_ml import CSIClassifier, CSI_CLASSES, CSI_LABELS

  clf = CSIClassifier(window_frames=30)
  clf.train(empty_frames, stationary_frames, movement_frames)
  clf.save()

  # Inference
  clf.add_frame(frame)
  if clf.ready:
      probs = clf.predict_proba()  # → {EMPTY: 0.1, STATIONARY: 0.7, MOVEMENT: 0.2}

Dipendenze opzionali:
  scikit-learn  (apt: python3-sklearn, pip: scikit-learn)
  joblib        (per salvare/caricare modello)
"""

import json
import os
import sys
import time
from collections import deque
from statistics import mean, stdev

# sklearn: import lazy
_SKLEARN_AVAILABLE = False
_RF_CLASS = None
try:
    from sklearn.ensemble import RandomForestClassifier as _RFC
    _RF_CLASS = _RFC
    _SKLEARN_AVAILABLE = True
except ImportError:
    pass

try:
    import joblib as _joblib
except ImportError:
    _joblib = None

# ============================================================
# Config
# ============================================================
MODEL_DIR = os.path.dirname(os.path.abspath(__file__))
CSI_MODEL_PATH = os.path.join(MODEL_DIR, "csi_model.joblib")

# Classi
EMPTY = 0
STATIONARY = 1
MOVEMENT = 2
CSI_CLASSES = {EMPTY: "EMPTY", STATIONARY: "STATIONARY", MOVEMENT: "MOVEMENT"}
CSI_LABELS = ["EMPTY", "STATIONARY", "MOVEMENT"]

# Feature names globali (24) + per-subcarrier (32 mean + 32 std = 64) = 88 totali
GLOBAL_FEATURE_NAMES = [
    "variance_across_subcarriers",     # varianza del profilo medio tra subcarrier
    "max_var_subcarrier_index",         # indice subcarrier con + varianza temporale
    "max_var_subcarrier_value",         # valore di quella varianza
    "temporal_variance",               # varianza di ampl_mean nel tempo
    "temporal_std_variance",           # varianza di ampl_std nel tempo
    "sub_peak_mean",                   # media delle 3 subcarrier con ampl maggiore
    "sub_peak_std",                    # std delle 3 subcarrier con ampl maggiore
    "ampl_mean_min", "ampl_mean_max", "ampl_mean_range",
    "ampl_std_min", "ampl_std_max", "ampl_std_range",
    "rssi_mean", "rssi_std",
    "noise_floor_mean",
    "window_frames",
]

SUB_MEAN_PREFIX = "sub_mean_"
SUB_STD_PREFIX = "sub_std_"
NUM_CSI_SUBCARRIERS = 32  # prime 32 subcarrier usate come feature

# Numero totale feature = globali + 2 * NUM_CSI_SUBCARRIERS (mean + std per sub)
CSI_FEATURE_SIZE = len(GLOBAL_FEATURE_NAMES) + 2 * NUM_CSI_SUBCARRIERS

# Nomi completi delle feature (per export)
CSI_FEATURE_NAMES = (
    GLOBAL_FEATURE_NAMES
    + [f"{SUB_MEAN_PREFIX}{i}" for i in range(NUM_CSI_SUBCARRIERS)]
    + [f"{SUB_STD_PREFIX}{i}" for i in range(NUM_CSI_SUBCARRIERS)]
)


# ============================================================
# Feature extraction
# ============================================================

def extract_csi_profile(frames_window: list) -> dict:
    """Estrae feature da una finestra di frame CSI.

    Args:
        frames_window: Lista di dict CSI (da parse_csi_line) con chiave 'csi'.

    Returns:
        dict con feature, o {"_empty": True} se dati insufficienti.
    """
    n = len(frames_window)
    if n < 2:
        return {"_empty": True}

    # Colleziona ampiezze per-subcarrier e metriche globali per frame
    ampl_vectors = []
    ampl_means_per_frame = []
    ampl_stds_per_frame = []
    rssi_vals = []
    noise_vals = []

    for f in frames_window:
        csi = f.get("csi")
        if not csi or not isinstance(csi, list):
            continue

        amps = [c.get("ampl", 0) for c in csi if isinstance(c, dict)]
        if len(amps) < 2:
            continue

        ampl_vectors.append(amps)
        ampl_means_per_frame.append(mean(amps))
        ampl_stds_per_frame.append(stdev(amps))

        rssi = f.get("rssi")
        if rssi is not None:
            rssi_vals.append(float(rssi))

        noise = f.get("noise_floor")
        if noise is not None:
            noise_vals.append(float(noise))

    if not ampl_vectors:
        return {"_empty": True}

    # Allinea tutte le subcarrier alla stessa lunghezza (padding/trunc a 64)
    MAX_SUB = 64
    aligned = []
    for v in ampl_vectors:
        if len(v) < MAX_SUB:
            v = list(v) + [0.0] * (MAX_SUB - len(v))
        aligned.append(v[:MAX_SUB])

    num_frames = len(aligned)
    num_sub = MAX_SUB

    # Profilo medio e std per subcarrier attraverso la finestra
    per_sub_mean = [
        mean(aligned[j][i] for j in range(num_frames)) for i in range(num_sub)
    ]
    per_sub_std = [
        stdev([aligned[j][i] for j in range(num_frames)]) if num_frames >= 2 else 0.0
        for i in range(num_sub)
    ]

    feats = {}
    feats["window_frames"] = num_frames

    # Feature globali
    feats["variance_across_subcarriers"] = round(
        stdev(per_sub_mean) if len(per_sub_mean) >= 2 else 0.0, 4
    )

    # Subcarrier con massima varianza temporale (è dove il multipath cambia di +)
    max_var_sub_idx = max(range(num_sub), key=lambda i: per_sub_std[i])
    feats["max_var_subcarrier_index"] = max_var_sub_idx
    feats["max_var_subcarrier_value"] = round(per_sub_std[max_var_sub_idx], 4)

    # Varianza temporale
    feats["temporal_variance"] = round(
        stdev(ampl_means_per_frame) if len(ampl_means_per_frame) >= 2 else 0.0, 4
    )
    feats["temporal_std_variance"] = round(
        stdev(ampl_stds_per_frame) if len(ampl_stds_per_frame) >= 2 else 0.0, 4
    )

    # Subcarrier con ampiezza maggiore (picchi spettrali)
    sorted_means = sorted(per_sub_mean, reverse=True)
    feats["sub_peak_mean"] = round(mean(sorted_means[:3]), 4) if len(sorted_means) >= 3 else round(mean(sorted_means), 4)
    feats["sub_peak_std"] = round(stdev(sorted_means[:3]), 4) if len(sorted_means) >= 3 else 0.0

    feats["ampl_mean_min"] = round(min(ampl_means_per_frame), 4)
    feats["ampl_mean_max"] = round(max(ampl_means_per_frame), 4)
    feats["ampl_mean_range"] = round(feats["ampl_mean_max"] - feats["ampl_mean_min"], 4)
    feats["ampl_std_min"] = round(min(ampl_stds_per_frame), 4)
    feats["ampl_std_max"] = round(max(ampl_stds_per_frame), 4)
    feats["ampl_std_range"] = round(feats["ampl_std_max"] - feats["ampl_std_min"], 4)

    feats["rssi_mean"] = round(mean(rssi_vals), 2) if rssi_vals else 0.0
    feats["rssi_std"] = round(stdev(rssi_vals), 2) if len(rssi_vals) >= 2 else 0.0
    feats["noise_floor_mean"] = round(mean(noise_vals), 2) if noise_vals else 0.0

    # Per-subcarrier feature (prime 32)
    for i in range(NUM_CSI_SUBCARRIERS):
        feats[f"{SUB_MEAN_PREFIX}{i}"] = round(per_sub_mean[i], 4)
        feats[f"{SUB_STD_PREFIX}{i}"] = round(per_sub_std[i], 4)

    return feats


def csi_window_to_vector(frames_window: list) -> list | None:
    """Converte finestra frame CSI in vettore feature flat per sklearn."""
    f = extract_csi_profile(frames_window)
    if f.get("_empty"):
        return None
    return [f[n] for n in CSI_FEATURE_NAMES]


# ============================================================
# CSIClassifier
# ============================================================

class CSIClassifier:
    """
    Random Forest multi-classe per classificazione attività da CSI.

    Usage:
        clf = CSIClassifier(window_frames=30)

        # Training
        clf.train(empty_frames, stationary_frames, movement_frames)
        clf.save()

        # Real-time inference
        clf.add_frame(csi_frame_dict)
        if clf.ready:
            probs = clf.predict_proba()
            # → {EMPTY: 0.1, STATIONARY: 0.7, MOVEMENT: 0.2}
    """

    def __init__(self, window_frames: int = 30):
        self.window_frames = window_frames
        self.frame_hist: deque = deque(maxlen=window_frames)
        self._model = None
        self._trained = False
        self._t0 = 0.0
        self._last_features: dict = {}
        self._last_probas: dict = {lbl: 0.0 for lbl in CSI_LABELS}
        self._feature_importance: dict = {}

    # ---- Properties ----

    @property
    def ready(self) -> bool:
        """Pronto per inferenza: finestra piena e modello addestrato."""
        return self._trained and len(self.frame_hist) == self.window_frames

    @property
    def trained(self) -> bool:
        return self._trained

    # ---- Training ----

    @staticmethod
    def _check_sklearn():
        if not _SKLEARN_AVAILABLE:
            raise RuntimeError(
                "scikit-learn non installato.\n"
                "  UNO Q: sudo apt install python3-sklearn python3-joblib\n"
                "  Mac:   pip install scikit-learn joblib"
            )

    def _frames_to_xy(self, frames: list, label: int) -> tuple:
        """Converte lista frame in feature matrix X, label vector y."""
        X, y = [], []
        window = deque(maxlen=self.window_frames)

        for f in frames:
            window.append(f)
            if len(window) == self.window_frames:
                vec = csi_window_to_vector(list(window))
                if vec is not None:
                    X.append(vec)
                    y.append(label)

        return X, y

    def train(
        self,
        empty_frames: list,
        stationary_frames: list | None = None,
        movement_frames: list | None = None,
    ) -> dict:
        """Addestra il Random Forest multi-classe.

        Args:
            empty_frames: frame CSI stanza vuota (classe EMPTY)
            stationary_frames: frame CSI persona ferma (classe STATIONARY, opzionale)
            movement_frames: frame CSI con movimento (classe MOVEMENT)

        Returns:
            dict con metriche di training.
        """
        self._check_sklearn()
        assert _RF_CLASS is not None  # type narrowing

        if not empty_frames or not movement_frames:
            raise ValueError("Servono almeno frame EMPTY e MOVEMENT")

        X0, y0 = self._frames_to_xy(empty_frames, EMPTY)
        X1, y1 = ([], [])
        if stationary_frames:
            X1, y1 = self._frames_to_xy(stationary_frames, STATIONARY)
        X2, y2 = self._frames_to_xy(movement_frames, MOVEMENT)

        if len(X0) < 3:
            raise ValueError(f"Pochi feature vector da EMPTY: {len(X0)}")
        if len(X2) < 3:
            raise ValueError(f"Pochi feature vector da MOVEMENT: {len(X2)}")

        X = X0 + X1 + X2
        y = y0 + y1 + y2

        n_classes = len(set(y))
        print(f"  [CSIClassifier] Training: {len(X)} campioni x {len(X[0])} feature")
        print(f"  Classi: EMPTY={y0.count(EMPTY)}, "
              f"STATIONARY={y1.count(STATIONARY)}, "
              f"MOVEMENT={y2.count(MOVEMENT)}")

        self._model = _RF_CLASS(
            n_estimators=50,
            max_depth=8,
            class_weight="balanced",
            random_state=42,
            n_jobs=1,
        )
        self._model.fit(X, y)
        self._trained = True

        # Feature importance
        self._feature_importance = dict(
            sorted(
                (
                    (n, round(v, 4))
                    for n, v in zip(CSI_FEATURE_NAMES, self._model.feature_importances_)
                ),
                key=lambda kv: -kv[1],
            )
        )

        metrics = {
            "n_train": len(X),
            "n_features": len(X[0]),
            "n_classes": n_classes,
            "class_distribution": {
                CSI_CLASSES[c]: y.count(c) for c in sorted(set(y))
            },
            "feature_importance": dict(
                list(self._feature_importance.items())[:15]
            ),
        }

        print(f"\n  Top-15 feature importance:")
        for name, imp in list(self._feature_importance.items())[:15]:
            bar = "█" * max(1, int(imp * 60))
            print(f"    {name:>28}: {imp:.4f}  {bar}")

        return metrics

    # ---- Persistence ----

    def save(self, path: str = CSI_MODEL_PATH) -> str:
        """Salva modello con joblib."""
        if not self._trained:
            raise RuntimeError("Modello non addestrato, chiama train() prima")
        if _joblib is None:
            raise RuntimeError("joblib non installato (pip install joblib)")

        _joblib.dump(self._model, path)
        print(f"  [CSIClassifier] Modello salvato: {path}")
        return path

    def load(self, path: str = CSI_MODEL_PATH) -> bool:
        """Carica modello salvato."""
        self._check_sklearn()
        if _joblib is None:
            raise RuntimeError("joblib non installato (pip install joblib)")
        if not os.path.exists(path):
            print(f"  [CSIClassifier] Modello non trovato: {path}")
            return False

        self._model = _joblib.load(path)
        self._trained = True
        print(f"  [CSIClassifier] Modello caricato: {path}")
        return True

    # ---- Real-time inference ----

    def add_frame(self, frame: dict) -> None:
        """Aggiunge un frame CSI per inferenza."""
        self.frame_hist.append(frame)
        if self._t0 == 0:
            self._t0 = time.time()

    def predict_proba(self) -> dict:
        """Probabilità per ogni classe.

        Returns:
            dict {EMPTY: 0.1, STATIONARY: 0.7, MOVEMENT: 0.2}
            Tutte a 0.0 se non pronto.
        """
        if not self.ready:
            return {lbl: 0.0 for lbl in CSI_LABELS}
        assert self._model is not None  # garantito da self.ready

        vec = csi_window_to_vector(list(self.frame_hist))
        if vec is None:
            return {lbl: 0.0 for lbl in CSI_LABELS}

        probas = self._model.predict_proba([vec])[0]

        # Mappa probabilità alle classi
        result = {}
        for i, cls in enumerate(self._model.classes_):
            result[CSI_CLASSES[int(cls)]] = round(float(probas[i]), 4)

        # Assicura che tutte le classi siano presenti
        for lbl in CSI_LABELS:
            if lbl not in result:
                result[lbl] = 0.0

        self._last_probas = result
        self._last_features = {
            k: v for k, v in zip(CSI_FEATURE_NAMES, vec)
        }
        return result

    def predict(self) -> str:
        """Classe predetta."""
        if not self.ready:
            return "UNKNOWN"
        assert self._model is not None  # garantito da self.ready
        vec = csi_window_to_vector(list(self.frame_hist))
        if vec is None:
            return "UNKNOWN"
        cls = int(self._model.predict([vec])[0])
        return CSI_CLASSES.get(cls, "UNKNOWN")

    def get_features(self) -> dict:
        """Ultime feature estratte (per debug)."""
        return dict(self._last_features)

    def get_info(self) -> dict:
        return {
            "trained": self._trained,
            "window_frames": self.window_frames,
            "buffer_fill": len(self.frame_hist),
            "ready": self.ready,
            "last_probas": dict(self._last_probas),
            "feature_importance": dict(
                list(self._feature_importance.items())[:10]
            ),
        }


# ============================================================
# CLI
# ============================================================

def main():
    """CLI minima per training offline."""
    import argparse

    parser = argparse.ArgumentParser(description="CSI ML Classifier — training e test")
    parser.add_argument("--train", nargs="+", metavar=("EMPTY.json", "[STATIONARY.json]", "MOVEMENT.json"),
                        help="File JSON con frame etichettati (min 2: EMPTY MOVEMENT)")
    parser.add_argument("--save", default=CSI_MODEL_PATH,
                        help="Percorso modello (default: csi_model.joblib)")
    parser.add_argument("--load", type=str, help="Carica modello e mostra info")
    args = parser.parse_args()

    if args.train:
        if len(args.train) < 2:
            print("ERRORE: servono almeno EMPTY.json e MOVEMENT.json")
            sys.exit(1)

        labeled = {}
        for path in args.train:
            if not os.path.exists(path):
                print(f"ERRORE: file non trovato: {path}")
                sys.exit(1)
            with open(path) as f:
                data = json.load(f)
            frames = data if isinstance(data, list) else data.get("frames", data)
            # Estrai label dal nome file o dal contenuto
            label = "EMPTY"
            for kw in ("EMPTY", "STATIONARY", "MOVEMENT", "empty", "stationary", "movement"):
                if kw.lower() in os.path.basename(path).lower():
                    label = kw.upper()
                    break
            labeled[label] = frames
            print(f"  Caricati {len(frames)} frame da {path} → {label}")

        clf = CSIClassifier(window_frames=30)
        metrics = clf.train(
            labeled.get("EMPTY", []),
            labeled.get("STATIONARY"),
            labeled.get("MOVEMENT", []),
        )
        clf.save(args.save)

    elif args.load:
        if not os.path.exists(args.load):
            print(f"ERRORE: modello non trovato: {args.load}")
            sys.exit(1)
        clf = CSIClassifier()
        clf.load(args.load)
        info = clf.get_info()
        print(f"  Addestrato: {info['trained']}")
        print(f"  Finestra: {info['window_frames']} frame")
        print(f"  Feature importance top-10:")
        for name, imp in info["feature_importance"].items():
            print(f"    {name}: {imp:.4f}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
