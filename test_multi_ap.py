#!/usr/bin/env python3
"""
test_multi_ap.py — Test multi-AP: parser, classificatore, e integrazione.
Zero hardware richiesto. Dati sintetici.
"""

import sys
import os
import math
import random
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from csi_processor import parse_csi_line
from csi_ml import (
    MultiAPCSIClassifier,
    CSI_LABELS,
    CSI_FEATURE_SIZE,
    MULTI_AP_FEATURE_SIZE,
    MULTI_AP_FEATURE_NAMES,
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
# Helpers — frame CSI sintetici con ap_id
# ============================================================

def _make_csi_frame(seq: int, rssi: int = -45, num_sub: int = 64,
                    ampl_mean: float = 20.0, ampl_std: float = 3.0,
                    ap_id: int = 0, label: str = "baseline") -> dict:
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
        "ap_id": ap_id,
        "_label": label,
    }


def _make_csi_frames(n: int, label: str = "baseline", ap_id: int = 0,
                     ampl_mean: float = 20.0, ampl_noise: float = 3.0) -> list:
    """Genera N frame CSI per un AP specifico."""
    return [_make_csi_frame(i, -45, 64, ampl_mean, ampl_noise, ap_id, label)
            for i in range(n)]


def _make_multi_ap_frames(ap_id: int, n: int, label: str = "baseline",
                          ampl_mean: float = 20.0, ampl_noise: float = 3.0) -> list:
    """Genera frame per un AP, intervallati con frame vuoti degli altri AP
    per simulare il channel hop reale."""
    frames = []
    cycle = [0, 1, 2]
    for i in range(n):
        for aid in cycle:
            mean = ampl_mean + (2.0 if aid == ap_id else 0.0)  # AP target leggermente diverso
            noise = ampl_noise + (1.0 if aid == ap_id else 0.0)
            frames.append(_make_csi_frame(
                i * 3 + aid, -45 + aid * 2, 64, mean, noise, aid, label
            ))
    return frames


# ============================================================
# Test 1: parse_csi_line con AP context
# ============================================================

def test_parser_ap_context():
    """parse_csi_line gestisce AP:<id> e AP_SWITCH:<id>."""
    # AP context line
    r = parse_csi_line("AP:0")
    assert r == {"ap_context": 0, "type": "ap_context"}, f"Unexpected: {r}"

    r = parse_csi_line("AP:2")
    assert r == {"ap_context": 2, "type": "ap_context"}, f"Unexpected: {r}"

    # AP switch
    r = parse_csi_line("AP_SWITCH:1")
    assert r == {"ap_switch": 1, "type": "ap_switch"}, f"Unexpected: {r}"

    log("Parser AP context lines", True)


def test_parser_csi_with_ap_id():
    """CSI frame dopo AP:<id> ha ap_id corretto."""
    parse_csi_line("AP:0")
    r = parse_csi_line("CSI:1:-45:-90:0:0:20:128:10,20,30,40")
    assert r is not None and r.get("ap_id") == 0, f"ap_id should be 0, got: {r}"

    parse_csi_line("AP:2")
    r = parse_csi_line("CSI:2:-48:-92:0:0:20:128:10,20,30,40")
    assert r is not None and r.get("ap_id") == 2, f"ap_id should be 2, got: {r}"

    # Heartbeat ignorato
    assert parse_csi_line("HB:1s wifi=3") is None

    # Valori di default
    from csi_processor import _AP_CONTEXT
    original = _AP_CONTEXT
    # Dopo AP:0, ap_id=0
    parse_csi_line("AP:0")
    r = parse_csi_line("CSI:3:-50:-90:0:0:20:128:10,20,30,40")
    assert r is not None and r.get("ap_id") == 0

    log("Parser CSI ap_id", True)


# ============================================================
# Test 2: MultiAPCSIClassifier — routing per ap_id
# ============================================================

def test_multi_ap_routing():
    """MultiAPCSIClassifier smista frame al window corretto per ap_id."""
    clf = MultiAPCSIClassifier(window_frames=10, num_aps=3)

    # Aggiungi frame per AP 0 e AP 1
    for i in range(5):
        clf.add_frame({"ap_id": 0, "csi": [{"ampl": 10}]})
        clf.add_frame({"ap_id": 1, "csi": [{"ampl": 20}]})

    assert len(clf.ap_windows[0]) == 5
    assert len(clf.ap_windows[1]) == 5
    assert len(clf.ap_windows[2]) == 0  # AP2 non usato

    # ap_id fuori range ignorato
    clf.add_frame({"ap_id": 5, "csi": [{"ampl": 1}]})
    assert len(clf.ap_windows[0]) == 5  # unchanged

    # Nessun ap_id → default 0
    clf.add_frame({"csi": [{"ampl": 1}]})
    assert len(clf.ap_windows[0]) == 6

    # Non pronto finché tutti i window non sono pieni
    assert not clf.ready

    log("Multi-AP frame routing", True)


