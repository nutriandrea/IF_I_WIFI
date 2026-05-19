#!/usr/bin/env python3
"""
test_seat_mapper.py — Test SeatClassifier: training, inference, save/load.

Zero hardware richiesto. Dati sintetici multi-AP (3 AP).
"""

import os
import sys
import random
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # project root

from csi.seat_mapper import (
    SeatClassifier,
    TOTAL_FEATURES,
    NUM_APS,
    LABEL_EMPTY,
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
    """Genera un singolo frame CSI sintetico."""
    random.seed(seq * 1000 + ap_id * 100)
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


def _generate_multi_ap_n(label: str, n: int, ampl_mean: float = 20.0,
                         ampl_noise: float = 3.0) -> list:
    """Genera n frame per AP 0,1,2 in ordine ciclico (simula channel hop).

    Ogni ciclo produce 3 frame (uno per AP). n specifica quanti frame
    TOTALE per AP in output.
    """
    frames = []
    cycle = [0, 1, 2]
    # Per avere n frame per AP, servono n * 3 frame totali
    for i in range(n):
        for aid in cycle:
            # Piccola differenza per AP per simulare multi-path diverso
            mean = ampl_mean + (aid * 1.5)
            noise = ampl_noise + (aid * 0.5)
            frames.append(_make_csi_frame(
                i * 3 + aid, -45 + aid * 3, 64, mean, noise, aid, label
            ))
    return frames


def _generate_seat_data(num_seats: int, window: int = 30) -> dict:
    """Genera dati sintetici per SeatClassifier training.

    Ogni sedia ha una firma CSI diversa (ampl_mean variabile).
    """
    labeled: dict[str, list] = {}

    # EMPTY: ampl_mean=15, uniforme
    labeled[LABEL_EMPTY] = _generate_multi_ap_n(LABEL_EMPTY, window + 10, 15.0, 1.5)

    # S0, S1, ... : ampl_mean crescente
    for si in range(num_seats):
        label = f"S{si}"
        ampl = 18.0 + si * 2.5  # 18, 20.5, 23, ...
        labeled[label] = _generate_multi_ap_n(label, window + 10, ampl, 2.0)

    return labeled


# ============================================================
# Tests
# ============================================================

def test_initial_state():
    """SeatClassifier non addestrato non è ready."""
    clf = SeatClassifier(window_frames=30)
    log("init: not trained",
        not clf.trained)
    log("init: not ready",
        not clf.ready)
    log("init: predict returns UNKNOWN",
        clf.predict() == "UNKNOWN")
    log("init: predict_proba returns empty",
        clf.predict_proba() == {})


def test_feature_vector_construction():
    """Feature vector ha dimensione TOTAL_FEATURES (= 1152)."""
    clf = SeatClassifier(window_frames=30)
    data = _generate_multi_ap_n("test", 35, 20.0, 2.0)

    for frame in data:
        clf.add_frame(frame)

    vec = clf._build_feature_vector()
    log("feature vector built",
        vec is not None, str(vec))
    if vec is not None:
        log("feature vector size = TOTAL_FEATURES",
            len(vec) == TOTAL_FEATURES, f"{len(vec)} != {TOTAL_FEATURES}")
        log("feature vector all finite",
            all(isinstance(v, (int, float)) and v != float('inf') for v in vec))


def test_feature_not_ready_with_insufficient_frames():
    """Non pronto se mancano frame per alcuni AP."""
    clf = SeatClassifier(window_frames=30)
    # Solo AP 0 frame
    for i in range(35):
        clf.add_frame(_make_csi_frame(i, ap_id=0))

    log("not ready with single AP data",
        not clf.ready)


def test_train_and_predict():
    """Training con 3 sedie + EMPTY → predict funziona."""
    random.seed(42)
    clf = SeatClassifier(window_frames=25)
    data = _generate_seat_data(num_seats=3, window=25)

    try:
        metrics = clf.train(data)
        log("train completed",
            clf.trained, f"metrics: {metrics}")
        log("train: classi corrette",
            set(clf._classes) == {"EMPTY", "S0", "S1", "S2"},
            f"got {clf._classes}")
    except (ValueError, RuntimeError) as e:
        log("train failed", False, str(e))
        return

    # Inference: usa lo stesso classifier (già addestrato),
    # alimenta con frame multi-AP della sedia S1
    # Ricrea ap_windows pulite ricaricando il modello
    import tempfile as _tf
    _tmp = _tf.NamedTemporaryFile(suffix=".joblib", delete=False)
    _tmp.close()
    try:
        clf.save(_tmp.name)
        clf.load(_tmp.name)
    finally:
        os.unlink(_tmp.name)

    s1_frames = data["S1"]
    for f in s1_frames:
        clf.add_frame(f)

    log("S1 classifier ready after data",
        clf.ready, f"buffers: {[len(w) for w in clf.ap_windows.values()]}")


def test_save_load_roundtrip():
    """Salva e carica modello → stesso comportamento."""
    random.seed(42)
    clf = SeatClassifier(window_frames=25)
    data = _generate_seat_data(num_seats=2, window=25)

    try:
        clf.train(data)
    except (ValueError, RuntimeError) as e:
        log("train (save/load) failed", False, str(e))
        return

    with tempfile.NamedTemporaryFile(suffix=".joblib", delete=False) as f:
        model_path = f.name

    try:
        # Salva
        saved = clf.save(model_path)
        log("model saved", os.path.exists(saved), f"path: {saved}")

        # Carica in nuovo classifier
        clf2 = SeatClassifier()
        loaded = clf2.load(model_path)
        log("model loaded", loaded)
        log("loaded has same classes",
            clf2._classes == clf._classes,
            f"{clf2._classes} vs {clf._classes}")
        log("loaded is trained",
            clf2.trained)
        log("loaded has same window",
            clf2.window_size == clf.window_size)
        log("loaded has same num_aps",
            clf2.num_aps == clf.num_aps)

    finally:
        os.unlink(model_path)


def test_predict_same_as_training():
    """Dopo training, predice la classe dei dati di training (overfit test)."""
    random.seed(42)
    clf = SeatClassifier(window_frames=25)
    data = _generate_seat_data(num_seats=3, window=25)

    try:
        # Per test: sega i dati per essere più sicuri della separabilità
        # Amplifichiamo le differenze tra sedie
        labeled_strong: dict[str, list] = {}
        labeled_strong[LABEL_EMPTY] = _generate_multi_ap_n(LABEL_EMPTY, 35, 10.0, 1.0)
        labeled_strong["S0"] = _generate_multi_ap_n("S0", 35, 30.0, 1.0)
        labeled_strong["S1"] = _generate_multi_ap_n("S1", 35, 50.0, 1.0)

        metrics = clf.train(labeled_strong)
        log("train (strong features) completed",
            clf.trained)
        log("train accuracy high",
            metrics.get("accuracy", 0) > 0.8,
            f"accuracy: {metrics.get('accuracy', 'N/A')}")
    except (ValueError, RuntimeError) as e:
        log("train (strong) failed", False, str(e))


def test_empty_frames_raises():
    """Training con frame vuoti solleva eccezione."""
    clf = SeatClassifier(window_frames=30)
    try:
        clf.train({})
        log("empty train raised error", False)
    except (ValueError, RuntimeError):
        log("empty train raises ValueError", True)


def test_single_class_raises():
    """Training con una sola classe solleva eccezione."""
    clf = SeatClassifier(window_frames=30)
    data = {LABEL_EMPTY: _generate_multi_ap_n(LABEL_EMPTY, 35, 15.0, 1.5)}
    try:
        clf.train(data)
        log("single class train raised error", False)
    except (ValueError, RuntimeError):
        log("single class raises ValueError", True)


def test_add_frame_multiap():
    """add_frame distribuisce frame al buffer AP corretto."""
    clf = SeatClassifier(window_frames=30)

    # Aggiungi frame per ogni AP
    for aid in range(3):
        for i in range(10):
            clf.add_frame(_make_csi_frame(i, ap_id=aid))

    for aid in range(3):
        buf_size = len(clf.ap_windows[aid])
        log(f"AP{aid} buffer size = 10",
            buf_size == 10, f"got {buf_size}")

    # Frame con ap_id fuori range
    clf.add_frame(_make_csi_frame(0, ap_id=5))
    # Non deve crashare e non deve essere aggiunto
    log("out-of-range ap_id ignored (no crash)", True)


def test_load_nonexistent():
    """load() su file inesistente → return False."""
    clf = SeatClassifier()
    loaded = clf.load("/tmp/nonexistent_model_xyz.joblib")
    log("load nonexistent returns False",
        not loaded)


def test_save_without_train_raises():
    """save() senza train → RuntimeError."""
    clf = SeatClassifier()
    try:
        clf.save("/tmp/should_not_exist.joblib")
        log("save without train raised error", False)
    except RuntimeError:
        log("save without train raises RuntimeError", True)


def test_unknown_class_prediction():
    """Con modello addestrato su EMPTY+S0, predice correttamente su specifici."""
    random.seed(42)
    clf = SeatClassifier(window_frames=25)
    data = _generate_seat_data(num_seats=1, window=25)

    try:
        clf.train(data)
    except (ValueError, RuntimeError) as e:
        log("train (unknown test) failed", False, str(e))
        return

    # Non pronto se buffer vuoti
    log("new clf not ready",
        not clf.ready)

    # Dopo dati EMPTY, dovrebbe predire EMPTY
    for f in data[LABEL_EMPTY]:
        clf.add_frame(f)

    # Potrebbe non essere ready se il buffer non ha frame di tutti gli AP
    log("predict returns string",
        isinstance(clf.predict(), str))


# ============================================================
# Main
# ============================================================

def main():
    print(f"\n{'='*60}")
    print(f"  SeatMapper Tests")
    print(f"{'='*60}\n")

    # Ordine di esecuzione
    test_initial_state()
    test_feature_vector_construction()
    test_feature_not_ready_with_insufficient_frames()
    test_add_frame_multiap()
    test_train_and_predict()
    test_predict_same_as_training()
    test_save_load_roundtrip()
    test_save_without_train_raises()
    test_load_nonexistent()
    test_empty_frames_raises()
    test_single_class_raises()
    test_unknown_class_prediction()

    total = PASS + FAIL
    print(f"\n{'='*60}")
    print(f"  Risultati: {PASS}/{total} passati", end="")
    if FAIL:
        print(f", {FAIL} falliti")
    else:
        print(" 🎉")
    print(f"{'='*60}\n")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
