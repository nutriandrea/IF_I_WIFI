#!/usr/bin/env python3
"""
Test per CSI Processor — parser CSV + CSIDetector.
Self-contained: nessuna importazione da file di calibrazione.
"""
import sys, os, json, math, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from csi.csi_processor import parse_csi_line, CSIDetector


# ============================================================
# Helper — genera linea CSI finta
# ============================================================

def _fake_csi_line(rssi: int = -45, num_sub: int = 64,
                   ampl_mean: float = 10.0, ampl_std: float = 2.0,
                   mac: str = "aa:bb:cc:dd:ee:ff",
                   channel: int = 1) -> str:
    """Genera una linea CSI_DATA finta per test."""
    import random
    random.seed(42)

    parts = [
        "CSI_DATA",     # 0: prefix (consumed by parser)
        "1",            # 1: type
        "1",            # 2: role
        mac,            # 3: mac
        str(rssi),      # 4: rssi
        "72",           # 5: rate
        "1",            # 6: sig_mode
        "7",            # 7: mcs
        "0",            # 8: bandwidth
        "0",            # 9: smoothing
        "1",            # 10: not_sounding
        "0",            # 11: aggregation
        "0",            # 12: stbc
        "1",            # 13: fec_coding
        "0",            # 14: sgi
        "-90",          # 15: noise_floor
        "0",            # 16: ampdu_cnt
        str(channel),   # 17: channel
        str(int(time.time() * 1000000)),  # 18: local_timestamp
        "0",            # 19: ant
        str(num_sub * 4),  # 20: sig_len
        "0",            # 21: rx_state
        str(num_sub * 4),  # 22: len
        "0",            # 23: first_word
    ]

    # Genera dati complessi con media/std controllati
    for _ in range(num_sub):
        # Campionatura da distribuzione normale per ogni subcarrier
        re = random.gauss(ampl_mean, ampl_std)
        im = random.gauss(ampl_mean * 0.3, ampl_std * 0.3)
        parts.append(f"{re:.3f}")
        parts.append(f"{im:.3f}")

    return ",".join(parts)


# ============================================================
# Test: parse_csi_line
# ============================================================

def test_parse_valid_csv():
    """Linea CSI_DATA valida produce dict strutturato."""
    line = _fake_csi_line(rssi=-45, num_sub=64)
    result = parse_csi_line(line)
    assert result is not None, "Valid CSI_DATA should be parsed"
    # Il prefisso "CSI_DATA" viene consumato; result["type"] è il campo dopo
    assert result["type"] == "1"  # type field value dopo il prefisso CSI_DATA
    assert result["rssi"] == -45
    assert result["num_subcarriers"] == 64
    assert len(result["csi"]) == 64
    for k in ("ampl_mean", "ampl_std", "ampl_max", "ampl_min"):
        assert k in result, f"Missing field: {k}"


def test_parse_invalid_prefix():
    """Linee non CSI_DATA vengono scartate."""
    assert parse_csi_line("NOT_CSI,1,2,3") is None
    assert parse_csi_line("") is None
    assert parse_csi_line("CSI_DATA") is None  # too few fields


def test_parse_csi_complex_values():
    """Verifica che i valori complessi siano parsati correttamente."""
    line = _fake_csi_line(num_sub=2, ampl_mean=10.0, ampl_std=0)
    result = parse_csi_line(line)
    assert result is not None
    assert result["num_subcarriers"] == 2
    # Subcarrier 0
    assert "real" in result["csi"][0]
    assert "imag" in result["csi"][0]
    assert "ampl" in result["csi"][0]
    assert "phase" in result["csi"][0]
    assert result["csi"][0]["subcarrier"] == 0
    assert result["csi"][1]["subcarrier"] == 1
    # Phase should be atan2(imag, real)
    assert isinstance(result["csi"][0]["phase"], float)
    assert isinstance(result["csi"][0]["ampl"], float)


