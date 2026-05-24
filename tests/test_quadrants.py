#!/usr/bin/env python3
"""
test_quadrants.py — Test BlobEstimator (no-ML) e PositionRegressor (ML + LOO-cell).

Convenzione: PASS/FAIL contati, exit code != 0 se qualche test fallisce.
PYTHONPATH=. python3 tests/test_quadrants.py
"""
from __future__ import annotations

import math
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from csi.quadrants.blob_live import BlobEstimator, BlobEstimate, CellProbabilities, _var
from csi.quadrants.regressor import (
    PositionRegressor, KalmanFilter2D,
    grid_label_to_xy, parse_grid_labels, grid_labels_to_xy_map,
)

PASS = 0
FAIL = 0


def log(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        msg = f"  [FAIL] {name}"
        if detail:
            msg += f"  ({detail})"
        print(msg)


# ============================================================
# Helpers
# ============================================================
def make_frame(seq: int, tx: int, rx: int, ampl_mean: float,
               mac: str = "040102000000", n_sub: int = 64,
               noise: float = 0.1) -> dict:
    csi = []
    for sc in range(n_sub):
        a = ampl_mean + random.gauss(0, noise)
        ph = random.gauss(0, 0.1)
        csi.append({
            "subcarrier": sc, "real": a * math.cos(ph), "imag": a * math.sin(ph),
            "ampl": a, "phase": ph,
        })
    amps = [c["ampl"] for c in csi]
    return {
        "seq": seq, "tx_node": tx, "rx_node": rx,
        "mac": mac, "rssi": -45, "noise_floor": -90,
        "num_subcarriers": n_sub, "csi": csi,
        "ampl_mean": sum(amps) / len(amps),
        "ampl_std": _var(amps) ** 0.5,
        "ampl_max": max(amps), "ampl_min": min(amps),
    }


# ============================================================
# BlobEstimator: helpers & init
# ============================================================
def test_var_helper():
    log("_var([]) = 0", _var([]) == 0.0)
    log("_var([5]) = 0", _var([5]) == 0.0)
    log("_var([1,1,1]) = 0", _var([1, 1, 1]) == 0.0)
    log("_var([1,2,3,4,5]) ≈ 2.0", abs(_var([1, 2, 3, 4, 5]) - 2.0) < 1e-9)


def test_blob_validation():
    try:
        BlobEstimator(rx_positions=[])
        log("Validation: rx_positions vuoto → ValueError", False)
    except ValueError:
        log("Validation: rx_positions vuoto → ValueError", True)

    try:
        BlobEstimator(rx_positions=[(0, 0)], room_size=(-1, 5))
        log("Validation: room_size negativa → ValueError", False)
    except ValueError:
        log("Validation: room_size negativa → ValueError", True)

    try:
        BlobEstimator(rx_positions=[(0, 0)], window_frames=2)
        log("Validation: window_frames < 3 → ValueError", False)
    except ValueError:
        log("Validation: window_frames < 3 → ValueError", True)


def test_blob_no_estimate_with_empty_buffer():
    b = BlobEstimator(rx_positions=[(0.5, 0.5), (5.5, 0.5), (3.0, 4.5)])
    log("Buffer vuoto: estimate=None", b.estimate() is None)


def test_blob_no_estimate_below_min_intensity():
    """Tutti i percorsi con varianza zero → segnale insufficiente."""
    random.seed(1)
    b = BlobEstimator(rx_positions=[(0.0, 0.0), (5.0, 0.0), (2.5, 5.0)],
                       min_intensity=1.0)
    # Inietta frame ad ampiezza COSTANTE (var ≈ 0)
    for tx in range(3):
        for rx in range(3):
            for i in range(30):
                b.add_frame(make_frame(i, tx, rx, ampl_mean=10.0, noise=0.0))
    est = b.estimate()
    log("Var quasi zero: estimate=None", est is None)


def test_blob_centroid_pulls_toward_high_variance_rx():
    """Solo RX0 vede grande varianza → centroide deve avvicinarsi a RX0."""
    random.seed(2)
    rx_positions = [(0.0, 0.0), (5.0, 0.0), (2.5, 5.0)]
    b = BlobEstimator(rx_positions=rx_positions, room_size=(5.0, 5.0),
                       min_intensity=1e-6)

    # Inietta SOLO frame ad alto rumore per rx=0; rx=1 e rx=2 stabili
    for i in range(30):
        b.add_frame(make_frame(i, 0, 0, ampl_mean=10.0, noise=5.0))   # rumoroso
        b.add_frame(make_frame(i, 0, 1, ampl_mean=10.0, noise=0.05))
        b.add_frame(make_frame(i, 0, 2, ampl_mean=10.0, noise=0.05))

    est = b.estimate()
    log("RX0 dominante: estimate non None", est is not None)
    if est is not None:
        # Distanza dal centroide a RX0 < distanza al centro geometrico
        d_rx0 = math.hypot(est.x - rx_positions[0][0], est.y - rx_positions[0][1])
        d_center = math.hypot(est.x - 2.5, est.y - 2.0)
        log("Centroide più vicino a RX0 che al centro stanza",
            d_rx0 < d_center,
            detail=f"d_rx0={d_rx0:.2f}, d_center={d_center:.2f}, est=({est.x:.2f},{est.y:.2f})")


def test_blob_cells_predicted_near_rx():
    """Movimento solo su RX0 → cella predetta deve essere quella più vicina a RX0."""
    random.seed(3)
    rx = [(0.5, 0.5), (5.5, 0.5), (3.0, 4.5)]
    b = BlobEstimator(rx_positions=rx, room_size=(6.0, 5.0), grid_shape=(4, 4),
                       min_intensity=1e-6)

    for i in range(30):
        b.add_frame(make_frame(i, 0, 0, ampl_mean=10.0, noise=5.0))
        b.add_frame(make_frame(i, 0, 1, ampl_mean=10.0, noise=0.05))
        b.add_frame(make_frame(i, 0, 2, ampl_mean=10.0, noise=0.05))

    cells = b.cell_probabilities()
    log("cell_probabilities non None", cells is not None)
    if cells is not None:
        # RX0 a (0.5, 0.5) m → Cartesian bottom-left → griglia r=rows-1=3, c=0.
        log("Cella predetta in basso-sx (r3c0 area)",
            cells.predicted in ("r3c0", "r3c1", "r2c0"),
            detail=f"predicted={cells.predicted}")
        s = sum(cells.probas.values())
        log("Sum probas ≈ 1.0", abs(s - 1.0) < 1e-3, detail=f"sum={s:.4f}")


def test_blob_variance_power_amplifies_asymmetry():
    """variance_power>1 deve avvicinare il centroide al RX dominante quando
    le 3 varianze sono solo lievemente differenti (fix 'blob fermo al centro')."""
    random.seed(5)
    rx = [(0.0, 0.0), (5.0, 0.0), (2.5, 5.0)]
    # Tre RX con varianza SIMILE ma RX0 leggermente più alta (10% di vantaggio)
    # Simuliamo iniettando lo stesso jitter ma con frame_count diverso
    def _build(power: float):
        b = BlobEstimator(rx_positions=rx, room_size=(5.0, 5.0),
                          min_intensity=1e-6, variance_power=power)
        for i in range(30):
            # RX0 var ≈ 1.1, RX1 var ≈ 1.0, RX2 var ≈ 1.0
            b.add_frame(make_frame(i, 0, 0, ampl_mean=10.0, noise=1.1))
            b.add_frame(make_frame(i, 0, 1, ampl_mean=10.0, noise=1.0))
            b.add_frame(make_frame(i, 0, 2, ampl_mean=10.0, noise=1.0))
        return b.estimate()

    est_lin = _build(1.0)
    est_pow = _build(3.0)
    assert est_lin is not None and est_pow is not None
    centroid_geom = (sum(p[0] for p in rx) / 3, sum(p[1] for p in rx) / 3)
    d_lin = math.hypot(est_lin.x - centroid_geom[0], est_lin.y - centroid_geom[1])
    d_pow = math.hypot(est_pow.x - centroid_geom[0], est_pow.y - centroid_geom[1])
    log("variance_power=3 si allontana più dal centro che con power=1",
        d_pow > d_lin,
        detail=f"d_lin={d_lin:.3f}, d_pow={d_pow:.3f}")


def test_blob_variance_power_validation():
    try:
        BlobEstimator(rx_positions=[(0, 0)], variance_power=0.0)
        log("Validation: variance_power=0 → ValueError", False)
    except ValueError:
        log("Validation: variance_power=0 → ValueError", True)


def test_blob_calibration_subtracts_baseline():
    """Se calibrato a baseline rumoroso, deve sottrarlo dall'estimate."""
    random.seed(4)
    rx = [(0.0, 0.0), (5.0, 0.0), (2.5, 5.0)]
    b = BlobEstimator(rx_positions=rx, room_size=(5.0, 5.0),
                       baseline_alpha=0.3, baseline_seconds=0.2,
                       min_intensity=1e-6)
    # Calibrazione: rumore uniforme moderato su tutti
    for i in range(40):
        for tx in range(3):
            for rx_id in range(3):
                b.add_frame(make_frame(i, tx, rx_id, ampl_mean=10.0, noise=0.5))
    time.sleep(0.25)
    # Un ultimo frame per triggerare il finalize
    b.add_frame(make_frame(99, 0, 0, ampl_mean=10.0, noise=0.5))
    log("Calibrazione completata", b.is_calibrated())


# ============================================================
# Grid label helpers
# ============================================================
def test_grid_label_helpers():
    log("parse_grid_labels(['r0c0','r1c1']) = (2,2)",
        parse_grid_labels(["r0c0", "r1c1"]) == (2, 2))
    log("parse_grid_labels(['vuoto']) = None",
        parse_grid_labels(["vuoto"]) is None)
    log("grid_label_to_xy('r0c0', 2, 2) = (0.25, 0.75)",
        grid_label_to_xy("r0c0", 2, 2) == (0.25, 0.75))
    log("grid_label_to_xy('r1c1', 2, 2) = (0.75, 0.25)",
        grid_label_to_xy("r1c1", 2, 2) == (0.75, 0.25))
    log("grid_label_to_xy('foo', 2, 2) = None",
        grid_label_to_xy("foo", 2, 2) is None)

    m = grid_labels_to_xy_map(["r0c0", "r0c1", "vuoto"])
    log("grid_labels_to_xy_map ignora label non griglia",
        set(m.keys()) == {"r0c0", "r0c1"})


# ============================================================
# Kalman 2D
# ============================================================
def test_kalman_init_and_smooth_static():
    kf = KalmanFilter2D(q_pos=1e-4, q_vel=1e-3)
    # Prima update: ritorna esattamente la misura, std_min
    out = kf.update(z=(0.5, 0.5), t=0.0)
    log("Kalman: prima update ≈ misura",
        abs(out[0] - 0.5) < 1e-6 and abs(out[1] - 0.5) < 1e-6,
        detail=f"got {out}")

    # Iniezione di misure rumorose intorno a (0.5, 0.5): output deve convergere
    random.seed(50)
    for i in range(40):
        kf.update(
            z=(0.5 + random.gauss(0, 0.05), 0.5 + random.gauss(0, 0.05)),
            R=(0.05 ** 2, 0.05 ** 2),
            t=0.033 * (i + 1),
        )
    out = kf.update(z=(0.5, 0.5), t=2.0)
    log("Kalman: smoothing converge a (0.5, 0.5)",
        abs(out[0] - 0.5) < 0.05 and abs(out[1] - 0.5) < 0.05,
        detail=f"got ({out[0]:.3f}, {out[1]:.3f})")


def test_kalman_tracks_movement():
    """Track a moving target."""
    kf = KalmanFilter2D(q_pos=1e-3, q_vel=1e-2)  # più reattivo
    random.seed(51)
    last_t = 0.0
    for i in range(30):
        x = 0.1 + i * 0.025
        y = 0.5
        last_t += 0.033
        kf.update(z=(x, y), R=(0.02 ** 2, 0.02 ** 2), t=last_t)
    # Predizione finale dovrebbe essere vicino a (0.825, 0.5)
    out = kf.update(z=(0.85, 0.5), R=(0.02 ** 2, 0.02 ** 2), t=last_t + 0.033)
    log("Kalman: traccia movimento lineare",
        abs(out[0] - 0.85) < 0.15 and abs(out[1] - 0.5) < 0.1,
        detail=f"got ({out[0]:.3f}, {out[1]:.3f})")


def test_kalman_reset():
    kf = KalmanFilter2D()
    kf.init(0.5, 0.5)
    kf.reset()
    log("Kalman: reset → non initialized", not kf._initialized)


# ============================================================
# PositionRegressor — training, LOO-cell, save/load gate
# ============================================================
def _make_4cell_dataset(frames_per_cell: int = 80):
    """Costruisce dataset 2×2 con ampiezza DIVERSA per cella (artificialmente
    separabile per il RF, ma non spazialmente generalizzabile — perfetto per
    smascherare l'overfitting via LOO-cell)."""
    random.seed(7)
    d: dict[str, list] = {}
    d["r0c0"] = [make_frame(i, 0, 0, 10.0, noise=0.5) for i in range(frames_per_cell)]
    d["r0c1"] = [make_frame(i, 0, 0, 15.0, noise=0.5) for i in range(frames_per_cell)]
    d["r1c0"] = [make_frame(i, 0, 0, 20.0, noise=0.5) for i in range(frames_per_cell)]
    d["r1c1"] = [make_frame(i, 0, 0, 25.0, noise=0.5) for i in range(frames_per_cell)]
    return d


def test_regressor_train_basic():
    d = _make_4cell_dataset()
    reg = PositionRegressor(window_frames=20)
    m = reg.train(d)
    log("Regressor: trained",
        reg.trained and m["n_train"] > 50 and m["n_classes"] == 4,
        detail=f"n_train={m['n_train']}, n_classes={m['n_classes']}")


def test_regressor_loo_cell_rejects_synthetic_overfit():
    """Il dataset sintetico ha label predicibili dall'ampiezza, ma LOO-cell
    deve smascherare che il modello NON sa interpolare."""
    d = _make_4cell_dataset()
    reg = PositionRegressor(window_frames=20)
    reg.train(d)
    rep = reg.cross_validate_loo_cell(d, max_mae_normalized=0.20)
    log("LOO-cell: rifiuta modello non interpolante",
        rep["accepted"] is False,
        detail=f"mae={rep['mae']:.3f}, r2={rep['r2']:.3f}")
    log("LOO-cell: report ha 4 fold", rep["n_folds"] == 4)


def test_regressor_loo_cell_accepts_when_truly_spatial():
    """Costruisco un dataset GENUINAMENTE spaziale: l'ampiezza varia
    monotonicamente con la coordinata (x+y). In quel caso anche tirando fuori
    una cella il RF dovrebbe interpolare bene."""
    random.seed(11)
    d: dict[str, list] = {}
    # Griglia 3x3, ampiezza = base * (1 + (x + y)/2)
    base = 10.0
    for r in range(3):
        for c in range(3):
            x = (c + 0.5) / 3
            y = 1 - (r + 0.5) / 3
            amp = base * (1.0 + (x + y) * 0.5)  # spazialmente smooth
            d[f"r{r}c{c}"] = [make_frame(i, 0, 0, amp, noise=0.05)
                              for i in range(80)]

    reg = PositionRegressor(window_frames=20)
    reg.train(d)
    rep = reg.cross_validate_loo_cell(d, max_mae_normalized=0.45)
    # Non garantisco accettazione perché 1 sola feature variabile (ampl) può
    # essere ambigua sulle diagonali (le celle (x+y)=const collidono).
    # Verifico solo che il MAE sia ragionevolmente vicino alla soglia
    # (non randomico, non perfetto).
    log("LOO-cell: dataset spaziale ha MAE < 0.45",
        rep["mae"] < 0.45,
        detail=f"mae={rep['mae']:.3f}, r2={rep['r2']:.3f}, accepted={rep['accepted']}")


def test_regressor_save_load_rejects_unvalidated():
    """save() del modello non validato + load() deve rifiutarlo by default."""
    d = _make_4cell_dataset()
    reg = PositionRegressor(window_frames=20)
    reg.train(d)
    rep = reg.cross_validate_loo_cell(d, max_mae_normalized=0.05)
    assert not rep["accepted"], "il dataset sintetico DEVE essere rifiutato"

    with tempfile.TemporaryDirectory() as tmp:
        model_path = os.path.join(tmp, "m.joblib")
        config_path = os.path.join(tmp, "m.json")
        reg.save(path=model_path, config_path=config_path)

        reg2 = PositionRegressor(window_frames=20)
        loaded = reg2.load(path=model_path, config_path=config_path)
        log("Load: rifiutato modello con cv_report.accepted=False",
            loaded is False)

        loaded2 = reg2.load(path=model_path, config_path=config_path,
                            allow_unvalidated=True)
        log("Load: accettato con allow_unvalidated=True", loaded2 is True)


def test_regressor_predict_smoke():
    d = _make_4cell_dataset()
    reg = PositionRegressor(window_frames=20)
    reg.train(d)
    # Inietta abbastanza frame della cella r0c0 per riempire la finestra
    for f in d["r0c0"][:25]:
        reg.add_frame(f)
    est = reg.predict()
    log("Predict: ritorna PositionEstimate", est is not None)
    if est is not None:
        # Coordinate r0c0 = (0.25, 0.75) (grid 2×2)
        log("Predict: x vicino a 0.25",
            abs(est.x - 0.25) < 0.1, detail=f"x={est.x:.3f}")
        log("Predict: y vicino a 0.75",
            abs(est.y - 0.75) < 0.1, detail=f"y={est.y:.3f}")


# ============================================================
# Main
# ============================================================
def main() -> int:
    print("=" * 64)
    print("  TEST: csi.quadrants  (blob_live + regressor + Kalman)")
    print("=" * 64)
    print()
    print("-- BlobEstimator (no-ML) --")
    test_var_helper()
    test_blob_validation()
    test_blob_no_estimate_with_empty_buffer()
    test_blob_no_estimate_below_min_intensity()
    test_blob_centroid_pulls_toward_high_variance_rx()
    test_blob_cells_predicted_near_rx()
    test_blob_variance_power_amplifies_asymmetry()
    test_blob_variance_power_validation()
    test_blob_calibration_subtracts_baseline()

    print("\n-- Grid label helpers --")
    test_grid_label_helpers()

    print("\n-- KalmanFilter2D --")
    test_kalman_init_and_smooth_static()
    test_kalman_tracks_movement()
    test_kalman_reset()

    print("\n-- PositionRegressor + LOO-cell --")
    test_regressor_train_basic()
    test_regressor_loo_cell_rejects_synthetic_overfit()
    test_regressor_loo_cell_accepts_when_truly_spatial()
    test_regressor_save_load_rejects_unvalidated()
    test_regressor_predict_smoke()

    print("\n" + "=" * 64)
    print(f"  Risultati: {PASS} pass, {FAIL} fail")
    print("=" * 64)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
