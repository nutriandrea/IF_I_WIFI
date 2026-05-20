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
  from csi.csi_ml import CSIClassifier, CSI_CLASSES, CSI_LABELS

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
POSITIONS_MODEL_PATH = os.path.join(MODEL_DIR, "csi_positions_model.joblib")
POSITIONS_LABELS_PATH = os.path.join(MODEL_DIR, "csi_positions_labels.json")

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
SUB_PHASE_MEAN_PREFIX = "phase_mean_"
SUB_PHASE_STD_PREFIX = "phase_std_"
NUM_CSI_SUBCARRIERS = 64  # prime 64 subcarrier su 128

# Numero totale feature = globali + 2 * NUM_CSI_SUBCARRIERS (mean + std per sub)
CSI_FEATURE_SIZE = len(GLOBAL_FEATURE_NAMES) + 2 * NUM_CSI_SUBCARRIERS

# Nomi completi delle feature (per export)
CSI_FEATURE_NAMES = (
    GLOBAL_FEATURE_NAMES
    + [f"{SUB_MEAN_PREFIX}{i}" for i in range(NUM_CSI_SUBCARRIERS)]
    + [f"{SUB_STD_PREFIX}{i}" for i in range(NUM_CSI_SUBCARRIERS)]
)

MAX_SUB = 128  # numero massimo di subcarrier nelle feature extraction


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

    # Allinea tutte le subcarrier alla stessa lunghezza (padding/trunc a MAX_SUB=128)
    global MAX_SUB
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
# Feature extraction per-MAC (per modello posizioni)
# ============================================================

def _extract_mac_profile(frames: list, mac: str | None = None) -> dict | None:
    """Estrae ampl_mean, ampl_std, phase_mean, phase_std per un singolo MAC.

    Args:
        frames: lista di frame CSI.
        mac: MAC address da filtrare (None = tutti i frame).

    Returns:
        dict con chiavi 'ampl_mean', 'ampl_std', 'phase_mean', 'phase_std',
        ciascuna lista di MAX_SUB valori. None se dati insufficienti.
    """
    if mac is not None:
        mac_frames = [f for f in frames if f.get("mac") == mac]
    else:
        mac_frames = frames

    if len(mac_frames) < 2:
        return None

    ampl_vectors = []
    phase_vectors = []
    for f in mac_frames:
        csi = f.get("csi")
        if not csi or not isinstance(csi, list):
            continue
        amps = [c.get("ampl", 0) for c in csi if isinstance(c, dict)]
        phases = [c.get("phase", 0) for c in csi if isinstance(c, dict)]
        if len(amps) < 2:
            continue
        ampl_vectors.append(amps)
        phase_vectors.append(phases)

    if not ampl_vectors:
        return None

    # Allinea a MAX_SUB
    aligned_amp = []
    for v in ampl_vectors:
        if len(v) < MAX_SUB:
            v = list(v) + [0.0] * (MAX_SUB - len(v))
        aligned_amp.append(v[:MAX_SUB])

    aligned_phase = []
    for v in phase_vectors:
        if len(v) < MAX_SUB:
            v = list(v) + [0.0] * (MAX_SUB - len(v))
        aligned_phase.append(v[:MAX_SUB])

    nf = len(aligned_amp)

    def _mean_col(idx):
        return mean(aligned_amp[j][idx] for j in range(nf))

    def _std_col(idx):
        return stdev([aligned_amp[j][idx] for j in range(nf)]) if nf >= 2 else 0.0

    def _phase_mean_col(idx):
        return mean(aligned_phase[j][idx] for j in range(nf))

    def _phase_std_col(idx):
        return stdev([aligned_phase[j][idx] for j in range(nf)]) if nf >= 2 else 0.0

    return {
        "ampl_mean": [_mean_col(i) for i in range(MAX_SUB)],
        "ampl_std": [_std_col(i) for i in range(MAX_SUB)],
        "phase_mean": [_phase_mean_col(i) for i in range(MAX_SUB)],
        "phase_std": [_phase_std_col(i) for i in range(MAX_SUB)],
    }


def _generate_position_feature_names(macs: list[str]) -> list[str]:
    """Genera feature names per modello posizioni con per-MAC + fase.

    Include feature globali + per ogni MAC: sub_mean_0..N-1, sub_std_0..N-1,
    phase_mean_0..N-1, phase_std_0..N-1.
    """
    names = list(GLOBAL_FEATURE_NAMES)  # inizia con feature globali
    for mac in macs:
        short_mac = mac[-8:] if len(mac) > 8 else mac  # ultimi 8 hex char
        for i in range(NUM_CSI_SUBCARRIERS):
            names.append(f"{short_mac}_{SUB_MEAN_PREFIX}{i}")
            names.append(f"{short_mac}_{SUB_STD_PREFIX}{i}")
            names.append(f"{short_mac}_{SUB_PHASE_MEAN_PREFIX}{i}")
            names.append(f"{short_mac}_{SUB_PHASE_STD_PREFIX}{i}")
    return names