def test_parse_mac_address():
    """MAC address viene estratto correttamente."""
    mac = "12:34:56:78:9a:bc"
    line = _fake_csi_line(mac=mac)
    result = parse_csi_line(line)
    assert result is not None
    assert result["mac"] == mac


def test_parse_different_sizes():
    """Supporto per 32, 64, 128, 256 subcarrier."""
    for num_sub in [32, 64, 128]:
        line = _fake_csi_line(num_sub=num_sub)
        result = parse_csi_line(line)
        assert result is not None
        assert result["num_subcarriers"] == num_sub
        assert len(result["csi"]) == num_sub


def test_parse_all_fields_present():
    """Tutti i campi dell'header CSI_DATA sono parsati."""
    line = _fake_csi_line(rssi=-55, channel=6, mac="aa:bb:cc:dd:ee:ff")
    result = parse_csi_line(line)
    assert result is not None
    for field in ["type", "role", "mac", "rssi", "rate", "sig_mode", "mcs",
                  "bandwidth", "channel", "noise_floor", "ant", "len"]:
        assert field in result, f"Missing field: {field}"


# ============================================================
# Test: CSIDetector
# ============================================================

def test_detector_not_calibrated_initially():
    """Detector non calibrato non rileva presenza."""
    det = CSIDetector()
    frame = {"ampl_std": 5.0, "rssi": -45, "ampl_mean": 10.0}
    presence, info = det.update(frame)
    assert not presence
    assert not info["calibrated"]


def test_detector_calibrates_after_window():
    """Dopo calibration_window frame, detector si calibra."""
    det = CSIDetector()
    for i in range(25):
        frame = {"ampl_std": 1.0, "rssi": -45, "ampl_mean": 10.0}
        presence, info = det.update(frame)
        # Calibration avviene al frame 19 (0-indexed: len>=20 dopo il 20esimo append)
        if i < 18:
            assert not info["calibrated"], f"Should not calibrate at {i}"

    # Dopo 25 frame dovrebbe essere calibrato
    # (cal_window=20, primo cal al frame 19 perche len>=20 dopo append del 20esimo)
    calibrated = []
    for i in range(20, 25):
        presence, info = det.update({"ampl_std": 1.0, "rssi": -45, "ampl_mean": 10.0})
        calibrated.append(info["calibrated"])
    assert any(calibrated), "Should be calibrated after 20+ frames"


def test_detector_empty_room():
    """In stanza vuota, ampl_std vicino a baseline → non presenza."""
    det = CSIDetector(ampl_threshold=3.0)
    # Calibrazione con ampl_std basso
    for i in range(25):
        frame = {"ampl_std": 1.0, "rssi": -45, "ampl_mean": 10.0}
        det.update(frame)

    # Stanza vuota: ampl_std normale
    presence, info = det.update({"ampl_std": 1.2, "rssi": -45, "ampl_mean": 10.0})
    assert not presence, f"Empty room should not trigger presence (score={info['score']})"


def test_detector_occupied():
    """Movimento aumenta ampl_std → presenza rilevata."""
    det = CSIDetector(ampl_threshold=3.0)
    # Calibrazione
    for i in range(25):
        det.update({"ampl_std": 1.0, "rssi": -45, "ampl_mean": 10.0})

    # Presenza: ampl_std molto più alto della baseline
    presence, info = det.update({"ampl_std": 8.0, "rssi": -50, "ampl_mean": 15.0})
    assert presence, f"High ampl_std should trigger presence (score={info['score']})"
    assert len(info["reasons"]) > 0


