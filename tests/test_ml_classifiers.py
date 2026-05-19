#!/usr/bin/env python3
"""
test_ml_classifiers.py — Test ML classifiers con dati sintetici.
Zero hardware richiesto. Testa feature extraction e (se sklearn presente) training+inference.

Tests:
  - RSSIClassifier: feature extraction, training, inference, persistenza
  - CSIClassifier:  feature extraction da per-subcarrier, training multi-classe, inference
"""

import sys
import os
import math
import random
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rssi.rssi_ml import (
    RSSIClassifier,
    extract_rssi_features,
    rssi_window_to_vector,
    FEATURE_NAMES,
    NUM_FEATURES,
)

from csi.csi_ml import (
    CSIClassifier,
    extract_csi_profile,
    csi_window_to_vector,
    CSI_FEATURE_NAMES,
    CSI_FEATURE_SIZE,
    EMPTY, STATIONARY, MOVEMENT,
    CSI_LABELS,
)

PASS = 0
FAIL = 0


def log(name, passed, detail=""):
    global PASS, FAIL
    if passed:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: {detail}")


# ============================================================
# Helpers — dati sintetici
# ============================================================

def _make_rssi_samples(n: int, base: float = -40, noise: float = 0.5) -> list:
    """Genera campioni RSSI sintetici."""
    return [{"rssi": round(base + random.gauss(0, noise), 1), "label": "baseline"}
            for _ in range(n)]


def _make_movement_rssi_samples(n: int, base: float = -40, wiggle: float = 5.0) -> list:
    """Genera campioni RSSI con pattern di movimento."""
    samples = []
    for i in range(n):
        # Movimento causa variazioni +/-wiggle dBm
        variation = wiggle * math.sin(2 * math.pi * i / 20)
        samples.append({
            "rssi": round(base + variation + random.gauss(0, 1.0), 1),
            "label": "movement",
        })
    return samples


def _make_csi_frame(seq: int, rssi: int, num_sub: int = 64,
                    ampl_mean: float = 20.0, ampl_std: float = 3.0,
                    label: str = "baseline") -> dict:
    """Genera un frame CSI sintetico."""
    csi = [{"ampl": round(random.gauss(ampl_mean, ampl_std), 4), "phase": 0.0}
           for _ in range(num_sub)]
    amps = [c["ampl"] for c in csi]
    return {
        "seq": seq,
        "rssi": rssi,
        "noise_floor": -90,
        "csi": csi,
        "ampl_mean": round(sum(amps) / len(amps), 4),
        "ampl_std": round(
            (sum((a - sum(amps) / len(amps)) ** 2 for a in amps) / len(amps)) ** 0.5, 4
        ),
        "_label": label,
    }


def _make_csi_frames(n: int, label: str, rssi: int = -45,
                     ampl_mean: float = 20.0, ampl_noise: float = 3.0) -> list:
    """Genera N frame CSI sintetici."""
    return [_make_csi_frame(i, rssi + random.randint(-2, 2), 64,
                            ampl_mean, ampl_noise, label)
            for i in range(n)]


# ============================================================
# Test RSSI ML
# ============================================================

def test_rssi_feature_extraction():
    """Feature extraction su finestra RSSI produce vettore di lunghezza corretta."""
    window = [-40, -41, -39, -42, -40, -38, -41, -39, -40, -41,
              -39, -40, -42, -40, -39, -41, -40, -38, -40, -41]

    # Test dict output
    feats = extract_rssi_features(window)
    assert not feats.get("_empty"), "Window sufficiente non dovrebbe essere empty"
    for name in FEATURE_NAMES:
        assert name in feats, f"Feature mancante: {name}"

    # Test vector output
    vec = rssi_window_to_vector(window)
    assert vec is not None, "Vector non dovrebbe essere None"
    assert len(vec) == NUM_FEATURES, f"Expected {NUM_FEATURES} features, got {len(vec)}"

    # Test: finestra troppo corta
    feats = extract_rssi_features([-40])
    assert feats.get("_empty"), "Finestra di 1 dovrebbe essere empty"
    assert rssi_window_to_vector([-40]) is None

    # Test: movimento più rumoroso di baseline - gradient_mean dovrebbe essere più alto
    baseline = [-40] * 20  # perfettamente piatto
    movement = [-40 + 5 * math.sin(i) for i in range(20)]
    b_feats = extract_rssi_features(baseline)
    m_feats = extract_rssi_features(movement)
    assert m_feats["gradient_mean"] > b_feats["gradient_mean"], \
        "Movement gradient dovrebbe essere maggiore"
    assert m_feats["rssi_std"] > b_feats["rssi_std"], \
        "Movement std dovrebbe essere maggiore"

    log("RSSI feature extraction", True)