def extract_csi_profile_per_mac(frames_window: list, known_macs: list[str]) -> dict:
    """Estrae feature per-MAC con fase + feature globali.

    Per ogni known_mac, estrae profilo ampl_mean/ampl_std/phase_mean/phase_std
    dai soli frame di quel MAC nella finestra. Se un MAC non ha frame sufficienti,
    le sue feature sono zero.

    Args:
        frames_window: Lista di dict CSI.
        known_macs: Lista di MAC address (stringhe 12 hex) da considerare.

    Returns:
        dict con feature + chiave 'window_frames', o {"_empty": True} se dati insufficienti.
    """
    # Feature globali da tutti i frame (usando extract_csi_profile)
    profile = extract_csi_profile(frames_window)
    if profile.get("_empty"):
        return {"_empty": True}

    # Per-MAC + phase feature per ogni MAC noto
    for mac in known_macs:
        mp = _extract_mac_profile(frames_window, mac)
        short_mac = mac[-8:] if len(mac) > 8 else mac

        if mp is None:
            # MAC assente nella finestra → feature zero
            for i in range(NUM_CSI_SUBCARRIERS):
                profile[f"{short_mac}_{SUB_MEAN_PREFIX}{i}"] = 0.0
                profile[f"{short_mac}_{SUB_STD_PREFIX}{i}"] = 0.0
                profile[f"{short_mac}_{SUB_PHASE_MEAN_PREFIX}{i}"] = 0.0
                profile[f"{short_mac}_{SUB_PHASE_STD_PREFIX}{i}"] = 0.0
        else:
            for i in range(NUM_CSI_SUBCARRIERS):
                profile[f"{short_mac}_{SUB_MEAN_PREFIX}{i}"] = round(mp["ampl_mean"][i], 4)
                profile[f"{short_mac}_{SUB_STD_PREFIX}{i}"] = round(mp["ampl_std"][i], 4)
                profile[f"{short_mac}_{SUB_PHASE_MEAN_PREFIX}{i}"] = round(mp["phase_mean"][i], 4)
                profile[f"{short_mac}_{SUB_PHASE_STD_PREFIX}{i}"] = round(mp["phase_std"][i], 4)

    profile["_macs"] = known_macs
    return profile


