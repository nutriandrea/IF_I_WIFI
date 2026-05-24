#!/usr/bin/env python3
"""
test_presence.py — Test PresenceDetector con frame sintetici.

Zero hardware. Stessa convenzione standalone degli altri test:
contatore PASS/FAIL, exit code != 0 se qualche test fallisce.

Esegui:
    PYTHONPATH=. python3 tests/test_presence.py
"""
from __future__ import annotations

import math
import os
import random
import sys
import time
from statistics import mean

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from csi.presence.detector import (
    PresenceDetector,
    PresenceReading,
    PresenceState,
    _std,
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
# Helpers: frame factory
# ============================================================
def make_frame(seq: int, tx: int, rx: int, ampl_mean: float) -> dict:
    return {
        "seq": seq,
        "tx_node": tx,
        "rx_node": rx,
        "ampl_mean": ampl_mean,
        "rssi": -50,
    }


def feed_quiet(det: PresenceDetector, n: int, ampl: float = 10.0,
               jitter: float = 0.05, n_tx: int = 3, n_rx: int = 3):
    """Inietta n frame "stanza vuota": ampiezza stabile con minima oscillazione."""
    for i in range(n):
        for tx in range(n_tx):
            for rx in range(n_rx):
                det.add_frame(make_frame(i, tx, rx, ampl + random.gauss(0, jitter)))


def feed_moving(det: PresenceDetector, n: int, ampl: float = 10.0,
                jitter: float = 2.0, n_tx: int = 3, n_rx: int = 3):
    """Inietta n frame con grande variazione (movimento)."""
    for i in range(n):
        for tx in range(n_tx):
            for rx in range(n_rx):
                det.add_frame(make_frame(i, tx, rx, ampl + random.gauss(0, jitter)))


def feed_stationary(det: PresenceDetector, n: int, ampl: float = 10.0,
                    jitter: float = 0.3, n_tx: int = 3, n_rx: int = 3):
    """Inietta n frame con variazione intermedia (persona ferma)."""
    for i in range(n):
        for tx in range(n_tx):
            for rx in range(n_rx):
                det.add_frame(make_frame(i, tx, rx, ampl + random.gauss(0, jitter)))


# ============================================================
# Tests: helpers
# ============================================================
def test_std_helper():
    log("std() di lista vuota = 0", _std([]) == 0.0)
    log("std() di un solo valore = 0", _std([5.0]) == 0.0)
    log("std() di [1,1,1,1] = 0", _std([1, 1, 1, 1]) == 0.0)
    # std popolazione di [1,2,3,4,5] = sqrt(2)
    ok = abs(_std([1, 2, 3, 4, 5]) - math.sqrt(2)) < 1e-9
    log("std() di [1..5] ≈ sqrt(2)", ok)


# ============================================================
# Tests: detector lifecycle
# ============================================================
def test_uncalibrated_state():
    d = PresenceDetector(window_size=10, baseline_seconds=30.0)
    r = d.current_reading()
    log("Iniziale: state UNKNOWN", r.state == PresenceState.UNKNOWN)
    log("Iniziale: not calibrated", r.calibrated is False)
    log("Iniziale: calibration_progress 0", r.calibration_progress == 0.0)


def test_calibration_progress():
    # Calibrazione cortissima: 0.5 secondi
    d = PresenceDetector(window_size=10, baseline_seconds=0.5)
    feed_quiet(d, 20, jitter=0.05)
    r = d.current_reading()
    # Subito dopo feed: la calibrazione è iniziata da pochi ms → ancora in corso
    # (a meno che il primo aggregato non sia arrivato dopo > 0.5s, improbabile in test)
    log("Calibrazione progresso > 0 dopo feed", r.calibration_progress > 0)
    # Sleep 0.6s e injecto un altro frame → trigger della finalize
    time.sleep(0.6)
    d.add_frame(make_frame(999, 0, 0, 10.0))
    log("Calibrato dopo baseline_seconds", d.is_calibrated())


def test_empty_room_after_calibration():
    random.seed(42)
    d = PresenceDetector(window_size=20, baseline_seconds=0.2, min_dwell_s=0.0)
    feed_quiet(d, 50, jitter=0.05)
    time.sleep(0.25)
    feed_quiet(d, 50, jitter=0.05)
    r = d.current_reading()
    log("Dopo cal + quiet: state EMPTY", r.state == PresenceState.EMPTY,
        detail=f"got {r.state}, intensity {r.intensity:.3f}, baseline {r.baseline:.3f}")
    log("Dopo cal + quiet: calibrated", r.calibrated)


def test_movement_detection():
    random.seed(7)
    d = PresenceDetector(window_size=20, baseline_seconds=0.2, min_dwell_s=0.0,
                         empty_mult=1.5, move_mult=4.0)
    feed_quiet(d, 50, jitter=0.05)
    time.sleep(0.25)
    # Dopo calibrazione, inietta movimento forte (jitter molto più grande del baseline)
    feed_moving(d, 50, jitter=5.0)
    r = d.current_reading()
    log("Dopo movement: state MOTION", r.state == PresenceState.MOTION,
        detail=f"got {r.state}, intensity {r.intensity:.3f}, baseline {r.baseline:.3f}")
    log("Dopo movement: confidence > 0", r.confidence > 0)


def test_stationary_detection():
    random.seed(13)
    d = PresenceDetector(window_size=20, baseline_seconds=0.2, min_dwell_s=0.0,
                         empty_mult=1.5, move_mult=4.0)
    feed_quiet(d, 50, jitter=0.05)
    time.sleep(0.25)
    # Calibrazione: baseline ~0.05, soglie a 0.075 (EMPTY/STILL) e 0.2 (STAT/MOVE).
    # Variazione "stazionaria": > 1.5x baseline ma < 4x.
    feed_stationary(d, 50, jitter=0.12)
    r = d.current_reading()
    log("Stationary: state STILL", r.state == PresenceState.STILL,
        detail=f"got {r.state}, intensity {r.intensity:.3f}, baseline {r.baseline:.3f}")


def test_state_transitions():
    """quiet → moving → quiet : transitions and durations make sense.

    min_dwell_s richiede tempo wall-clock fra feed e reading; alterno feed
    a piccoli sleep per simulare un input continuo realistico.
    """
    random.seed(99)
    d = PresenceDetector(window_size=20, baseline_seconds=0.2, min_dwell_s=0.05,
                         empty_mult=1.5, move_mult=4.0)
    feed_quiet(d, 50, jitter=0.05)
    time.sleep(0.25)
    # Drain post-calibrazione: alcuni cicli con feed+sleep per maturare il dwell
    for _ in range(3):
        feed_quiet(d, 10, jitter=0.05)
        time.sleep(0.05)
    r1 = d.current_reading()

    # Fase movimento: anche qui feed+sleep per attraversare il dwell
    for _ in range(5):
        feed_moving(d, 10, jitter=5.0)
        time.sleep(0.05)
    r2 = d.current_reading()

    # Ritorno a quiet: tanti frame + sleep per far decadere EMA e maturare dwell
    for _ in range(20):
        feed_quiet(d, 20, jitter=0.05)
        time.sleep(0.02)
    r3 = d.current_reading()

    log("Transition: r1 EMPTY", r1.state == PresenceState.EMPTY,
        detail=f"r1={r1.state}")
    log("Transition: r2 MOTION", r2.state == PresenceState.MOTION,
        detail=f"r2={r2.state}")
    log("Transition: r3 torna EMPTY o STILL",
        r3.state in (PresenceState.EMPTY, PresenceState.STILL),
        detail=f"r3={r3.state}, intensity={r3.intensity:.3f}")


def test_single_node_degraded():
    """Solo 1 ESP32 (tx=0, rx=0): il detector deve continuare a funzionare."""
    random.seed(21)
    d = PresenceDetector(window_size=20, baseline_seconds=0.2, min_dwell_s=0.0)
    feed_quiet(d, 50, jitter=0.05, n_tx=1, n_rx=1)
    time.sleep(0.25)
    feed_moving(d, 50, jitter=5.0, n_tx=1, n_rx=1)
    r = d.current_reading()
    log("Single-node: detector calibrato", r.calibrated)
    log("Single-node: 1 percorso attivo", r.n_active_paths == 1,
        detail=f"got {r.n_active_paths}")
    log("Single-node: movimento rilevato", r.state == PresenceState.MOTION,
        detail=f"got {r.state}")


def test_hysteresis_dwell_time():
    """Verifica che cambi di stato richiedano dwell_s >= soglia."""
    random.seed(33)
    d = PresenceDetector(window_size=20, baseline_seconds=0.2, min_dwell_s=0.5,
                         empty_mult=1.5, move_mult=4.0)
    feed_quiet(d, 50, jitter=0.05)
    time.sleep(0.25)
    # Drain post-calibrazione per maturare la transizione UNKNOWN→EMPTY (dwell 0.5s)
    for _ in range(20):
        feed_quiet(d, 5, jitter=0.05)
        time.sleep(0.03)
    r0 = d.current_reading()
    if r0.state != PresenceState.EMPTY:
        log("Hysteresis pre-condizione: state EMPTY", False,
            detail=f"got {r0.state}")
        return
    log("Hysteresis pre-condizione: state EMPTY", True)

    # Inietta movimento UNA VOLTA, senza aspettare dwell_s (0.5s)
    feed_moving(d, 30, jitter=5.0)
    r1 = d.current_reading()
    log("Hysteresis: senza dwell, state ancora EMPTY",
        r1.state == PresenceState.EMPTY,
        detail=f"got {r1.state}")

    # Continua a iniettare movement per > min_dwell_s
    for _ in range(25):
        feed_moving(d, 5, jitter=5.0)
        time.sleep(0.03)
    r2 = d.current_reading()
    log("Hysteresis: dopo dwell state cambia in MOTION",
        r2.state == PresenceState.MOTION,
        detail=f"got {r2.state}")


def test_reset_calibration():
    d = PresenceDetector(window_size=10, baseline_seconds=0.2, min_dwell_s=0.0)
    feed_quiet(d, 30, jitter=0.05)
    time.sleep(0.25)
    feed_quiet(d, 30, jitter=0.05)
    assert d.is_calibrated(), "should be calibrated before reset"

    d.reset_calibration()
    r = d.current_reading()
    log("Reset: calibrated=False", not r.calibrated)
    log("Reset: state UNKNOWN", r.state == PresenceState.UNKNOWN)
    log("Reset: progress 0", r.calibration_progress < 0.5)


def test_to_json_line_schema():
    """Snapshot serializzabile JSON con i campi attesi."""
    d = PresenceDetector(window_size=10, baseline_seconds=0.2, min_dwell_s=0.0)
    feed_quiet(d, 20, jitter=0.05)
    r = d.current_reading()
    line = r.to_json_line()
    import json
    obj = json.loads(line)
    expected_keys = {"state", "confidence", "intensity", "intensity_raw",
                     "baseline", "duration_s", "per_rx_intensity",
                     "n_active_paths", "n_total_paths", "calibrated",
                     "calibration_progress", "t"}
    log("JSON line: tutte le chiavi presenti",
        expected_keys.issubset(set(obj.keys())),
        detail=f"missing: {expected_keys - set(obj.keys())}")
    log("JSON line: state è string valida",
        obj["state"] in [s.value for s in PresenceState])


def test_param_validation():
    try:
        PresenceDetector(ema_alpha=0.0)
        log("Validation: ema_alpha=0 → ValueError", False, "no error raised")
    except ValueError:
        log("Validation: ema_alpha=0 → ValueError", True)

    try:
        PresenceDetector(empty_mult=5.0, move_mult=2.0)
        log("Validation: empty_mult > move_mult → ValueError", False)
    except ValueError:
        log("Validation: empty_mult > move_mult → ValueError", True)

    try:
        PresenceDetector(window_size=1)
        log("Validation: window_size < 3 → ValueError", False)
    except ValueError:
        log("Validation: window_size < 3 → ValueError", True)


def test_robust_to_missing_ampl_mean():
    d = PresenceDetector(window_size=10, baseline_seconds=0.2, min_dwell_s=0.0)
    # Frame senza ampl_mean → ignorato silenziosamente
    d.add_frame({"seq": 1, "tx_node": 0, "rx_node": 0})  # niente ampl_mean
    r = d.current_reading()
    log("Frame senza ampl_mean: detector non si rompe",
        r.state == PresenceState.UNKNOWN)


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 64)
    print("  TEST: PresenceDetector")
    print("=" * 64)

    test_std_helper()
    test_uncalibrated_state()
    test_calibration_progress()
    test_empty_room_after_calibration()
    test_movement_detection()
    test_stationary_detection()
    test_state_transitions()
    test_single_node_degraded()
    test_hysteresis_dwell_time()
    test_reset_calibration()
    test_to_json_line_schema()
    test_param_validation()
    test_robust_to_missing_ampl_mean()

    print("\n" + "=" * 64)
    print(f"  Risultati: {PASS} pass, {FAIL} fail")
    print("=" * 64)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
