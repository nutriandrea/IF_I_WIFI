#!/usr/bin/env python3
"""
test_position_regressor.py — Test PositionRegressor + KalmanFilter2D con dati sintetici.

Tests:
  - grid_label_to_xy / parse_grid_labels / grid_labels_to_xy_map
  - KalmanFilter2D: smoothing statico, tracking movimento, reset
  - PositionRegressor: training sintetico, inferenza, salvataggio/caricamento
  - Pipeline: training → inferenza → Kalman smoothing
"""

import sys
import os
import math
import random
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statistics import mean, stdev
from csi.quadrants.regressor import (
    PositionRegressor,
    KalmanFilter2D,
    grid_label_to_xy,
    parse_grid_labels,
    grid_labels_to_xy_map,
    DEFAULT_MODEL_PATH,
    DEFAULT_CONFIG_PATH,
)

# ============================================================
# Helpers: dati sintetici
# ============================================================

def _make_csi_frame(seq: int, ampl_mean: float = 15.0,
                    ampl_noise: float = 2.0, mac: str = "040102000000"):
    """Genera un frame CSI sintetico per test."""
    csi_data = []
    for sc in range(64):
        ampl = ampl_mean + random.gauss(0, ampl_noise)
        phase = random.gauss(0, 0.1)
        real_v = ampl * math.cos(phase)
        imag_v = ampl * math.sin(phase)
        csi_data.append({
            "subcarrier": sc,
            "real": real_v,
            "imag": imag_v,
            "ampl": round(ampl, 3),
            "phase": round(phase, 4),
        })
    amps = [c["ampl"] for c in csi_data]
    return {
        "seq": seq,
        "mac": mac,
        "rssi": -45,
        "noise_floor": -90,
        "num_subcarriers": 64,
        "csi": csi_data,
        "ampl_mean": float(mean(amps)),
        "ampl_std": float(stdev(amps)) if len(amps) >= 2 else 0,
        "ampl_max": float(max(amps)),
        "ampl_min": float(min(amps)),
    }


def _make_training_data(grid_rows: int = 2, grid_cols: int = 2,
                        frames_per_cell: int = 60):
    """Crea dataset sintetico per training PositionRegressor.

    Ogni cella ha un'amplificazione diversa per simulare
    posizioni differenti nella stanza.
    """
    data = {}
    for r in range(grid_rows):
        for c in range(grid_cols):
            label = f"r{r}c{c}"
            ampl = 12.0 + (r * grid_cols + c) * 3.0
            data[label] = [
                _make_csi_frame(i, ampl_mean=ampl, ampl_noise=2.0)
                for i in range(frames_per_cell * 3)
            ]
    return data


def _cleanup():
    for p in [DEFAULT_MODEL_PATH, DEFAULT_CONFIG_PATH]:
        if os.path.exists(p):
            os.remove(p)


# ============================================================
# Tests
# ============================================================

def test_grid_label_mapping():
    """Test conversione label griglia → coordinate continue."""
    xy = grid_label_to_xy("r0c0", 2, 2)
    assert abs(xy[0] - 0.25) < 1e-6, f"r0c0 x={xy[0]} expected 0.25"
    assert abs(xy[1] - 0.75) < 1e-6, f"r0c0 y={xy[1]} expected 0.75"

    xy = grid_label_to_xy("r1c1", 2, 2)
    assert abs(xy[0] - 0.75) < 1e-6
    assert abs(xy[1] - 0.25) < 1e-6

    xy = grid_label_to_xy("r1c1", 3, 3)
    assert abs(xy[0] - 0.5) < 1e-6
    assert abs(xy[1] - 0.5) < 1e-6

    assert grid_label_to_xy("vuoto", 2, 2) is None
    assert grid_label_to_xy("r0c0x", 2, 2) is None


def test_parse_grid_labels():
    """Test parsing lista label → dimensioni griglia."""
    labels = ["vuoto", "r0c0", "r0c1", "r1c0", "r1c1"]
    dims = parse_grid_labels(labels)
    assert dims == (2, 2), f"Expected (2,2) got {dims}"

    labels = ["vuoto", "r0c0", "r0c1", "r0c2", "r1c0", "r1c1", "r1c2"]
    dims = parse_grid_labels(labels)
    assert dims == (2, 3), f"Expected (2,3) got {dims}"

    assert parse_grid_labels(["vuoto", "sedia", "tavolo"]) is None


def test_grid_labels_to_xy_map():
    """Test mapping completo label → xy."""
    labels = ["vuoto", "r0c0", "r0c1", "r1c0", "r1c1"]
    xy_map = grid_labels_to_xy_map(labels)

    assert "vuoto" not in xy_map
    assert len(xy_map) == 4
    assert "r0c0" in xy_map
    assert abs(xy_map["r1c1"][0] - 0.75) < 1e-6


def test_kalman_static_smoothing():
    """Kalman filter: deve ridurre il rumore su target statico."""
    kf = KalmanFilter2D(q_pos=1e-5, q_vel=1e-4)
    kf.init(0.5, 0.5)

    raw_vals, smooth_vals = [], []
    for _ in range(100):
        z = 0.5 + random.gauss(0, 0.04)
        x, y, _, _ = kf.update((z, 0.5), (0.0016, 0.0016))
        raw_vals.append(z)
        smooth_vals.append(x)

    raw_std = stdev(raw_vals)
    smooth_std = stdev(smooth_vals)
    reduction = (1 - smooth_std / raw_std) * 100

    print(f"  [Kalman] Static: raw_std={raw_std:.4f} "
          f"smooth_std={smooth_std:.4f} reduction={reduction:.1f}%")
    assert reduction > 25, f"Rumore ridotto solo del {reduction:.1f}%"
    assert abs(mean(smooth_vals) - 0.5) < 0.02