def test_detector_rssi_delta():
    """Grande variazione RSSI contribuisce allo score."""
    det = CSIDetector(ampl_threshold=10.0, var_threshold=1.5)
    # Calibrazione con RSSI stabile
    for i in range(25):
        det.update({"ampl_std": 1.0, "rssi": -45, "ampl_mean": 10.0})

    # RSSI drasticamente diverso
    presence, info = det.update({"ampl_std": 1.0, "rssi": -65, "ampl_mean": 10.0})
    # Dovrebbe contribuire rssi_delta
    assert info["score"] > 0
    has_rssi_reason = any("rssi_delta" in r for r in info["reasons"])
    assert has_rssi_reason, f"Expected rssi_delta reason, got {info['reasons']}"


def test_detector_transitions():
    """Transizioni presenza/non-presenza funzionano."""
    det = CSIDetector(ampl_threshold=3.0)
    # Calibrazione
    for i in range(25):
        det.update({"ampl_std": 1.0, "rssi": -45, "ampl_mean": 10.0})

    transitions = 0
    last_presence = False
    states = [
        (1.0, False),   # vuoto
        (1.0, False),   # vuoto
        (9.0, True),    # presenza
        (9.0, True),    # presenza
        (1.0, False),   # torna vuoto
        (1.0, False),   # vuoto
        (9.0, True),    # presenza
    ]
    for ampl_std, expected in states:
        presence, info = det.update({"ampl_std": ampl_std, "rssi": -45, "ampl_mean": 10.0})
        if presence != last_presence:
            transitions += 1
            last_presence = presence
        assert presence == expected, (
            f"ampl_std={ampl_std}: expected presence={expected}, "
            f"got {presence} (score={info['score']})"
        )

    # Dovremmo vedere transizioni vuoto→presenza e presenza→vuoto
    assert transitions >= 2, f"Expected ≥2 transitions, got {transitions}"


# ============================================================
# Test: CSI with CSIDetector (integration-like)
# ============================================================

def test_detector_with_realistic_data():
    """CSIDetector processa frame realistici (con subcarrier).
    Simula dati tipo ESP32: ampl_std calcolato dal parser."""
    det = CSIDetector(ampl_threshold=3.0)
    line_empty = _fake_csi_line(rssi=-42, ampl_mean=8.0, ampl_std=1.0)
    line_occupied = _fake_csi_line(rssi=-55, ampl_mean=12.0, ampl_std=8.0)

    # Calibrazione su stanza vuota
    for i in range(25):
        frame = parse_csi_line(line_empty)
        assert frame is not None
        det.update(frame)

    # Occupato
    frame = parse_csi_line(line_occupied)
    assert frame is not None
    presence, info = det.update(frame)
    assert presence, (
        f"Occupied should trigger presence (ampl_std={frame['ampl_std']}, "
        f"score={info['score']})"
    )


# ============================================================
# Test: Edge cases
# ============================================================

def test_edge_empty_ampl_hist():
    """Detector gestisce lista vuota di ampl_std."""
    det = CSIDetector()
    presence, info = det.update({"rssi": -45})
    assert not presence


def test_edge_partial_data():
    """Detector gestisce frame con campi mancanti."""
    det = CSIDetector()
    # Solo RSSI, niente CSI
    presence, _ = det.update({"rssi": -45})
    assert not presence
    # Solo ampl_std
    presence, info = det.update({"ampl_std": 2.0})
    assert not presence  # non calibrato
    assert not info["calibrated"]
    # Tutto presente
    for i in range(25):
        det.update({"ampl_std": 1.0, "rssi": -45, "ampl_mean": 10.0})
    presence, info = det.update({"ampl_std": 5.0, "rssi": -45, "ampl_mean": 10.0})
    assert presence
    # Poi frame vuoto
    presence, info = det.update({})
    assert not presence


def test_many_frames_no_memory_leak():
    """Window size limita la memoria: deque non cresce all'infinito."""
    det = CSIDetector(window_size=50)
    for i in range(200):
        det.update({"ampl_std": 1.0, "rssi": -45 + (i % 10), "ampl_mean": 10.0})
    assert len(det.ampl_std_hist) <= 50
    assert len(det.ampl_mean_hist) <= 50
    assert len(det.rssi_hist) <= 50