# ============================================================
# Test 3: MultiAPCSIClassifier — training + inference sintetico
# ============================================================

def test_multi_ap_training():
    """MultiAPCSIClassifier training con dati sintetici da 3 AP."""
    try:
        from sklearn.ensemble import RandomForestClassifier
    except ImportError:
        log("Multi-AP training (sklearn non disponibile)", False,
            "skip: sklearn mancante")
        return

    # Genera dati separabili per 3 AP
    # empty: ampl basso e costante su tutti e 3 gli AP
    empty = _make_multi_ap_frames(0, 50, "baseline", ampl_mean=15.0, ampl_noise=1.0)
    # movement: ampl alto su AP 0
    movement = _make_multi_ap_frames(0, 50, "movement", ampl_mean=25.0, ampl_noise=5.0)

    clf = MultiAPCSIClassifier(window_frames=10, num_aps=3)
    metrics = clf.train(empty, None, movement)

    assert clf.trained
    assert metrics["n_train"] > 0
    assert metrics["n_features"] == MULTI_AP_FEATURE_SIZE, \
        f"Expected {MULTI_AP_FEATURE_SIZE}, got {metrics['n_features']}"
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
        clf2 = MultiAPCSIClassifier(window_frames=10, num_aps=3)
        assert clf2.load(tmp_path)
        assert clf2.trained
    finally:
        os.unlink(tmp_path)

    log("Multi-AP training + inference", True)


# ============================================================
# Test 4: MultiAPCSIClassifier — ternary (3 classi)
# ============================================================

def test_multi_ap_ternary():
    """MultiAPCSIClassifier training ternario EMPTY/STATIONARY/MOVEMENT."""
    try:
        from sklearn.ensemble import RandomForestClassifier
    except ImportError:
        log("Multi-AP ternary (sklearn non disponibile)", False,
            "skip: sklearn mancante")
        return

    # 3 classi con livelli di ampl separabili
    empty = _make_multi_ap_frames(0, 40, "empty", ampl_mean=10.0, ampl_noise=1.0)
    stationary = _make_multi_ap_frames(1, 40, "stationary", ampl_mean=15.0, ampl_noise=2.0)
    movement = _make_multi_ap_frames(2, 40, "movement", ampl_mean=25.0, ampl_noise=5.0)

    clf = MultiAPCSIClassifier(window_frames=10, num_aps=3)
    metrics = clf.train(empty, stationary, movement)

    assert clf.trained
    assert metrics["n_features"] == MULTI_AP_FEATURE_SIZE
    assert metrics["n_classes"] == 3

    # Inference
    for f in stationary[:30]:
        clf.add_frame(f)
    assert clf.ready
    probas = clf.predict_proba()
    assert isinstance(probas, dict)
    assert all(lbl in probas for lbl in CSI_LABELS)

    log("Multi-AP ternary classification", True)


# ============================================================
# Test 5: MultiAPCSIClassifier — edge cases
# ============================================================

def test_multi_ap_edge_cases():
    """MultiAPCSIClassifier gestisce casi limite."""
    clf = MultiAPCSIClassifier(window_frames=10, num_aps=3)

    # predict_proba prima del training
    assert clf.predict_proba() == -1.0
    assert clf.predict() == CSI_LABELS[0]
    assert not clf.ready

    # save prima del training
    try:
        clf.save("test.joblib")
        assert False, "Dovrebbe lanciare RuntimeError"
    except RuntimeError:
        pass

    # load da path inesistente
    assert not clf.load("/nonexistent/path.joblib")

    log("Multi-AP edge cases", True)


# ============================================================
# Test 6: Constants
# ============================================================

def test_multi_ap_constants():
    """Costanti multi-AP corrette."""
    assert MULTI_AP_FEATURE_SIZE == CSI_FEATURE_SIZE * 3, \
        f"{MULTI_AP_FEATURE_SIZE} != {CSI_FEATURE_SIZE} * 3"
    assert len(MULTI_AP_FEATURE_NAMES) == MULTI_AP_FEATURE_SIZE
    assert MULTI_AP_FEATURE_NAMES[0].startswith("ap0_")
    assert MULTI_AP_FEATURE_NAMES[CSI_FEATURE_SIZE].startswith("ap1_")
    assert MULTI_AP_FEATURE_NAMES[CSI_FEATURE_SIZE * 2].startswith("ap2_")

    log("Multi-AP constants", True)


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("=== Test Multi-AP ===\n")

    test_parser_ap_context()
    test_parser_csi_with_ap_id()
    test_multi_ap_routing()
    test_multi_ap_training()
    test_multi_ap_ternary()
    test_multi_ap_edge_cases()
    test_multi_ap_constants()

    print(f"\n=== Risultato: {PASS} passati, {FAIL} falliti ===")
    sys.exit(0 if FAIL == 0 else 1)
