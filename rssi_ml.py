#!/usr/bin/env python3
"""
RSSI ML Classifier — Random Forest per rilevamento presenza via RSSI.

Sostituisce GradientDetector con un classificatore Random Forest che:
  - Impara il pattern di rumore specifico dell'ambiente
  - Produce una probabilità di presenza (0.0–1.0) invece di un punteggio ad-hoc
  - Si adatta a frequenze di campionamento variabili (5–20 Hz)

Feature ingegnerizzate da finestra mobile di RSSI:
  - mean, std, min, max, range
  - gradient_mean, gradient_std, gradient_max_abs
  - zero_crossing_rate

Uso:
  from rssi_ml import RSSIClassifier

  clf = RSSIClassifier(window_size=20)
  # Training
  clf.train(baseline_samples, movement_samples)
  clf.save()

  # Inference
  clf.add_sample(rssi)
  prob = clf.predict_proba()  # → 0.0-1.0

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

# sklearn: import lazy — il file deve poter essere importato senza sklearn.
# Su UNO Q: apt install python3-sklearn python3-joblib
# Su Mac:   pip install scikit-learn joblib
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
RSSI_MODEL_PATH = os.path.join(MODEL_DIR, "rssi_model.joblib")

NUM_FEATURES = 9
FEATURE_NAMES = [
    "rssi_mean", "rssi_std", "rssi_min", "rssi_max", "rssi_range",
    "gradient_mean", "gradient_std", "gradient_max_abs", "zero_crossing_rate",
]


# ============================================================
# Feature extraction
# ============================================================

def extract_rssi_features(rssi_window: list) -> dict:
    """Estrae feature da una finestra di valori RSSI.

    Args:
        rssi_window: Lista di valori RSSI consecutivi (almeno 2).

    Returns:
        dict con feature calcolate, o {"_empty": True} se finestra troppo corta.
    """
    n = len(rssi_window)
    if n < 2:
        return {"_empty": True}

    feats = {
        "rssi_mean": round(mean(rssi_window), 2),
        "rssi_std": round(stdev(rssi_window), 2),
        "rssi_min": min(rssi_window),
        "rssi_max": max(rssi_window),
        "rssi_range": max(rssi_window) - min(rssi_window),
    }

    grads = [rssi_window[i + 1] - rssi_window[i] for i in range(n - 1)]
    abs_grads = [abs(g) for g in grads]

    feats["gradient_mean"] = round(mean(abs_grads), 3)
    feats["gradient_std"] = round(stdev(abs_grads), 3) if len(abs_grads) >= 2 else 0.0
    feats["gradient_max_abs"] = round(max(abs_grads), 3)

    # Zero crossing rate: frequenza con cui il gradiente cambia segno
    if len(grads) >= 2:
        sign_changes = sum(1 for i in range(len(grads) - 1) if grads[i] * grads[i + 1] < 0)
        feats["zero_crossing_rate"] = round(sign_changes / max(len(grads) - 1, 1), 4)
    else:
        feats["zero_crossing_rate"] = 0.0

    return feats


def rssi_window_to_vector(rssi_window: list) -> list | None:
    """Converte finestra RSSI in vettore feature flat per sklearn."""
    f = extract_rssi_features(rssi_window)
    if f.get("_empty"):
        return None
    return [f[n] for n in FEATURE_NAMES]


# ============================================================
# RSSIClassifier
# ============================================================

class RSSIClassifier:
    """
    Random Forest classifier per presenza da RSSI.

    Sostituisce GradientDetector: impara il pattern di rumore dell'ambiente
    e produce probabilità calibrate invece di punteggi ad-hoc.

    Usage:
        # Training dopo calibrazione
        clf = RSSIClassifier(window_size=20)
        clf.train(baseline_samples, movement_samples)
        clf.save()

        # Real-time inference
        clf.add_sample(rssi_value)
        if clf.ready:
            prob = clf.predict_proba()  # → 0.0–1.0
    """

    def __init__(self, window_size: int = 20):
        self.window_size = window_size
        self.rssi_hist: deque = deque(maxlen=window_size)
        self._model = None
        self._trained = False
        self._t0 = 0.0
        self._last_features: dict = {}
        self._last_proba: float = -1.0
        self._feature_importance: dict = {}

    # ---- Properties ----

    @property
    def ready(self) -> bool:
        """Pronto per inferenza: finestra piena e modello addestrato."""
        return self._trained and len(self.rssi_hist) == self.window_size

    @property
    def trained(self) -> bool:
        return self._trained

    def calibrated(self) -> bool:
        """Compatibilità API con GradientDetector."""
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

    def _samples_to_xy(self, samples: list, default_label: int = 0) -> tuple:
        """Converte lista campioni RSSI in matrice feature X, vettore label y."""
        X, y = [], []
        window = deque(maxlen=self.window_size)

        for s in samples:
            rssi = s.get("rssi", s.get("_rssi")) if isinstance(s, dict) else s
            if not isinstance(rssi, (int, float)):
                continue

            window.append(float(rssi))

            if len(window) == self.window_size:
                vec = rssi_window_to_vector(list(window))
                if vec is not None:
                    X.append(vec)
                    if isinstance(s, dict):
                        lbl = s.get("label", s.get("_label", ""))
                        y.append(1 if lbl in ("movement", "present") else 0)
                    else:
                        y.append(default_label)

        return X, y

    def train(self, baseline_samples: list, movement_samples: list) -> dict:
        """Addestra il Random Forest su dati di calibrazione.

        Args:
            baseline_samples: campioni stanza vuota (label=0)
            movement_samples: campioni con movimento (label=1)

        Returns:
            dict con metriche di training.
        """
        self._check_sklearn()
        assert _RF_CLASS is not None  # type narrowing

        if not baseline_samples or not movement_samples:
            raise ValueError("Servono campioni baseline e movement")

        bl = len(baseline_samples)
        mv = len(movement_samples)
        print(f"  [RSSIClassifier] Training: {bl} baseline + {mv} movement")

        X0, y0 = self._samples_to_xy(baseline_samples, 0)
        X1, y1 = self._samples_to_xy(movement_samples, 1)

        if len(X0) < 3:
            raise ValueError(f"Pochi feature vector da baseline: {len(X0)} (servono >=3)")
        if len(X1) < 3:
            raise ValueError(f"Pochi feature vector da movement: {len(X1)} (servono >=3)")

        X = X0 + X1
        y = y0 + y1

        print(f"  Feature matrix: {len(X)} righe x {len(X[0])} colonne")
        print(f"  Classi: 0={y.count(0)}, 1={y.count(1)}")

        self._model = _RF_CLASS(
            n_estimators=30,
            max_depth=5,
            class_weight="balanced",
            random_state=42,
            n_jobs=1,
        )
        self._model.fit(X, y)
        self._trained = True

        # Feature importance
        self._feature_importance = dict(
            sorted(
                ((n, round(v, 4)) for n, v in zip(FEATURE_NAMES, self._model.feature_importances_)),
                key=lambda kv: -kv[1],
            )
        )

        metrics = {
            "n_train": len(X),
            "n_features": len(X[0]),
            "n_baseline": len(y0),
            "n_movement": len(y1),
            "feature_importance": dict(self._feature_importance),
        }

        print(f"  Feature importance:")
        for name, imp in self._feature_importance.items():
            bar = "█" * max(1, int(imp * 60))
            print(f"    {name:>20}: {imp:.4f}  {bar}")

        return metrics

    @property
    def feature_importance(self) -> dict:
        return dict(self._feature_importance)

    # ---- Persistence ----

    def save(self, path: str = RSSI_MODEL_PATH) -> str:
        """Salva modello con joblib."""
        if not self._trained:
            raise RuntimeError("Modello non addestrato, chiama train() prima")
        if _joblib is None:
            raise RuntimeError("joblib non installato (pip install joblib)")

        _joblib.dump(self._model, path)
        print(f"  [RSSIClassifier] Modello salvato: {path}")
        return path

    def load(self, path: str = RSSI_MODEL_PATH) -> bool:
        """Carica modello salvato da joblib."""
        if not _SKLEARN_AVAILABLE:
            self._check_sklearn()
        if _joblib is None:
            raise RuntimeError("joblib non installato (pip install joblib)")
        if not os.path.exists(path):
            print(f"  [RSSIClassifier] Modello non trovato: {path}")
            return False

        self._model = _joblib.load(path)
        self._trained = True
        print(f"  [RSSIClassifier] Modello caricato: {path}")
        return True

    # ---- Real-time inference ----

    def add_sample(self, rssi: float) -> None:
        """Aggiunge un campione RSSI per inferenza in tempo reale."""
        self.rssi_hist.append(rssi)
        if self._t0 == 0:
            self._t0 = time.time()

    def predict_proba(self) -> float:
        """Probabilità di presenza sul buffer corrente.

        Returns:
            float 0.0–1.0, oppure -1.0 se non pronto.
        """
        if not self.ready:
            return -1.0
        assert self._model is not None  # garantito da self.ready

        vec = rssi_window_to_vector(list(self.rssi_hist))
        if vec is None:
            return -1.0

        proba = self._model.predict_proba([vec])[0]
        # Se classes_ è [0, 1], prob_present = proba[1]
        if len(proba) > 1 and self._model.classes_[1] == 1:
            self._last_proba = round(float(proba[1]), 4)
        else:
            self._last_proba = round(float(proba[0]), 4)

        self._last_features = dict(zip(FEATURE_NAMES, vec))
        return self._last_proba

    def predict(self) -> bool:
        """Predizione binaria: presenza True/False."""
        return self.predict_proba() >= 0.5

    def get_features(self) -> dict:
        """Ultime feature estratte (per debug)."""
        return dict(self._last_features)

    def get_info(self) -> dict:
        """Stato completo del classificatore."""
        return {
            "trained": self._trained,
            "window_size": self.window_size,
            "buffer_fill": len(self.rssi_hist),
            "ready": self.ready,
            "last_proba": self._last_proba,
            "feature_importance": self._feature_importance,
        }


# ============================================================
# CLI
# ============================================================

def main():
    """CLI minima per test e training offline."""
    import argparse

    parser = argparse.ArgumentParser(description="RSSI ML Classifier — training e test")
    parser.add_argument("--train", nargs=2, metavar=("BASELINE_JSON", "MOVEMENT_JSON"),
                        help="Addestra su file JSON di calibrazione")
    parser.add_argument("--save", default=RSSI_MODEL_PATH,
                        help="Percorso salvataggio modello (default: rssi_model.joblib)")
    parser.add_argument("--load", type=str, help="Carica modello e mostra info")
    args = parser.parse_args()

    if args.train:
        bl_file, mv_file = args.train
        for f in (bl_file, mv_file):
            if not os.path.exists(f):
                print(f"ERRORE: file non trovato: {f}")
                sys.exit(1)

        with open(bl_file) as f:
            baseline = json.load(f)
        with open(mv_file) as f:
            movement = json.load(f)

        bl_samples = baseline if isinstance(baseline, list) else baseline.get("samples", baseline)
        mv_samples = movement if isinstance(movement, list) else movement.get("samples", movement)

        print(f"Baseline: {len(bl_samples)} campioni, Movement: {len(mv_samples)} campioni")

        clf = RSSIClassifier(window_size=20)
        metrics = clf.train(bl_samples, mv_samples)
        clf.save(args.save)

        print(f"\nFeature importance:")
        for name, imp in metrics["feature_importance"].items():
            print(f"  {name}: {imp:.4f}")

    elif args.load:
        if not os.path.exists(args.load):
            print(f"ERRORE: modello non trovato: {args.load}")
            sys.exit(1)
        clf = RSSIClassifier()
        clf.load(args.load)
        print(clf.get_info())

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