# ============================================================
# Test: Nuovo formato CSI (CSI:<seq>:...)
# ============================================================

def _fake_new_csi_line(rssi: int = -45, sub_count: int = 64,
                       ampl_mean: float = 10.0, ampl_std: float = 2.0) -> str:
    """Genera una linea nel nuovo formato CSI:<seq>:... per test.
    I dati sono interi (int8) come dal firmware ESP32 reale."""
    import random
    random.seed(7)

    parts = [
        "CSI:1",         # seq=1
        str(rssi),       # rssi
        "-90",           # noise_floor
        "72",            # rate
        "20",            # bandwidth
        str(sub_count),  # sub_count
    ]

    # Genera dati interi (int8 come firmware reale)
    vals = []
    for _ in range(sub_count):
        re = int(random.gauss(ampl_mean, ampl_std))
        im = int(random.gauss(ampl_mean * 0.3, ampl_std * 0.3))
        vals.append(str(re))
        vals.append(str(im))
    parts.append(",".join(vals))

    return ":".join(parts)


def test_new_format_parse():
    """Nuovo formato CSI:<seq>:... parsato correttamente."""
    line = _fake_new_csi_line(rssi=-45, sub_count=64)
    result = parse_csi_line(line)
    assert result is not None, "Valid new format should be parsed"
    assert result["rssi"] == -45
    assert result["num_subcarriers"] == 64
    assert len(result["csi"]) == 64
    assert result["seq"] == 1
    assert result["noise_floor"] == -90
    assert result["rate"] == 72
    assert result["bandwidth"] == 20
    for k in ("ampl_mean", "ampl_std", "ampl_max", "ampl_min"):
        assert k in result, f"Missing field: {k}"


def test_new_format_invalid():
    """Linee malformate nel nuovo formato vengono scartate."""
    assert parse_csi_line("CSI:x:bad") is None
    assert parse_csi_line("CSI:") is None
    assert parse_csi_line("") is None


def test_new_format_subcarrier_values():
    """Valori complessi parsati correttamente nel nuovo formato."""
    line = _fake_new_csi_line(sub_count=2, ampl_mean=10.0, ampl_std=0)
    result = parse_csi_line(line)
    assert result is not None
    assert result["num_subcarriers"] == 2
    assert "real" in result["csi"][0]
    assert "imag" in result["csi"][0]
    assert "ampl" in result["csi"][0]
    assert "phase" in result["csi"][0]
    assert result["csi"][0]["subcarrier"] == 0
    assert result["csi"][1]["subcarrier"] == 1


def test_new_format_zero_subcarriers():
    """Nuovo formato con 0 subcarrier non crasha."""
    line = "CSI:1:-45:-90:72:20:0:"
    result = parse_csi_line(line)
    assert result is not None
    assert result["num_subcarriers"] == 0
    assert result["csi"] == []


def test_both_formats_consistent():
    """Vecchio e nuovo formato producono ampl_std simili per stessi dati."""
    line_old = _fake_csi_line(rssi=-50, num_sub=32, ampl_mean=10.0, ampl_std=3.0)
    line_new = _fake_new_csi_line(rssi=-50, sub_count=32, ampl_mean=10.0, ampl_std=3.0)
    r_old = parse_csi_line(line_old)
    r_new = parse_csi_line(line_new)
    assert r_old is not None and r_new is not None
    # ampl_std dovrebbe essere simile (stessa distribuzione)
    assert abs(r_old["ampl_std"] - r_new["ampl_std"]) < 2.5, (
        f"ampl_std mismatch: old={r_old['ampl_std']:.2f} new={r_new['ampl_std']:.2f}"
    )


# ============================================================
# Run
# ============================================================

if __name__ == "__main__":
    import inspect
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    total = passed + failed
    print(f"\n  {total} test, {passed} passati, {failed} falliti")
    sys.exit(0 if failed == 0 else 1)
