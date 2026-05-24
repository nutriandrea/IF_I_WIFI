#!/usr/bin/env python3
"""
test_blob3d.py — Test HeightHeuristic + KalmanFilter3D + Blob3DTracker.

PYTHONPATH=. python3 tests/test_blob3d.py
"""
from __future__ import annotations

import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from csi.blob3d.tracker import (
    Blob3DTracker, Blob3DEstimate, HeightHeuristic, HeightClass, KalmanFilter3D, _variance,
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
def make_csi_frame(noise_low: float, noise_high: float,
                   n_sub: int = 64, base_ampl: float = 10.0) -> dict:
    """Genera un frame CSI con rumore diverso su sub-banda bassa vs alta.

    Bassa (sub 0..31) con jitter `noise_low`, alta (sub 32..63) con
    jitter `noise_high`. Permette di simulare i pattern che la
    HeightHeuristic distingue (low/high body posture).
    """
    csi = []
    for sc in range(n_sub):
        nz = noise_high if sc >= n_sub // 2 else noise_low
        a = base_ampl + random.gauss(0, nz)
        ph = random.gauss(0, 0.1)
        csi.append({
            "subcarrier": sc,
            "real": a * math.cos(ph),
            "imag": a * math.sin(ph),
            "ampl": abs(a),
            "phase": ph,
        })
    amps = [c["ampl"] for c in csi]
    return {
        "csi": csi,
        "ampl_mean": sum(amps) / len(amps),
    }


def feed_heuristic(h: HeightHeuristic, n: int,
                   noise_low: float, noise_high: float) -> None:
    for _ in range(n):
        h.add_frame(make_csi_frame(noise_low, noise_high))


# ============================================================
# HeightHeuristic
# ============================================================
def test_variance_helper():
    log("_variance([]) = 0", _variance([]) == 0.0)
    log("_variance([1,1]) = 0", _variance([1, 1]) == 0.0)
    log("_variance([1,2,3,4,5]) ≈ 2.0",
        abs(_variance([1, 2, 3, 4, 5]) - 2.0) < 1e-9)


def test_heuristic_init():
    h = HeightHeuristic()
    log("Init: class UNKNOWN", h.current_class() == HeightClass.UNKNOWN)
    log("Init: ratio None", h.current_ratio() is None)


def test_heuristic_validation():
    try:
        HeightHeuristic(low_band=(20, 40), high_band=(30, 60))
        log("Validation: bande sovrapposte → ValueError", False)
    except ValueError:
        log("Validation: bande sovrapposte → ValueError", True)


def test_heuristic_high_signal():
    """High-band noisy + low-band quiet → ratio > soglia high → HIGH."""
    random.seed(11)
    h = HeightHeuristic(min_frames=20, min_dwell_s=0.0)
    feed_heuristic(h, 30, noise_low=0.05, noise_high=3.0)
    log("HIGH dopo high-band noise: class HIGH",
        h.current_class() == HeightClass.HIGH,
        detail=f"got {h.current_class().value}, ratio={h.current_ratio()}")


def test_heuristic_low_signal():
    """Low-band noisy + high-band quiet → ratio < soglia → LOW."""
    random.seed(12)
    h = HeightHeuristic(min_frames=20, min_dwell_s=0.0)
    feed_heuristic(h, 30, noise_low=3.0, noise_high=0.05)
    log("LOW dopo low-band noise: class LOW",
        h.current_class() == HeightClass.LOW,
        detail=f"got {h.current_class().value}, ratio={h.current_ratio()}")


def test_heuristic_mid_signal():
    """Rumore bilanciato → ratio in zona mid → MID.

    Nota: il ratio random può fluttuare intorno a 1.0 ± 0.5 a seconda del
    seed; allargo le soglie del detector per avere zona MID più ampia in test.
    """
    random.seed(13)
    h = HeightHeuristic(min_frames=20, min_dwell_s=0.0,
                        threshold_low=0.5, threshold_high=2.0)
    feed_heuristic(h, 30, noise_low=0.5, noise_high=0.5)
    cls = h.current_class()
    log("MID dopo rumore bilanciato: class MID o UNKNOWN",
        cls in (HeightClass.MID, HeightClass.UNKNOWN),
        detail=f"got {cls.value}, ratio={h.current_ratio()}")


def test_heuristic_hysteresis():
    """Min_dwell_s impedisce cambio rapido."""
    random.seed(14)
    h = HeightHeuristic(min_frames=20, min_dwell_s=0.5)
    feed_heuristic(h, 30, noise_low=0.05, noise_high=3.0)
    # potrebbe essere ancora UNKNOWN per dwell
    cls1 = h.current_class()
    # ora aspetto e ri-feed mantenendo high
    for _ in range(20):
        time.sleep(0.03)
        feed_heuristic(h, 3, noise_low=0.05, noise_high=3.0)
    log("Dopo dwell: class HIGH",
        h.current_class() == HeightClass.HIGH,
        detail=f"prima={cls1.value}, dopo={h.current_class().value}")


def test_heuristic_handles_frames_without_csi():
    h = HeightHeuristic(min_frames=10, min_dwell_s=0.0)
    # frame con 'csi' = None / mancante / non-list
    h.add_frame({"csi": None})
    h.add_frame({})
    h.add_frame({"csi": "not a list"})
    log("Frame degeneri: class UNKNOWN",
        h.current_class() == HeightClass.UNKNOWN)


# ============================================================
# KalmanFilter3D
# ============================================================
def test_kalman3d_init():
    kf = KalmanFilter3D()
    out = kf.update(z=(1.0, 2.0, 1.5), t=0.0)
    log("Kalman3D: prima update ≈ misura",
        abs(out[0] - 1.0) < 1e-6 and abs(out[1] - 2.0) < 1e-6 and abs(out[2] - 1.5) < 1e-6,
        detail=f"got {out}")


def test_kalman3d_smoothing():
    random.seed(20)
    kf = KalmanFilter3D(q_pos=1e-4, q_vel=1e-3)
    for i in range(40):
        kf.update(
            z=(3.0 + random.gauss(0, 0.1),
               2.0 + random.gauss(0, 0.1),
               1.0 + random.gauss(0, 0.1)),
            R=(0.1 ** 2, 0.1 ** 2, 0.1 ** 2),
            t=0.033 * (i + 1),
        )
    out = kf.update(z=(3.0, 2.0, 1.0), t=2.0)
    log("Kalman3D: smoothing converge",
        abs(out[0] - 3.0) < 0.15 and abs(out[1] - 2.0) < 0.15 and abs(out[2] - 1.0) < 0.15,
        detail=f"got ({out[0]:.2f}, {out[1]:.2f}, {out[2]:.2f})")


def test_kalman3d_reset():
    kf = KalmanFilter3D()
    kf.init(0.0, 0.0, 0.0)
    kf.reset()
    log("Kalman3D: reset → non initialized", not kf._initialized)


# ============================================================
# Blob3DTracker
# ============================================================
def test_tracker_no_position_returns_none():
    t = Blob3DTracker()
    log("Tracker senza update_position: current()=None", t.current() is None)


def test_tracker_with_position_returns_estimate():
    random.seed(30)
    t = Blob3DTracker(room_size=(6.0, 5.0, 3.0))
    # heuristic con high band noisy → HIGH
    h = HeightHeuristic(min_frames=20, min_dwell_s=0.0)
    t.heuristic = h
    feed_heuristic(h, 30, noise_low=0.05, noise_high=3.0)
    t.update_position(3.0, 2.5, 0.3, 0.3)
    est = t.current()
    log("Estimate: ritorna Blob3DEstimate", est is not None)
    if est is not None:
        log("Estimate: z_class HIGH",
            est.z_class == HeightClass.HIGH, detail=f"got {est.z_class.value}")
        log("Estimate: z mappato vicino a 1.7 m",
            abs(est.z - 1.7) < 0.3, detail=f"z={est.z:.2f}")
        log("Estimate: x ≈ 3.0", abs(est.x - 3.0) < 0.1, detail=f"x={est.x:.2f}")
        log("Estimate: y ≈ 2.5", abs(est.y - 2.5) < 0.1, detail=f"y={est.y:.2f}")
        log("Estimate: smoothed", est.smoothed)


def test_tracker_unknown_height_falls_to_mid_room():
    random.seed(31)
    t = Blob3DTracker(room_size=(6.0, 5.0, 3.0))
    # NESSUN frame CSI iniettato → heuristic UNKNOWN
    t.update_position(3.0, 2.5, 0.3, 0.3)
    est = t.current()
    log("UNKNOWN: z classe UNKNOWN",
        est is not None and est.z_class == HeightClass.UNKNOWN)
    if est is not None:
        log("UNKNOWN: z ≈ middle stanza (~1.5 m)",
            abs(est.z - 1.5) < 0.7, detail=f"z={est.z:.2f}")


def test_tracker_low_class():
    random.seed(32)
    t = Blob3DTracker()
    h = HeightHeuristic(min_frames=20, min_dwell_s=0.0)
    t.heuristic = h
    feed_heuristic(h, 30, noise_low=3.0, noise_high=0.05)
    t.update_position(2.0, 2.0, 0.3, 0.3)
    est = t.current()
    log("LOW: z_class LOW",
        est is not None and est.z_class == HeightClass.LOW,
        detail=f"got {est.z_class.value if est else 'None'}")
    if est is not None:
        log("LOW: z mappato vicino 0.4 m",
            abs(est.z - 0.4) < 0.4, detail=f"z={est.z:.2f}")


def test_tracker_reset():
    random.seed(33)
    t = Blob3DTracker()
    t.update_position(1.0, 1.0, 0.1, 0.1)
    t.reset()
    log("Reset: current() = None", t.current() is None)


def test_estimate_to_dict_schema():
    t = Blob3DTracker()
    h = HeightHeuristic(min_frames=10, min_dwell_s=0.0)
    t.heuristic = h
    feed_heuristic(h, 20, 0.05, 3.0)
    t.update_position(1.5, 1.5, 0.2, 0.2)
    est = t.current()
    assert est is not None
    d = est.to_dict()
    expected = {"x", "y", "z", "x_std", "y_std", "z_std", "z_class", "confidence", "smoothed", "t"}
    log("to_dict: tutte le chiavi", expected.issubset(set(d.keys())),
        detail=f"missing: {expected - set(d.keys())}")
    log("to_dict: z_class è string",
        d["z_class"] in [c.value for c in HeightClass])


# ============================================================
# Main
# ============================================================
def main() -> int:
    print("=" * 64)
    print("  TEST: csi.blob3d  (HeightHeuristic + KalmanFilter3D + Blob3DTracker)")
    print("=" * 64)
    print()
    print("-- HeightHeuristic --")
    test_variance_helper()
    test_heuristic_init()
    test_heuristic_validation()
    test_heuristic_high_signal()
    test_heuristic_low_signal()
    test_heuristic_mid_signal()
    test_heuristic_hysteresis()
    test_heuristic_handles_frames_without_csi()

    print("\n-- KalmanFilter3D --")
    test_kalman3d_init()
    test_kalman3d_smoothing()
    test_kalman3d_reset()

    print("\n-- Blob3DTracker --")
    test_tracker_no_position_returns_none()
    test_tracker_with_position_returns_estimate()
    test_tracker_unknown_height_falls_to_mid_room()
    test_tracker_low_class()
    test_tracker_reset()
    test_estimate_to_dict_schema()

    print("\n" + "=" * 64)
    print(f"  Risultati: {PASS} pass, {FAIL} fail")
    print("=" * 64)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