def csi_window_to_vector_per_mac(frames_window: list, known_macs: list[str],
                                  feature_names: list[str]) -> list | None:
    """Converte finestra in vettore feature flat per modello posizioni per-MAC."""
    f = extract_csi_profile_per_mac(frames_window, known_macs)
    if f.get("_empty"):
        return None
    return [f[n] for n in feature_names]


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
        self._class_labels: list[str] | None = None  # label personalizzate (posizioni)
        self._known_macs: list[str] = []  # MAC noti per modello multi-trasmettitore
        self._custom_feature_names: list[str] = []  # feature names per modello custom

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

    def _frames_to_xy_custom(self, frames: list, label: int,
                              known_macs: list[str],
                              feature_names: list[str]) -> tuple:
        """Converte lista frame in feature matrix per modello posizioni per-MAC."""
        X, y = [], []
        window = deque(maxlen=self.window_frames)

        for f in frames:
            window.append(f)
            if len(window) == self.window_frames:
                vec = csi_window_to_vector_per_mac(list(window), known_macs, feature_names)
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

    def train_custom(self, labeled_frames: dict[str, list]) -> dict:
        """Addestra con etichette personalizzate (es. posizioni).

        Usa feature multi-MAC + fase se più trasmettitori rilevati.

        Args:
            labeled_frames: dict {label_str: [frame_dict, ...]}

        Returns:
            dict con metriche di training.
        """
        self._check_sklearn()
        assert _RF_CLASS is not None

        if len(labeled_frames) < 2:
            raise ValueError("Servono almeno 2 classi")

        self._class_labels = list(labeled_frames.keys())

        # Scansiona tutti i frame per trovare MAC unici
        all_macs = set()
        for frames in labeled_frames.values():
            for f in frames:
                mac = f.get("mac")
                if mac and isinstance(mac, str):
                    all_macs.add(mac)
        self._known_macs = sorted(all_macs)
        if len(self._known_macs) >= 2:
            self._custom_feature_names = _generate_position_feature_names(self._known_macs)
            print(f"  [Posizioni] Rilevati {len(self._known_macs)} trasmettitori: {', '.join(m[-8:] for m in self._known_macs)}")
        else:
            self._known_macs = []
            self._custom_feature_names = []
            print(f"  [Posizioni] Nessun MAC multiplo rilevato, uso feature standard")

        use_per_mac = len(self._known_macs) >= 2

        X, y = [], []
        class_counts = {}
        for label_idx, (label_name, frames) in enumerate(labeled_frames.items()):
            if not frames:
                continue
            if use_per_mac:
                Xi, yi = self._frames_to_xy_custom(frames, label_idx,
                                                    self._known_macs,
                                                    self._custom_feature_names)
            else:
                Xi, yi = self._frames_to_xy(frames, label_idx)
            X.extend(Xi)
            y.extend(yi)
            class_counts[label_name] = len(yi)

        if len(X) < 10:
            raise ValueError(f"Troppi pochi campioni: {len(X)} (servono almeno 10)")

        n_features = len(X[0])
        feature_names = self._custom_feature_names if use_per_mac else CSI_FEATURE_NAMES
        print(f"  [Posizioni] Training: {len(X)} campioni x {n_features} feature")
        for name, count in class_counts.items():
            print(f"    {name}: {count}")

        self._model = _RF_CLASS(
            n_estimators=50,
            max_depth=8,
            class_weight="balanced",
            random_state=42,
            n_jobs=1,
        )
        self._model.fit(X, y)
        self._trained = True

        self._feature_importance = dict(
            sorted(
                (
                    (n, round(v, 4))
                    for n, v in zip(feature_names, self._model.feature_importances_)
                ),
                key=lambda kv: -kv[1],
            )
        )

        metrics = {
            "n_train": len(X),
            "n_features": n_features,
            "n_classes": len(labeled_frames),
            "class_distribution": class_counts,
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

    def save_custom(self, path: str = POSITIONS_MODEL_PATH,
                    labels_path: str = POSITIONS_LABELS_PATH) -> str:
        """Salva modello posizioni + etichette + MAC noti."""
        if not self._trained or self._class_labels is None:
            raise RuntimeError("Modello non addestrato con train_custom()")
        if _joblib is None:
            raise RuntimeError("joblib non installato (pip install joblib)")

        _joblib.dump(self._model, path)
        meta = {
            "class_labels": self._class_labels,
            "known_macs": self._known_macs,
        }
        with open(labels_path, "w") as f:
            json.dump(meta, f)
        print(f"  [Posizioni] Modello salvato: {path}")
        print(f"  [Posizioni] Etichette: {self._class_labels}")
        if self._known_macs:
            print(f"  [Posizioni] MAC: {[m[-8:] for m in self._known_macs]}")
        return path

    def load_custom(self, path: str = POSITIONS_MODEL_PATH,
                   labels_path: str = POSITIONS_LABELS_PATH) -> bool:
        """Carica modello posizioni + etichette + MAC noti."""
        self._check_sklearn()
        if _joblib is None:
            raise RuntimeError("joblib non installato (pip install joblib)")
        if not os.path.exists(path) or not os.path.exists(labels_path):
            return False

        self._model = _joblib.load(path)
        with open(labels_path) as f:
            data = json.load(f)

        # Supporta formato vecchio (solo lista) e nuovo (dict con class_labels/known_macs)
        if isinstance(data, list):
            self._class_labels = data
            self._known_macs = []
        else:
            self._class_labels = data.get("class_labels", [])
            self._known_macs = data.get("known_macs", [])

        if self._known_macs:
            self._custom_feature_names = _generate_position_feature_names(self._known_macs)
        else:
            self._custom_feature_names = []

        self._trained = True
        print(f"  [Posizioni] Modello caricato: {path}")
        print(f"  [Posizioni] Etichette: {self._class_labels}")
        if self._known_macs:
            print(f"  [Posizioni] MAC: {[m[-8:] for m in self._known_macs]}")
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
            dict {label: prob} — classi standard o custom se train_custom() usato.
            Tutte a 0.0 se non pronto.
        """
        if not self.ready:
            return {lbl: 0.0 for lbl in (self._class_labels or CSI_LABELS)}
        assert self._model is not None  # garantito da self.ready

        # Usa feature per-MAC se MAC noti, altrimenti feature standard
        use_per_mac = len(self._known_macs) >= 2
        if use_per_mac:
            vec = csi_window_to_vector_per_mac(
                list(self.frame_hist), self._known_macs, self._custom_feature_names
            )
            feature_domain = self._custom_feature_names
        else:
            vec = csi_window_to_vector(list(self.frame_hist))
            feature_domain = CSI_FEATURE_NAMES

        if vec is None:
            return {lbl: 0.0 for lbl in (self._class_labels or CSI_LABELS)}

        probas = self._model.predict_proba([vec])[0]

        # Mappa probabilità alle classi
        result = {}
        labels = self._class_labels or CSI_CLASSES
        for i, cls in enumerate(self._model.classes_):
            if self._class_labels:
                result[self._class_labels[int(cls)]] = round(float(probas[i]), 4)
            else:
                result[CSI_CLASSES[int(cls)]] = round(float(probas[i]), 4)

        # Assicura che tutte le classi siano presenti
        for lbl in labels:
            if lbl not in result:
                result[lbl] = 0.0

        self._last_probas = result
        self._last_features = {
            k: v for k, v in zip(feature_domain, vec)
        }
        return result

    def predict(self) -> str:
        """Classe predetta."""
        if not self.ready:
            return "UNKNOWN"
        assert self._model is not None  # garantito da self.ready

        use_per_mac = len(self._known_macs) >= 2
        if use_per_mac:
            vec = csi_window_to_vector_per_mac(
                list(self.frame_hist), self._known_macs, self._custom_feature_names
            )
        else:
            vec = csi_window_to_vector(list(self.frame_hist))

        if vec is None:
            return "UNKNOWN"
        cls = int(self._model.predict([vec])[0])
        if self._class_labels:
            return self._class_labels[cls]
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


# ============================================================
# MultiAPCSIClassifier — 3 AP triangulation (243 feature)
# ============================================================

MULTI_AP_FEATURE_SIZE = CSI_FEATURE_SIZE * 3  # 81 * 3 = 243

MULTI_AP_FEATURE_NAMES = [
    f"ap{ap_id}_{name}"
    for ap_id in range(3)
    for name in CSI_FEATURE_NAMES
]


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


# ============================================================
# RSSIFeatureExtractor — feature RSSI avanzate
# ============================================================
# Adattato da RuView (github.com/ruvnet/RuView) RssiFeatureExtractor

RSSI_FEATURE_NAMES = [
    "mean", "variance", "std", "skewness", "kurtosis",
    "range", "iqr",
    "dominant_freq_hz", "breathing_band_power",
    "motion_band_power", "total_spectral_power",
    "n_change_points",
]


class RSSIFeatures:
    """Feature RSSI: time-domain, frequency-domain, CUSUM change-points."""

    __slots__ = (
        "mean", "variance", "std", "skewness", "kurtosis",
        "range", "iqr",
        "dominant_freq_hz", "breathing_band_power",
        "motion_band_power", "total_spectral_power",
        "n_change_points", "n_samples", "duration_seconds", "sample_rate_hz",
    )

    def __init__(self):
        for s in self.__slots__:
            setattr(self, s, 0.0)
        self.n_samples = 0
        self.n_change_points = 0
        self.duration_seconds = 0.0
        self.sample_rate_hz = 0.0

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}

    def to_vector(self) -> list:
        return [getattr(self, n) for n in RSSI_FEATURE_NAMES]


try:
    import numpy as _np
    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False

try:
    from scipy import fft as _scipy_fft
    from scipy import stats as _scipy_stats
    _SCIPY_AVAILABLE = True
except ImportError:
    _SCIPY_AVAILABLE = False


class RSSIFeatureExtractor:
    """
    Feature extraction avanzata da serie temporale RSSI.

    - Time-domain: mean, variance, std, skewness, kurtosis, range, IQR
    - Frequency-domain: FFT con finestra Hann, bande respiratorie (0.1-0.5 Hz)
      e movimento (0.5-3.0 Hz), frequenza dominante
    - Change-point: CUSUM change-point detection

    Parameters
    ----------
    window_seconds : float
        Finestra temporale per l'analisi (default 30).
    cusum_threshold : float
        Soglia CUSUM in deviazioni standard (default 3.0).
    cusum_drift : float
        Drift CUSUM in deviazioni standard (default 0.5).
    """

    def __init__(
        self,
        window_seconds: float = 30.0,
        cusum_threshold: float = 3.0,
        cusum_drift: float = 0.5,
    ):
        self._window_seconds = window_seconds
        self._cusum_threshold = cusum_threshold
        self._cusum_drift = cusum_drift

    def extract(self, rssi_values: list, timestamps: list | None = None,
                sample_rate: float | None = None) -> RSSIFeatures:
        """Estrae feature da una lista di valori RSSI."""
        feats = RSSIFeatures()
        feats.n_samples = len(rssi_values)

        if len(rssi_values) < 4:
            return feats

        # Stima sample rate
        if sample_rate is not None:
            sr = sample_rate
        elif timestamps and len(timestamps) > 1:
            sr = 1.0 / (sum(timestamps[i] - timestamps[i-1]
                           for i in range(1, len(timestamps))) / (len(timestamps) - 1))
        else:
            sr = 10.0  # fallback

        feats.sample_rate_hz = sr
        if timestamps and len(timestamps) > 1:
            feats.duration_seconds = timestamps[-1] - timestamps[0]

        if _NUMPY_AVAILABLE:
            rssi = _np.array(rssi_values, dtype=_np.float64)
            self._compute_time_domain(rssi, feats)
            if _SCIPY_AVAILABLE:
                self._compute_frequency_domain(rssi, sr, feats)
            self._compute_change_points(rssi, feats)
        else:
            # Fallback numpy-free minimale
            self._compute_time_domain_fallback(rssi_values, feats)

        return feats

    def _compute_time_domain(self, rssi: "_np.ndarray", feats: RSSIFeatures):
        feats.mean = float(_np.mean(rssi))
        feats.variance = float(_np.var(rssi, ddof=1)) if len(rssi) > 1 else 0.0
        feats.std = float(_np.std(rssi, ddof=1)) if len(rssi) > 1 else 0.0
        feats.range = float(_np.ptp(rssi))

        if feats.std < 1e-12:
            feats.skewness = 0.0
            feats.kurtosis = 0.0
        else:
            feats.skewness = float(_scipy_stats.skew(rssi, bias=False)) if len(rssi) > 2 else 0.0
            feats.kurtosis = float(_scipy_stats.kurtosis(rssi, bias=False)) if len(rssi) > 3 else 0.0

        q75, q25 = _np.percentile(rssi, [75, 25])
        feats.iqr = float(q75 - q25)

    @staticmethod
    def _compute_time_domain_fallback(rssi_values: list, feats: RSSIFeatures):
        """Fallback minimale senza numpy."""
        n = len(rssi_values)
        if n == 0:
            return
        feats.mean = sum(rssi_values) / n
        if n > 1:
            feats.variance = sum((v - feats.mean) ** 2 for v in rssi_values) / (n - 1)
            feats.std = feats.variance ** 0.5
        feats.range = max(rssi_values) - min(rssi_values)
        sorted_vals = sorted(rssi_values)
        feats.iqr = sorted_vals[int(n * 0.75)] - sorted_vals[int(n * 0.25)]

    def _compute_frequency_domain(
        self, rssi: "_np.ndarray", sample_rate: float, feats: RSSIFeatures
    ):
        """FFT con finestra Hann, band power."""
        n = len(rssi)
        if n < 4:
            return

        signal = rssi - _np.mean(rssi)
        window = _np.hanning(n)
        windowed = signal * window

        fft_vals = _scipy_fft.rfft(windowed)
        freqs = _scipy_fft.rfftfreq(n, d=1.0 / sample_rate)
        psd = (_np.abs(fft_vals) ** 2) / n

        if len(freqs) <= 1:
            return

        freqs_no_dc = freqs[1:]
        psd_no_dc = psd[1:]
        feats.total_spectral_power = float(_np.sum(psd_no_dc))

        peak_idx = int(_np.argmax(psd_no_dc))
        feats.dominant_freq_hz = float(freqs_no_dc[peak_idx])

        feats.breathing_band_power = float(
            _band_power(freqs_no_dc, psd_no_dc, 0.1, 0.5)
        )
        feats.motion_band_power = float(
            _band_power(freqs_no_dc, psd_no_dc, 0.5, 3.0)
        )

    def _compute_change_points(self, rssi: "_np.ndarray", feats: RSSIFeatures):
        """CUSUM change-point detection."""
        if len(rssi) < 4:
            return
        mean_val = _np.mean(rssi)
        std_val = _np.std(rssi, ddof=1)
        if std_val < 1e-12:
            feats.n_change_points = 0
            return
        threshold = self._cusum_threshold * std_val
        drift = self._cusum_drift * std_val
        feats.n_change_points = _cusum_detect(rssi, mean_val, threshold, drift)


def _band_power(
    freqs: "_np.ndarray", psd: "_np.ndarray", low_hz: float, high_hz: float
) -> float:
    """Somma PSD in banda [low_hz, high_hz]."""
    mask = (freqs >= low_hz) & (freqs <= high_hz)
    return float(_np.sum(psd[mask]))


def _cusum_detect(
    signal: "_np.ndarray", target: float, threshold: float, drift: float
) -> int:
    """
    CUSUM change-point detection.
    Ritorna il numero di change-point trovati.
    """
    n = len(signal)
    s_pos = 0.0
    s_neg = 0.0
    count = 0
    for i in range(n):
        deviation = float(signal[i]) - target
        s_pos = max(0.0, s_pos + deviation - drift)
        s_neg = max(0.0, s_neg - deviation - drift)
        if s_pos > threshold or s_neg > threshold:
            count += 1
            s_pos = 0.0
            s_neg = 0.0
    return count


# ============================================================
# RuleBasedClassifier — classificatore rule-based ternario
# ============================================================
# Adattato da RuView (github.com/ruvnet/RuView) PresenceClassifier

class RuleBasedResult:
    """Risultato classificazione rule-based."""

    __slots__ = (
        "label", "confidence", "presence_detected",
        "rssi_variance", "motion_band_energy",
        "breathing_band_energy", "n_change_points", "details",
    )

    def __init__(
        self,
        label: str = "EMPTY",
        confidence: float = 0.0,
        presence_detected: bool = False,
        rssi_variance: float = 0.0,
        motion_band_energy: float = 0.0,
        breathing_band_energy: float = 0.0,
        n_change_points: int = 0,
        details: str = "",
    ):
        self.label = label
        self.confidence = confidence
        self.presence_detected = presence_detected
        self.rssi_variance = rssi_variance
        self.motion_band_energy = motion_band_energy
        self.breathing_band_energy = breathing_band_energy
        self.n_change_points = n_change_points
        self.details = details

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}


class RuleBasedClassifier:
    """
    Classificatore rule-based ternario ABSENT/STILL/ACTIVE.

    Regole:
      1. presence = RSSI variance >= presence_variance_threshold
      2. ABSENT       se NOT presence
      3. ACTIVE       se presence AND motion_band_energy >= motion_energy_threshold
      4. PRESENT_STILL altrimenti (presence ma motion basso)

    Confidence:
      - Base (60%): quanto variance supera (o cade sotto) la soglia
      - Spettrale (20%): energia banda rilevante
      - Default agreement (20%): sempre 1.0 per singolo ricevitore
    """

    def __init__(
        self,
        presence_variance_threshold: float = 0.5,
        motion_energy_threshold: float = 0.1,
    ):
        self._var_thresh = presence_variance_threshold
        self._motion_thresh = motion_energy_threshold

    def classify(self, features: RSSIFeatures | None = None, **kwargs) -> RuleBasedResult:
        """
        Classifica da RSSIFeatures o da kwargs.

        Parameters
        ----------
        features : RSSIFeatures, optional
        **kwargs : sovrascrive campioni da features
            variance, motion_band_power, breathing_band_power, n_change_points
        """
        if features is not None:
            variance = features.variance
            motion_energy = features.motion_band_power
            breathing_energy = features.breathing_band_power
            n_cp = features.n_change_points
        else:
            variance = kwargs.get("variance", 0.0)
            motion_energy = kwargs.get("motion_band_power", 0.0)
            breathing_energy = kwargs.get("breathing_band_power", 0.0)
            n_cp = kwargs.get("n_change_points", 0)

        # Sovrascrittura esplicita
        if "variance" in kwargs:
            variance = kwargs["variance"]
        if "motion_band_power" in kwargs:
            motion_energy = kwargs["motion_band_power"]
        if "breathing_band_power" in kwargs:
            breathing_energy = kwargs["breathing_band_power"]
        if "n_change_points" in kwargs:
            n_cp = kwargs["n_change_points"]

        presence = variance >= self._var_thresh

        if not presence:
            label = "EMPTY"
        elif motion_energy >= self._motion_thresh:
            label = "MOVEMENT"
        else:
            label = "STATIONARY"

        confidence = self._compute_confidence(
            variance, motion_energy, breathing_energy, label
        )

        details = (
            f"var={variance:.4f} (thresh={self._var_thresh}), "
            f"motion_energy={motion_energy:.4f} (thresh={self._motion_thresh}), "
            f"breathing_energy={breathing_energy:.4f}, "
            f"change_points={n_cp}"
        )

        return RuleBasedResult(
            label=label,
            confidence=confidence,
            presence_detected=presence,
            rssi_variance=variance,
            motion_band_energy=motion_energy,
            breathing_band_energy=breathing_energy,
            n_change_points=n_cp,
            details=details,
        )

    def _compute_confidence(
        self, variance: float, motion_energy: float,
        breathing_energy: float, label: str,
    ) -> float:
        """Confidence score in [0, 1]."""
        # Base confidence (60%)
        if label == "EMPTY":
            base = max(0.0, 1.0 - variance / self._var_thresh) if self._var_thresh > 0 else 1.0
        else:
            ratio = variance / self._var_thresh if self._var_thresh > 0 else 10.0
            base = min(1.0, ratio)

        # Spectral confidence (20%)
        if label == "MOVEMENT":
            spectral = min(1.0, motion_energy / max(self._motion_thresh, 1e-12))
        elif label == "STATIONARY":
            spectral = min(1.0, breathing_energy / max(self._motion_thresh, 1e-12))
        else:
            spectral = 1.0

        # Agreement (20%) — default 1.0 per singolo ricevitore
        confidence = 0.6 * base + 0.2 * spectral + 0.2
        return max(0.0, min(1.0, confidence))


# ============================================================
# DopplerShiftExtractor — shift Doppler da fase CSI
# ============================================================

DOPPLER_FEATURE_NAMES = [
    "mean_doppler", "std_doppler",
    "max_positive_doppler", "max_negative_doppler",
    "doppler_abs_max", "doppler_band_power",
]


class DopplerShiftExtractor:
    """
    Estrae lo spostamento Doppler dal segnale CSI.

    Il Doppler shift e' calcolato dalla differenza di fase tra frame
    consecutivi:
        f_Doppler = (dφ/dt) / (2π)

    Feature:
      - mean_doppler:         frequenza Doppler media (Hz)
      - std_doppler:          deviazione standard
      - max_positive_doppler: massimo shift positivo (avvicinamento)
      - max_negative_doppler: minimo shift negativo (allontanamento)
      - doppler_abs_max:      massimo |Doppler|
      - doppler_band_power:   energia spettrale banda 0.5-10 Hz

    Parameters
    ----------
    window_frames : int
        Numero frame nella finestra scorrevole (default 30).
    sample_rate_hz : float
        Frequenza di campionamento CSI (default 50 Hz).
    phase_sanitizer : PhaseSanitizer, optional
        PhaseSanitizer per preprocessing fase (default: auto).
    """

    def __init__(
        self,
        window_frames: int = 30,
        sample_rate_hz: float = 50.0,
        phase_sanitizer=None,
    ):
        self.window_size = window_frames
        self.sample_rate = sample_rate_hz
        self._buffer: deque = deque(maxlen=window_frames)
        if phase_sanitizer is not None:
            self._phase_sanitizer = phase_sanitizer
        else:
            from .phase_sanitizer import PhaseSanitizer
            self._phase_sanitizer = PhaseSanitizer()

    def add_frame(self, frame: dict):
        """Aggiunge un frame CSI al buffer."""
        self._buffer.append(frame)

    @property
    def ready(self) -> bool:
        """Vero se ci sono abbastanza frame per calcolare il Doppler."""
        return len(self._buffer) >= 3

    @property
    def n_frames(self) -> int:
        return len(self._buffer)

    def compute(self) -> dict:
        """
        Calcola le feature Doppler sul buffer corrente.

        Returns
        -------
        dict con feature Doppler, o dict vuoto se non pronto.
        """
        if not self.ready or not _NUMPY_AVAILABLE:
            return {}

        frames = list(self._buffer)
        phase_frames = _extract_phase_vectors(frames)
        if phase_frames is None or len(phase_frames) < 3:
            return {}

        # Sanitize phase
        phase_clean = self._phase_sanitizer.sanitize(phase_frames)

        # Phase difference lungo il tempo (axis=0)
        phase_diff = _np.diff(phase_clean, axis=0)
        # Normalizza in [-π, π]
        phase_diff = (phase_diff + _np.pi) % (2 * _np.pi) - _np.pi

        # Doppler: f = Δφ / (2π * Δt)
        dt = 1.0 / self.sample_rate
        doppler = phase_diff / (2 * _np.pi * dt)  # Hz

        # Media tra subcarrier
        doppler_mean = _np.mean(doppler, axis=1)

        if len(doppler_mean) == 0:
            return {}

        result: dict = {
            "mean_doppler": float(_np.mean(doppler_mean)),
            "std_doppler": float(_np.std(doppler_mean)),
            "max_positive_doppler": float(_np.max(doppler_mean)),
            "max_negative_doppler": float(_np.min(doppler_mean)),
            "doppler_abs_max": float(_np.max(_np.abs(doppler_mean))),
            "doppler_band_power": 0.0,
        }

        # Banda spettrale Doppler (0.5-10 Hz) se scipy disponibile
        if _SCIPY_AVAILABLE and len(doppler_mean) >= 8:
            signal = doppler_mean - _np.mean(doppler_mean)
            window = _np.hanning(len(signal))
            windowed = signal * window
            fft_vals = _scipy_fft.rfft(windowed)
            freqs = _scipy_fft.rfftfreq(len(signal), d=dt)
            psd = (_np.abs(fft_vals) ** 2) / len(signal)
            if len(freqs) > 1:
                mask = (freqs >= 0.5) & (freqs <= 10.0)
                result["doppler_band_power"] = float(_np.sum(psd[mask]))

        return result


def _extract_phase_vectors(frames: list) -> "_np.ndarray | None":
    """Estrae matrice fase (n_frames, n_sub) da lista frame CSI."""
    if not _NUMPY_AVAILABLE:
        return None
    phase_frames = []
    for f in frames:
        csi = f.get("csi")
        if not csi or not isinstance(csi, list):
            continue
        phases = [c.get("phase", 0.0) for c in csi if isinstance(c, dict)]
        if len(phases) >= 4:
            phase_frames.append(phases)
    if len(phase_frames) < 3:
        return None
    return _np.array(phase_frames, dtype=_np.float64)


# ============================================================
# SleepQualityAnalyzer — respirazione e qualita' sonno
# ============================================================

SLEEP_FEATURE_NAMES = [
    "breathing_rate_bpm",
    "breathing_regularity",
    "sleep_stage",
    "sleep_confidence",
    "apnea_detected",
    "breathing_band_power",
    "change_points",
]


class SleepQualityAnalyzer:
    """
    Analizza qualita' del sonno e respirazione da segnale RSSI.

    Usa la banda 0.1-0.5 Hz per estrarre:
      - Frequenza respiratoria (BPM)
      - Regolarita' del respiro
      - Stima stadio sonno: AWAKE / REM / LIGHT / DEEP
      - Rilevazione apnea (calo prolungato energia respiratoria)

    Parameters
    ----------
    window_seconds : float
        Finestra di analisi (default 60 s per respirazione).
    sample_rate_hz : float
        Frequenza campionamento RSSI (default 10 Hz).
    """

    # Bande per stima stadio sonno (BPM)
    _DEEP_RANGE = (6, 16)
    _LIGHT_RANGE = (12, 20)
    _REM_RANGE = (16, 24)

    # Soglia apnea: energia banda respiratoria sotto questa frazione
    # del massimo storico viene marcata come possibile apnea
    _APNEA_THRESHOLD = 0.02

    def __init__(
        self,
        window_seconds: float = 60.0,
        sample_rate_hz: float = 10.0,
    ):
        self._window_seconds = window_seconds
        self._sample_rate = sample_rate_hz
        self._extractor = RSSIFeatureExtractor(
            window_seconds=window_seconds,
            cusum_threshold=2.0,
            cusum_drift=0.3,
        )
        # Storico per rilevamento trend apnea
        self._breathing_history: deque = deque(maxlen=10)

    def analyze(
        self,
        rssi_values: list,
        timestamps: list | None = None,
        sample_rate: float | None = None,
    ) -> dict:
        """
        Analizza finestra di valori RSSI.

        Parameters
        ----------
        rssi_values : list
            Valori RSSI in dBm.
        timestamps : list, optional
            Timestamp Unix per ogni campione.
        sample_rate : float, optional
            Frequenza campionamento (override default).

        Returns
        -------
        dict con: breathing_rate_bpm, breathing_regularity,
        sleep_stage, sleep_confidence, apnea_detected,
        breathing_band_power, change_points.
        """
        sr = sample_rate or self._sample_rate
        feats = self._extractor.extract(rssi_values, timestamps, sr)

        # Frequenza respiratoria (Hz → BPM)
        breathing_rate_bpm = (
            feats.dominant_freq_hz * 60.0 if feats.dominant_freq_hz > 0 else 0.0
        )

        # Regolarita': 1.0 = perfettamente regolare, 0.0 = caotico
        # Usa il picco spettrale come proxy: piu' il picco e' stretto,
        # piu' la respirazione e' regolare
        if feats.breathing_band_power > 0:
            # std relativo alla media come misura di dispersione
            rel_std = feats.std / max(abs(feats.mean), 1e-6)
            breathing_regularity = max(0.0, 1.0 - min(rel_std, 1.0))
        else:
            breathing_regularity = 0.0

        # Stima stadio sonno
        stage, stage_conf = self._estimate_sleep_stage(
            breathing_rate_bpm, breathing_regularity, feats.breathing_band_power
        )

        # Apnea: energia respiratoria sotto soglia
        self._breathing_history.append(feats.breathing_band_power)
        apnea_detected = self._detect_apnea(feats.breathing_band_power)

        return {
            "breathing_rate_bpm": round(breathing_rate_bpm, 1),
            "breathing_regularity": round(breathing_regularity, 3),
            "sleep_stage": stage,
            "sleep_confidence": round(stage_conf, 3),
            "apnea_detected": apnea_detected,
            "breathing_band_power": round(feats.breathing_band_power, 6),
            "change_points": feats.n_change_points,
        }

    def _estimate_sleep_stage(
        self, bpm: float, regularity: float, band_power: float
    ) -> tuple:
        """Stima stadio sonno basato su BPM e regolarita'."""
        if band_power < self._APNEA_THRESHOLD * 0.5:
            return ("AWAKE", 0.0)

        deep_lo, deep_hi = self._DEEP_RANGE
        light_lo, light_hi = self._LIGHT_RANGE
        rem_lo, rem_hi = self._REM_RANGE

        if regularity > 0.6 and deep_lo <= bpm <= deep_hi:
            conf = min(1.0, regularity)
            return ("DEEP", conf)
        elif regularity > 0.3 and light_lo <= bpm <= light_hi:
            conf = min(1.0, regularity * 0.8 + 0.2)
            return ("LIGHT", conf)
        elif rem_lo <= bpm <= rem_hi:
            conf = 0.5 + 0.3 * regularity
            return ("REM", conf)
        else:
            conf = max(0.0, 0.5 - abs(bpm - 15) / 30)
            return ("AWAKE", conf)

    def _detect_apnea(self, current_power: float) -> bool:
        """
        Rileva possibile apnea: potenza attuale bassa rispetto allo
        storico recente.
        """
        if len(self._breathing_history) < 3:
            return False
        recent_max = max(self._breathing_history)
        if recent_max > 0:
            ratio = current_power / recent_max
            # Apnea se energia < 20% del massimo recente
            # E il massimo recente era significativo (> 0.01)
            return ratio < 0.2 and recent_max > 0.01
        return False

    def reset(self):
        """Resetta lo storico (tra sessioni di monitoraggio)."""
        self._breathing_history.clear()


if __name__ == "__main__":
    main()