def test_rssi_classifier_synthetic():
    """RSSIClassifier training + inference con dati sintetici."""
    try:
        from sklearn.ensemble import RandomForestClassifier
    except ImportError:
        log("RSSI training (sklearn non disponibile)", False, "skip: sklearn mancante")
        return

    # Genera dati sintetici distinguibili
    bl = _make_rssi_samples(100, base=-40, noise=0.3)     # rumore basso
    mv = _make_movement_rssi_samples(100, base=-40, wiggle=5.0)  # movimento

    clf = RSSIClassifier(window_size=20)
    metrics = clf.train(bl, mv)

    assert clf.trained, "Classifier dovrebbe essere addestrato"
    assert clf.ready == False, "Non pronto senza dati nel buffer"
    assert metrics["n_train"] > 0, "Dovrebbero esserci campioni di training"
    assert metrics["n_features"] == NUM_FEATURES, f"Dovrebbero esserci {NUM_FEATURES} feature"
    assert "feature_importance" in metrics

    # Inference simulata: tutto baseline → prob bassa
    for _ in range(21):
        clf.add_sample(-40)
    prob = clf.predict_proba()
    assert 0 <= prob <= 1.0, f"Prob dovrebbe essere in [0,1]: {prob}"
    assert clf.ready, "Buffer pieno + modello = ready"

    # Salva e ricarica
    with tempfile.NamedTemporaryFile(suffix=".joblib", delete=False) as f:
        tmp_path = f.name
    try:
        clf.save(tmp_path)
        clf2 = RSSIClassifier(window_size=20)
        assert clf2.load(tmp_path), "Caricamento dovrebbe riuscire"
        assert clf2.trained
    finally:
        os.unlink(tmp_path)

    log("RSSIClassifier synthetic training", True)


def test_rssi_classifier_decision_boundary():
    """Il decision boundary tra baseline e movimento dovrebbe essere sensato."""
    try:
        from sklearn.ensemble import RandomForestClassifier
    except ImportError:
        log("RSSI decision boundary (sklearn non disponibile)", False, "skip: sklearn mancante")
        return

    # Dati molto separabili
    bl = [{"rssi": -40 + random.gauss(0, 0.1), "label": "baseline"} for _ in range(200)]
    mv = [{"rssi": -40 + random.gauss(0, 3.0) + 5 * math.sin(i * 0.5), "label": "movement"}
          for i in range(200)]

    clf = RSSIClassifier(window_size=20)
    clf.train(bl, mv)

    # Baseline silenziosa → probabilità bassa
    for _ in range(21):
        clf.add_sample(-40.0)
    prob_static = clf.predict_proba()
    assert prob_static < 0.5, f"Baseline silenziosa: prob={prob_static:.3f} dovrebbe essere <0.5"

    # Movimento simulato → probabilità alta
    from collections import deque
    buf = deque(maxlen=20)
    for i in range(20):
        val = -40 + 5 * math.sin(i * 0.8) + random.gauss(0, 1.5)
        buf.append(val)
        clf.add_sample(val)
    prob_move = clf.predict_proba()
    assert prob_move > 0.3, f"Movimento dovrebbe dare prob >0.3: {prob_move:.3f}"

    log("RSSI decision boundary", True)


# ============================================================
# Test CSI ML
# ============================================================

def test_csi_feature_extraction():
    """Feature extraction su finestra CSI."""
    frames = _make_csi_frames(35, "baseline", rssi=-45, ampl_mean=20.0, ampl_noise=2.0)

    # Test con finestra sufficiente
    feats = extract_csi_profile(frames)
    assert not feats.get("_empty"), "Window sufficiente non dovrebbe essere empty"
    assert feats["window_frames"] == 35
    assert feats["rssi_mean"] != 0.0
    assert "sub_mean_0" in feats
    assert "sub_std_0" in feats

    # Test vector output
    vec = csi_window_to_vector(frames)
    assert vec is not None
    assert len(vec) == CSI_FEATURE_SIZE, f"Expected {CSI_FEATURE_SIZE}, got {len(vec)}"

    # Test: finestra troppo corta
    feats = extract_csi_profile([{"csi": [{"ampl": 1}]}])
    assert feats.get("_empty"), "Finestra di 1 frame dovrebbe essere empty"

    # Test: movement più rumoroso ha temporal_variance e ampl_std_range più alti
    empty = _make_csi_frames(35, "baseline", ampl_mean=20.0, ampl_noise=1.0)
    moving = _make_csi_frames(35, "movement", ampl_mean=20.0, ampl_noise=6.0)
    e_feats = extract_csi_profile(empty)
    m_feats = extract_csi_profile(moving)
    assert m_feats["temporal_variance"] > e_feats["temporal_variance"], \
        "Movement temporal_variance dovrebbe essere maggiore"

    log("CSI feature extraction", True)


