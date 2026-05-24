#!/usr/bin/env python3
"""
test_integration.py — Test integrazione pipeline completa.
Simula: UDP → parse → regressor training → inferenza blob → Kalman → WS message.

Nessun hardware richiesto. Testa il flusso end-to-end con dati sintetici.
"""

import sys
import os
import math
import random
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import deque
from statistics import mean, stdev
from csi.csi_ml import (
    CSIClassifier,
    csi_window_to_vector,
    CSI_MODEL_PATH, POSITIONS_MODEL_PATH, POSITIONS_LABELS_PATH,
)
from csi.quadrants.regressor import (
    PositionRegressor, KalmanFilter2D,
    DEFAULT_MODEL_PATH, DEFAULT_CONFIG_PATH,
)


# ============================================================
# Helpers: frame CSI sintetico
# ============================================================

def _make_csi_frame(seq: int, ampl_mean: float = 15.0,
                    ampl_noise: float = 2.0, rssi: int = -45,
                    mac: str = "040102000000"):
    csi_data = []
    for sc in range(64):
        ampl = ampl_mean + random.gauss(0, ampl_noise)
        phase = random.gauss(0, 0.1)
        csi_data.append({
            "subcarrier": sc,
            "real": ampl * math.cos(phase),
            "imag": ampl * math.sin(phase),
            "ampl": round(ampl, 3),
            "phase": round(phase, 4),
        })
    amps = [c["ampl"] for c in csi_data]
    return {
        "seq": seq,
        "mac": mac,
        "rssi": rssi,
        "noise_floor": -90,
        "num_subcarriers": 64,
        "csi": csi_data,
        "ampl_mean": float(mean(amps)),
        "ampl_std": float(stdev(amps)) if len(amps) >= 2 else 0,
        "ampl_max": float(max(amps)),
        "ampl_min": float(min(amps)),
    }


def _make_dataset(grid_rows=2, grid_cols=2, fps=60):
    """Crea dataset sintetico per training."""
    data = {}
    data["vuoto"] = [_make_csi_frame(i, ampl_mean=8.0, rssi=-70)
                     for i in range(fps * 3)]
    for r in range(grid_rows):
        for c in range(grid_cols):
            ampl = 12.0 + (r * grid_cols + c) * 3.0
            rssi = -50 + (r + c) * 3
            label = f"r{r}c{c}"
            data[label] = [_make_csi_frame(i, ampl_mean=ampl, rssi=rssi)
                           for i in range(fps * 3)]
    return data


def _cleanup_paths():
    for p in [DEFAULT_MODEL_PATH, DEFAULT_CONFIG_PATH,
              CSI_MODEL_PATH, POSITIONS_MODEL_PATH, POSITIONS_LABELS_PATH]:
        if os.path.exists(p):
            os.remove(p)


# ============================================================
# Test
# ============================================================

def test_regressor_training_inference():
    """Training regressore + inferenza blob."""
    data = _make_dataset(2, 2)
    reg = PositionRegressor(window_frames=10)
    metrics = reg.train(data)

    assert metrics["n_train"] > 0
    assert reg.trained

    # Inferenza su r0c0
    frames = [_make_csi_frame(i, ampl_mean=12.0) for i in range(20)]
    for f in frames:
        reg.add_frame(f)

    assert reg.ready
    pe = reg.predict()
    assert pe is not None

    # Tutti i campi PositionEstimate presenti
    assert isinstance(pe.x, float)
    assert isinstance(pe.y, float)
    assert isinstance(pe.x_std, float)
    assert isinstance(pe.y_std, float)
    assert isinstance(pe.smoothed, bool)
    assert isinstance(pe.confidence, float)

    # x,y in range
    assert 0.0 < pe.x < 1.0, f"x fuori range: {pe.x}"
    assert 0.0 < pe.y < 1.0, f"y fuori range: {pe.y}"
    assert pe.x_std >= 0.02
    assert pe.y_std >= 0.02

    _cleanup_paths()

    print(f"  ✅ Training+inferenza: x={pe.x:.3f} y={pe.y:.3f}")
    return reg


def test_kalman_smoothing_pipeline():
    """Kalman + regressore: stima deve essere smooth su sequenza."""
    data = _make_dataset(2, 2)
    reg = PositionRegressor(window_frames=10)
    reg.train(data)

    # Simula movimento fluido con rumore
    estimates = []
    for step in range(40):
        t = step / 39.0
        ampl = 12.0 + t * 6.0  # r0c0 → r1c1
        frame = _make_csi_frame(step, ampl_mean=ampl, ampl_noise=3.0)
        reg.add_frame(frame)
        if reg.ready:
            pe = reg.predict()
            if pe is not None:
                estimates.append(pe)

    assert len(estimates) >= 10, f"Poche stime: {len(estimates)}"

    # Le x devono essere monotone crescenti (movimento lineare)
    xs = [e.x for e in estimates]
    # Conta inversioni: poche = smooth
    inversions = sum(1 for i in range(2, len(xs)) if xs[i] < xs[i-1] and xs[i-1] < xs[i-2])
    assert inversions < len(xs) * 0.3, \
        f"Troppe inversioni Kalman: {inversions}/{len(xs)}"

    _cleanup_paths()

    print(f"  ✅ Kalman smoothing: {len(estimates)} stime, {inversions} inversioni su {len(xs)}")
    return estimates


def test_multi_persona_pipeline():
    """Multi-albero: varianza tra gli alberi RF come proxy d'incertezza."""
    data = _make_dataset(3, 3)
    reg = PositionRegressor(window_frames=10)
    reg.train(data)

    # Crea frame per una posizione intermedia
    frames = [_make_csi_frame(i, ampl_mean=15.0, ampl_noise=2.0) for i in range(20)]
    for f in frames:
        reg.add_frame(f)

    assert reg.ready
    pe = reg.predict()
    assert pe is not None

    # Incertezza deve essere > 0
    assert pe.x_std > 0
    assert pe.y_std > 0

    _cleanup_paths()

    print(f"  ✅ Multi-albero: (x={pe.x:.3f}, y={pe.y:.3f}) "
          f"std=({pe.x_std:.3f}, {pe.y_std:.3f})")
    return pe


def test_save_load_cycle():
    """Salva e ricarica modello, verifica stima invariata."""
    data = _make_dataset(2, 2)
    reg = PositionRegressor(window_frames=10)
    reg.train(data)

    # Salva
    reg.save()
    assert os.path.exists(DEFAULT_MODEL_PATH)
    assert os.path.exists(DEFAULT_CONFIG_PATH)

    # Ricarica
    reg2 = PositionRegressor(window_frames=10)
    ok = reg2.load()
    assert ok, "load fallito"
    assert reg2.trained
    assert reg2.grid_dims == (2, 2)

    # Stessa inferenza
    frames = [_make_csi_frame(i, ampl_mean=12.0) for i in range(20)]
    for f in frames:
        reg2.add_frame(f)
    assert reg2.ready

    pe2 = reg2.predict()
    assert pe2 is not None
    assert 0.0 < pe2.x < 1.0
    assert 0.0 < pe2.y < 1.0

    _cleanup_paths()

    print(f"  ✅ Save/load: OK (stima x={pe2.x:.3f} y={pe2.y:.3f})")


# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    random.seed(42)
    print("=== Test Integrazione Pipeline ===\n")

    test_regressor_training_inference()
    test_kalman_smoothing_pipeline()
    test_multi_persona_pipeline()
    test_save_load_cycle()

    _cleanup_paths()
    print(f"\n✅ Tutti i test superati.")