def test_kalman_moving_tracking():
    """Kalman filter: deve seguire un target in movimento."""
    kf = KalmanFilter2D(q_pos=1e-4, q_vel=1e-3)
    kf.init(0.0, 0.0)

    # Movimento lineare con rumore
    estimates = []
    for step in range(50):
        true_x = step / 49.0
        noisy = true_x + random.gauss(0, 0.03)
        x, y, _, _ = kf.update((noisy, 0.5), (0.0009, 0.0009))
        estimates.append((x, y, true_x))

    final_lag = abs(estimates[-1][0] - estimates[-1][2])
    print(f"  [Kalman] Moving: final lag={final_lag:.4f}")
    assert final_lag < 0.15, f"Lag troppo alto: {final_lag}"


def test_kalman_reset():
    """Kalman filter: reset deve azzerare lo stato."""
    kf = KalmanFilter2D()
    kf.init(1.0, 2.0)
    kf.update((1.1, 2.1), (0.01, 0.01))
    assert kf._initialized

    kf.reset()
    assert not kf._initialized

    kf.init(0.0, 0.0)
    x, y, _, _ = kf.update((0.0, 0.0), (0.01, 0.01))
    assert kf._initialized


def test_regressor_training():
    """PositionRegressor: training e proprietà base."""
    data = _make_training_data(grid_rows=2, grid_cols=2, frames_per_cell=40)
    reg = PositionRegressor(window_frames=10)
    metrics = reg.train(data)

    assert reg.trained
    assert reg.grid_dims == (2, 2)
    assert metrics["n_train"] > 0
    assert "n_features" in metrics
    print(f"  [Regressor] Train: {metrics['n_train']} campioni, "
          f"{metrics['n_features']} feature, {metrics['n_classes']} classi")

    _cleanup()


def test_regressor_inference():
    """PositionRegressor: inferenza dopo training."""
    data = _make_training_data(grid_rows=2, grid_cols=2, frames_per_cell=40)
    reg = PositionRegressor(window_frames=10)
    reg.train(data)

    test_frames = [_make_csi_frame(i, ampl_mean=12.0, ampl_noise=2.0)
                   for i in range(20)]
    for f in test_frames:
        reg.add_frame(f)

    assert reg.ready
    pe = reg.predict()
    assert pe is not None
    assert 0.0 <= pe.x <= 1.0
    assert 0.0 <= pe.y <= 1.0
    assert pe.x_std >= 0.02
    assert pe.y_std >= 0.02

    print(f"  [Regressor] Inference: x={pe.x:.3f} y={pe.y:.3f} "
          f"std=({pe.x_std:.3f}, {pe.y_std:.3f})")

    _cleanup()


def test_regressor_save_load():
    """PositionRegressor: save/load con modello validato."""
    data = _make_training_data(grid_rows=2, grid_cols=2, frames_per_cell=40)
    reg = PositionRegressor(window_frames=10)
    reg.train(data)
    reg.save()

    assert os.path.exists(DEFAULT_MODEL_PATH)
    assert os.path.exists(DEFAULT_CONFIG_PATH)

    reg2 = PositionRegressor(window_frames=10)
    ok = reg2.load()
    assert ok, "load fallito"
    assert reg2.trained
    assert reg2.grid_dims == (2, 2)

    test_frames = [_make_csi_frame(i, ampl_mean=12.0, ampl_noise=2.0)
                   for i in range(20)]
    for f in test_frames:
        reg2.add_frame(f)
    assert reg2.ready

    pe2 = reg2.predict()
    assert pe2 is not None
    assert 0.0 <= pe2.x <= 1.0
    assert 0.0 <= pe2.y <= 1.0

    print(f"  [Regressor] Save/load: OK (x={pe2.x:.3f} y={pe2.y:.3f})")

    _cleanup()


def test_regressor_kalman_integration():
    """PositionRegressor: Kalman filter integrato."""
    data = _make_training_data(grid_rows=2, grid_cols=2, frames_per_cell=40)
    reg = PositionRegressor(window_frames=10)
    reg.train(data)

    test_frames = [_make_csi_frame(i, ampl_mean=12.0, ampl_noise=2.0)
                   for i in range(20)]
    for f in test_frames:
        reg.add_frame(f)

    pe1 = reg.predict()
    pe2 = reg.predict()
    assert pe1 is not None and pe2 is not None

    # Kalman attivo → output deve essere consistente
    assert abs(pe1.x - pe2.x) < 0.5

    print(f"  [Regressor] Kalman: stima1=({pe1.x:.3f},{pe1.y:.3f}) "
          f"stima2=({pe2.x:.3f},{pe2.y:.3f})")

    _cleanup()


# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    random.seed(42)
    print("=== Test PositionRegressor ===\n")

    test_grid_label_mapping()
    test_parse_grid_labels()
    test_grid_labels_to_xy_map()
    test_kalman_static_smoothing()
    test_kalman_moving_tracking()
    test_kalman_reset()
    test_regressor_training()
    test_regressor_inference()
    test_regressor_save_load()
    test_regressor_kalman_integration()

    _cleanup()
    print(f"\n✅ Tutti i test superati.")
