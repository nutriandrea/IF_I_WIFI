"""MultiAPCSIClassifier — 3 AP triangulation from CSI."""
import json
import os
import time
from collections import deque

from .features import (
    GLOBAL_FEATURE_NAMES, CSI_FEATURE_NAMES, CSI_FEATURE_SIZE,
    extract_csi_profile, csi_window_to_vector,
    _extract_source_profile, _generate_source_feature_names,
    extract_csi_profile_per_source, csi_window_to_vector_per_source,
)
from .classifier import CSI_LABELS

_NUMPY_AVAILABLE = False
try:
    import numpy as _np
    _NUMPY_AVAILABLE = True
except ImportError:
    _np = None

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

class MultiAPCSIClassifier:
    """
    Random Forest multi-class classifier per CSI da 3 AP simultanei.
    Mantiene finestre separate per ogni AP (0, 1, 2), estrae feature
    per-AP e concatena in un vettore unico da 243 feature.

    Uso:
        clf = MultiAPCSIClassifier(window_frames=30, num_aps=3)
        clf.train(empty_frames, stationary_frames, movement_frames)

        clf.add_frame(frame)  # frame["ap_id"] in {0, 1, 2}
        if clf.ready:
            probs = clf.predict_proba()
            cls = clf.predict()
    """

    def __init__(self, window_frames: int = 30, num_aps: int = 3):
        self.num_aps = num_aps
        self.window_size = window_frames
        self.ap_windows: dict = {
            i: deque(maxlen=window_frames) for i in range(num_aps)
        }
        self._model = None
        self._trained = False
        self._total_feature_size = CSI_FEATURE_SIZE * num_aps
        self._class_labels: list[str] | None = None  # label personalizzate (posizioni)
        self._known_sources: list[str] = []
        self._source_key: str = "mac"

    # ---- Properties ----

    @property
    def trained(self) -> bool:
        return self._trained

    @property
    def ready(self) -> bool:
        if not self._trained:
            return False
        return all(len(w) == self.window_size for w in self.ap_windows.values())

    # ---- Data ingestion ----

    def add_frame(self, frame: dict):
        ap_id = frame.get("ap_id", 0)
        if 0 <= ap_id < self.num_aps:
            self.ap_windows[ap_id].append(frame)

    def _build_feature_vector(self) -> list | None:
        """Concatena feature vector da tutti gli AP."""
        vec = []
        for ap_id in range(self.num_aps):
            window = list(self.ap_windows[ap_id])
            if len(window) < 2:
                return None
            ap_vec = csi_window_to_vector(window)
            if ap_vec is None:
                return None
            vec.extend(ap_vec)
        return vec

    # ---- sklearn helpers ----

    @staticmethod
    def _check_sklearn():
        if not _SKLEARN_AVAILABLE:
            raise RuntimeError(
                "scikit-learn non installato.\n"
                "  UNO Q: sudo apt install python3-sklearn python3-joblib\n"
                "  Mac:   pip install scikit-learn joblib"
            )

    # ---- Training ----

    def train(self, empty_frames: list, stationary_frames: list | None,
              movement_frames: list) -> dict:
        self._check_sklearn()
        assert _RF_CLASS is not None

        if not empty_frames or not movement_frames:
            raise ValueError("Servono campioni empty e movement")

        print(f"  [MultiAPCSIClassifier] Training: {len(empty_frames)} empty"
              f" + {len(stationary_frames or [])} stationary"
              f" + {len(movement_frames)} movement")

        X, y = [], []
        for label, frames in [(0, empty_frames), (1, stationary_frames),
                              (2, movement_frames)]:
            if not frames:
                continue
            # Per-AP buffer
            ap_buf: dict = {i: deque(maxlen=self.window_size)
                            for i in range(self.num_aps)}
            for frame in frames:
                ap_id = frame.get("ap_id", 0)
                if 0 <= ap_id < self.num_aps:
                    ap_buf[ap_id].append(frame)
                # Quando tutti gli AP hanno finestra piena, estrai feature
                if all(len(w) == self.window_size for w in ap_buf.values()):
                    vec = []
                    for aid in range(self.num_aps):
                        av = csi_window_to_vector(list(ap_buf[aid]))
                        if av:
                            vec.extend(av)
                    if len(vec) == self._total_feature_size:
                        X.append(vec)
                        y.append(label)

        if len(X) < 10:
            raise ValueError(
                f"Pochi feature vector: {len(X)} (servono >=10). "
                f"Servono almeno {self.window_size} frame per ciascuno dei {self.num_aps} AP."
            )

        print(f"  Feature matrix: {len(X)} righe x {len(X[0])} colonne")
        print(f"  Classi: {set(y)}")

        self._model = _RF_CLASS(n_estimators=100, random_state=42)
        self._model.fit(X, y)
        self._trained = True

        metrics = {
            "n_train": len(X),
            "n_features": len(X[0]),
            "n_classes": len(set(y)),
            "feature_importance": {},
        }
        if (hasattr(self._model, "feature_importances_")
                and self._model.feature_importances_ is not None):
            for i, imp in enumerate(self._model.feature_importances_):
                name = (MULTI_AP_FEATURE_NAMES[i]
                        if i < len(MULTI_AP_FEATURE_NAMES) else f"feat_{i}")
                metrics["feature_importance"][name] = round(imp, 4)

        print(f"  Modello addestrato: {metrics['n_train']} campioni, "
              f"{metrics['n_features']} feature")
        return metrics

    # ---- Inference ----

    def predict_proba(self) -> dict | float:
        if not self._trained:
            return -1.0
        vec = self._build_feature_vector()
        if vec is None:
            return -1.0
        probas = self._model.predict_proba([vec])[0]
        # Mappa le classi del modello alle etichette CSI_LABELS
        result = {}
        for i, cls in enumerate(self._model.classes_):
            result[CSI_LABELS[int(cls)]] = probas[i]
        # Classe mancante → probabilità 0
        for lbl in CSI_LABELS:
            result.setdefault(lbl, 0.0)
        return result

    def predict(self) -> str:
        probs = self.predict_proba()
        if not isinstance(probs, dict):
            return CSI_LABELS[0]
        return max(probs, key=probs.get)

    # ---- Persistence ----

    def save(self, path: str | None = None):
        if not self._trained:
            raise RuntimeError("Nessun modello da salvare")
        save_path = path or CSI_MODEL_PATH.replace(".joblib", "_multi.joblib")
        if _joblib:
            _joblib.dump(self._model, save_path)
            print(f"  Modello MultiAP salvato: {save_path}")
        else:
            raise RuntimeError("joblib non installato")

    def load(self, path: str | None = None) -> bool:
        load_path = path or CSI_MODEL_PATH.replace(".joblib", "_multi.joblib")
        if not os.path.exists(load_path):
            return False
        if _joblib:
            self._model = _joblib.load(load_path)
            self._trained = True
            return True
        return False