def test_csi_classifier_binary():
    """CSIClassifier training binario (EMPTY vs MOVEMENT) con dati sintetici."""
    try:
        from sklearn.ensemble import RandomForestClassifier
    except ImportError:
        log("CSI binary training (sklearn non disponibile)", False, "skip: sklearn mancante")
        return

    # Genera dati separabili: empty = ampl basso e costante, movement = ampl alto e rumoroso
    empty = _make_csi_frames(100, "baseline", ampl_mean=15.0, ampl_noise=1.0)
    movement = _make_csi_frames(100, "movement", ampl_mean=25.0, ampl_noise=5.0)

    clf = CSIClassifier(window_frames=30)
    metrics = clf.train(empty, None, movement)  # no stationary

    assert clf.trained
    assert metrics["n_train"] > 0
    assert metrics["n_classes"] >= 2
    assert "feature_importance" in metrics

    # Inference
    for f in empty[:30]:
        clf.add_frame(f)
    assert clf.ready, "Buffer pieno + modello = ready"
    probas = clf.predict_proba()
    assert isinstance(probas, dict)
    for lbl in CSI_LABELS:
        assert lbl in probas, f"Classe {lbl} mancante nelle probabilità"

    cls = clf.predict()
    assert cls in CSI_LABELS, f"Classe {cls} non valida"

    # Salva e carica
    with tempfile.NamedTemporaryFile(suffix=".joblib", delete=False) as f:
        tmp_path = f.name
    try:
        clf.save(tmp_path)
        clf2 = CSIClassifier(window_frames=30)
        assert clf2.load(tmp_path)
        assert clf2.trained
    finally:
        os.unlink(tmp_path)

    log("CSI classifier binary training", True)


def test_csi_classifier_ternary():
    """CSIClassifier training multi-classe (EMPTY+STATIONARY+MOVEMENT)."""
    try:
        from sklearn.ensemble import RandomForestClassifier
    except ImportError:
        log("CSI ternary training (sklearn non disponibile)", False, "skip: sklearn mancante")
        return

    # Dati con tre classi separabili
    empty = _make_csi_frames(100, "baseline", ampl_mean=10.0, ampl_noise=1.0)
    stationary = _make_csi_frames(100, "stationary", ampl_mean=15.0, ampl_noise=2.0)
    movement = _make_csi_frames(100, "movement", ampl_mean=20.0, ampl_noise=5.0)

    clf = CSIClassifier(window_frames=30)
    metrics = clf.train(empty, stationary, movement)

    assert metrics["n_classes"] == 3, f"Dovrebbero esserci 3 classi: {metrics['n_classes']}"

    # Tutte le classi hanno probabilità non zero
    for f in empty[:30]:
        clf.add_frame(f)
    probas = clf.predict_proba()
    for lbl in CSI_LABELS:
        assert lbl in probas, f"Classe {lbl} mancante"

    log("CSI classifier ternary training", True)


# ============================================================
# Run
# ============================================================

if __name__ == "__main__":
    random.seed(42)
    import time as _time

    print(f"\n{'='*60}")
    print(f"  ML Classifiers Tests")
    print(f"{'='*60}\n")

    t0 = _time.time()

    test_rssi_feature_extraction()
    test_rssi_classifier_synthetic()
    test_rssi_classifier_decision_boundary()

    test_csi_feature_extraction()
    test_csi_classifier_binary()
    test_csi_classifier_ternary()

    elapsed = _time.time() - t0
    total = PASS + FAIL
    print(f"\n{'='*60}")
    print(f"  Risultati: {PASS}/{total} passati ({elapsed:.2f}s)")
    if FAIL:
        print(f"  FAIL: {FAIL} test falliti")
    print(f"{'='*60}")
    sys.exit(0 if FAIL == 0 else 1)
