#!/usr/bin/env python3
"""
test_detectors.py — Algorithm validation, zero WiFi hardware required.
Runs on Mac/Linux. Tests core detection logic with synthetic data.

Tests the actual logic used by:
  enhanced_presence.py — GradientDetector (RSSI gradient + consecutive same-sign)
  monitor_presence.py  — Radiotap header parsing
  decision_engine.py   — MsgPack encode/decode + fusion scoring

All implementations are self-contained (no imports from project files).
"""

import sys, os, random, json, struct, time
from collections import deque
from statistics import mean, stdev

PASS = 0
FAIL = 0

# ============================================================
# Self-contained algorithm implementations
# ============================================================

class GradientDetector:
    """Same logic as enhanced_presence.py GradientDetector."""

    def __init__(self, window_size=20, grad_threshold=1.0, consecutive_threshold=3):
        self.rssi_hist = deque(maxlen=window_size)
        self.grad_hist = deque(maxlen=window_size)
        self.ping_hist = deque(maxlen=window_size)
        self.grad_threshold = grad_threshold
        self.consecutive_threshold = consecutive_threshold
        self.baseline_grad_mean = None
        self.baseline_grad_std = None
        self.calibrated = False
        self._t0 = 0

    def update(self, metrics):
        now = time.time()
        if self._t0 == 0:
            self._t0 = now
        info = {"t": round(now - self._t0, 3)}

        rssi = metrics.get("rssi")
        if rssi is not None:
            self.rssi_hist.append(rssi)
            if len(self.rssi_hist) >= 2:
                grad = rssi - list(self.rssi_hist)[-2]
                self.grad_hist.append(grad)
                info["grad"] = grad

        pm = metrics.get("ping_mdev")
        if pm is not None:
            self.ping_hist.append(pm)

        if not self.calibrated and len(self.grad_hist) >= 15:
            grads = list(self.grad_hist)
            self.baseline_grad_mean = mean(grads) if grads else 0
            self.baseline_grad_std = stdev(grads) if len(grads) >= 2 else 0.5
            self.calibrated = True
            info["calibrated"] = True

        presence = False
        score = 0.0
        reasons = []

        if self.calibrated:
            recent = (list(self.grad_hist)[-5:] if len(self.grad_hist) >= 5
                      else list(self.grad_hist))
            if recent:
                max_abs_grad = max(abs(g) for g in recent)
                gs = (max_abs_grad - self.baseline_grad_mean) / max(self.baseline_grad_std, 0.1)
                if gs > self.grad_threshold:
                    score += gs
                    reasons.append(f"grad={max_abs_grad:.1f}")

            if len(self.grad_hist) >= self.consecutive_threshold:
                recent = list(self.grad_hist)[-self.consecutive_threshold:]
                non_zero = [g for g in recent if abs(g) > 0.1]
                if len(non_zero) >= 2:
                    cons = 1
                    for i in range(1, len(non_zero)):
                        if non_zero[i] * non_zero[i-1] > 0:
                            cons += 1
                        else:
                            cons = 1
                    cs = cons / self.consecutive_threshold
                    if cs >= 0.8:
                        score += cs * 2
                        reasons.append(f"cons={cons}")

            if len(self.ping_hist) >= 3:
                rp = list(self.ping_hist)[-3:]
                jitter = stdev(rp) if len(rp) > 1 else 0
                if jitter > 5:
                    score += 1
                    reasons.append(f"jitter={jitter:.1f}ms")

            presence = score > 1.5

        info["score"] = round(score, 2)
        info["presence"] = presence
        info["reasons"] = reasons
        info["calibrated"] = self.calibrated
        return presence, info


def parse_radiotap_rssi(data):
    """Same logic as monitor_presence.py parse_radiotap_rssi."""
    if len(data) < 8:
        return None
    hdr_len = struct.unpack_from('<H', data, 2)[0]
    present = struct.unpack_from('<I', data, 4)[0]
    if len(data) < hdr_len:
        return None
    rssi = None
    offset = 8
    bit = 0
    sizes = {0: 8, 1: 1, 2: 1, 3: 4, 4: 2, 5: 1, 6: 1,
             7: 2, 8: 2, 9: 2, 10: 1, 11: 1, 12: 1, 13: 2}
    while bit < 32:
        if present & (1 << bit):
            if offset + 1 > hdr_len:
                break
            if bit in (5, 10):
                rssi = struct.unpack_from('<b', data, offset)[0]
                offset += 1
            else:
                offset += sizes.get(bit, 1)
        bit += 1
        if offset >= hdr_len:
            break
    return rssi


def _msgpack_encode(obj):
    """Same as bridge_client.py MessagePack encoder."""
    if obj is None:
        return b'\xc0'
    if isinstance(obj, bool):
        return b'\xc3' if obj else b'\xc2'
    if isinstance(obj, int):
        if 0 <= obj <= 0x7f:
            return bytes([obj])
        if -32 <= obj < 0:
            return bytes([obj & 0xff])
        if obj <= 0xff:
            return b'\xcc' + bytes([obj])
        return b'\xce' + struct.pack('>I', abs(obj))
    if isinstance(obj, float):
        return b'\xcb' + struct.pack('>d', obj)
    if isinstance(obj, str):
        data = obj.encode()
        n = len(data)
        if n <= 0x1f:
            return bytes([0xa0 | n]) + data
        if n <= 0xff:
            return b'\xd9' + bytes([n]) + data
        return b'\xda' + struct.pack('>H', n) + data
    if isinstance(obj, (list, tuple)):
        n = len(obj)
        buf = bytes([0x90 | n]) if n <= 0x0f else b'\xdc' + struct.pack('>H', n)
        for item in obj:
            buf += _msgpack_encode(item)
        return buf
    return b''


def _msgpack_decode(data, pos=0):
    """Same as bridge_client.py MessagePack decoder."""
    if pos >= len(data):
        raise ValueError("truncated")
    b = data[pos]; pos += 1
    if b <= 0x7f: return b, pos
    if b >= 0xe0: return b - 256, pos
    if 0xa0 <= b <= 0xbf:
        n = b & 0x1f
        return data[pos:pos+n].decode(), pos+n
    if b == 0xc0: return None, pos
    if b == 0xc2: return False, pos
    if b == 0xc3: return True, pos
    if b == 0xca: return struct.unpack('>f', data[pos:pos+4])[0], pos+4
    if b == 0xcb: return struct.unpack('>d', data[pos:pos+8])[0], pos+8
    if b == 0xcc: return data[pos], pos+1
    if b == 0xcd: return struct.unpack('>H', data[pos:pos+2])[0], pos+2
    if b == 0xce: return struct.unpack('>I', data[pos:pos+4])[0], pos+4
    if b == 0xd0: return struct.unpack('>b', data[pos:pos+1])[0], pos+1
    if 0x90 <= b <= 0x9f:
        n = b & 0x0f
        result = [None] * n
        for i in range(n):
            val, pos = _msgpack_decode(data, pos)
            result[i] = val
        return result, pos
    if b == 0xdc:
        n = struct.unpack('>H', data[pos:pos+2])[0]; pos += 2
        result = [None] * n
        for i in range(n):
            val, pos = _msgpack_decode(data, pos)
            result[i] = val
        return result, pos
    if b == 0xd9:
        n = data[pos]; pos += 1
        return data[pos:pos+n].decode(), pos+n
    raise ValueError(f"unknown 0x{b:02x}")


# ============================================================
# Synthetic data generators
# ============================================================

def _make_radiotap_frame(rssi_val, rate=6):
    """Build synthetic radiotap + 802.11 probe request frame."""
    present = 0x404  # rate(bit2) + dBm ant signal(bit10)
    rt_len = 10
    hdr = struct.pack('<BBH', 0, 0, rt_len)
    hdr += struct.pack('<I', present)
    hdr += struct.pack('<B', rate)
    hdr += struct.pack('<b', rssi_val)
    fc = 0x0040  # probe request
    body = (struct.pack('<HH', fc, 0) +
            b'\xff\xff\xff\xff\xff\xff' +  # addr1 broadcast
            b'\xaa\xbb\xcc\xdd\xee\x01' +  # addr2 source
            b'\xff\xff\xff\xff\xff\xff' +  # addr3 BSSID
            struct.pack('<H', 0) + b'\x00\x00')
    return hdr + body


def _empty_room(n=300):
    """Realistic empty-room RSSI: stable, rare ±1 dBm changes."""
    r = []
    rssi = -40
    for _ in range(n):
        if random.random() < 0.12:
            rssi += random.choice([-1, 1])
        rssi = max(min(rssi, -39), -41)
        r.append({"rssi": int(rssi), "signal_avg": int(rssi),
                  "ping_mdev": round(random.uniform(0.1, 1.5), 2)})
    return r


def _occupied_room(n=300):
    """Realistic occupied-room RSSI: directional drift, consecutive gradients."""
    r = []
    rssi = -40; direction = 1; phase = 0
    for _ in range(n):
        phase += 1
        if phase > random.randint(8, 25):
            direction *= -1; phase = 0
        rssi += random.uniform(1.0, 3.0) * direction
        rssi = max(min(rssi, -30), -65)
        ping = random.uniform(0.5, 10.0) if random.random() < 0.3 else random.uniform(0.1, 2.0)
        r.append({"rssi": int(round(rssi)), "signal_avg": int(round(rssi)),
                  "ping_mdev": round(ping, 2)})
    return r


# ============================================================
# Test helpers
# ============================================================

def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1; print(f"  \u2717 {name}: {detail}")


def test_gradient_empty():
    print(f"\n--- Test 1: Empty room (low FP) ---")
    random.seed(42)
    d = GradientDetector(grad_threshold=5.0, consecutive_threshold=4)
    fp = 0; total = 0
    for s in _empty_room(300):
        p, info = d.update(s)
        if info.get("calibrated"):
            total += 1
            if p: fp += 1
    rate = fp / max(total, 1) * 100
    ok("FP < 5% on empty room", rate < 5, f"{rate:.1f}%")


def test_gradient_occupied():
    print(f"\n--- Test 2: Occupied room (high TP) ---")
    random.seed(42)
    d = GradientDetector(consecutive_threshold=4)
    tp = 0; total = 0
    for s in _occupied_room(300):
        p, info = d.update(s)
        if info.get("calibrated"):
            total += 1
            if p: tp += 1
    rate = tp / max(total, 1) * 100
    print(f"  TP={tp}/{total}={rate:.1f}%")
    ok("TP > 60% on movement", rate > 60, f"{rate:.1f}%")


def test_gradient_transitions():
    print(f"\n--- Test 3: State transitions ---")
    random.seed(42)
    d = GradientDetector(grad_threshold=5.0, consecutive_threshold=4)
    phases = [_empty_room(200), _occupied_room(200), _empty_room(200)]
    results = []
    for data in phases:
        pr = []
        for s in data:
            p, info = d.update(s)
            if info.get("calibrated"):
                pr.append(p)
        results.append(pr)
    if any(len(r) == 0 for r in results):
        ok("Transitions: enough calibrated data", False, "empty phase")
        return
    fp1 = sum(results[0])/len(results[0])*100
    tp = sum(results[1])/len(results[1])*100
    fp2 = sum(results[2])/len(results[2])*100
    print(f"  Empty: FP={fp1:.0f}%  Move: TP={tp:.0f}%  Post: FP={fp2:.0f}%")
    ok("Phase 1 FP < 15%", fp1 < 15, f"{fp1:.0f}%")
    ok("Phase 2 TP > 50%", tp > 50, f"{tp:.0f}%")
    ok("Phase 3 FP < 15% (post-movement)", fp2 < 15, f"{fp2:.0f}%")


def test_consecutive():
    print(f"\n--- Test 4: Consecutive same-sign ---")
    # Monotonic drop (movement pattern)
    random.seed(42)
    d = GradientDetector()
    for i in range(20): d.update({"rssi": -50})
    for i in range(6): d.update({"rssi": -50 - i})
    _, info = d.update({"rssi": -56})
    ok("Monotonic → consecutive detected", "cons=" in str(info.get("reasons", "")),
       f"reasons={info.get('reasons')}")

    # Oscillating (noise, not movement)
    d2 = GradientDetector()
    for i in range(20): d2.update({"rssi": -50})
    for i in range(6): d2.update({"rssi": -50 + (5 if i % 2 == 0 else -5)})
    _, info2 = d2.update({"rssi": -50})
    ok("Oscillation → no consecutive", "cons=" not in str(info2.get("reasons", "")),
       f"reasons={info2.get('reasons')}")


def test_edge_cases():
    print(f"\n--- Test 5: Edge cases ---")
    d = GradientDetector()
    p, info = d.update({})
    ok("Empty → no presence", not p)
    ok("Empty → not calibrated", not info.get("calibrated", False))

    for _ in range(50): d.update({"rssi": -40})
    p, info = d.update({"rssi": -40})
    ok("50x flat → no presence", not p, f"score={info['score']}")
    ok("50x flat → calibrated", info["calibrated"])

    d2 = GradientDetector()
    for _ in range(20): d2.update({"rssi": -50})
    # Use widely varying ping values so stdev([last_3]) > 5ms
    for v in [0, 0, 0, 0, 15]:
        d2.update({"rssi": -50, "ping_mdev": v})
    _, info2 = d2.update({"rssi": -50, "ping_mdev": 20})
    ok("Ping jitter bonus", "jitter" in str(info2.get("reasons", "")),
       f"reasons={info2.get('reasons')}")

    d3 = GradientDetector()
    for _ in range(20): d3.update({"rssi": -40})
    p3, info3 = d3.update({"rssi": -55})
    ok("15dBm drop → high score", info3["score"] > 4, f"score={info3['score']}")


def test_msgpack():
    print(f"\n--- Test 6: MsgPack roundtrip ---")
    cases = [
        (None, "None"), (True, "True"), (False, "False"),
        (0, "0"), (42, "42"), (-17, "-17"), (255, "255"),
        (3.14159, "pi"), ("hello", "str"),
        ([1, 2, 3], "list"), (["a", None, True], "mixed"),
    ]
    for val, name in cases:
        enc = _msgpack_encode(val)
        dec, _ = _msgpack_decode(enc)
        match = (dec == val) or (isinstance(val, float) and abs(dec - val) < 1e-4)
        ok(f"msgpack: {name}", match, f"decode={dec!r}")


def test_radiotap():
    print(f"\n--- Test 7: Radiotap parser ---")
    for expected in [-30, -55, -90]:
        frame = _make_radiotap_frame(expected)
        rssi = parse_radiotap_rssi(frame)
        ok(f"radiotap: RSSI={expected}", rssi == expected, f"got {rssi}")
    ok("short header → None", parse_radiotap_rssi(b'\x00\x00\x05\x00') is None)
    ok("empty → None", parse_radiotap_rssi(b'') is None)


def test_fusion():
    print(f"\n--- Test 8: Fusion scoring ---")
    def fusion(grad, mon, wg=0.5, wm=0.3, ws=0.2, th=0.5):
        gn = min(1.0, grad / 3.0)
        s = gn*wg + mon*wm
        return {"score": round(s, 3), "presence": s > th}

    ok("Empty fusion < 0.2", fusion(0.1, 0.1)["score"] < 0.2)
    ok("Occupied fusion > 0.5", fusion(2.5, 0.8)["score"] > 0.5)
    ok("Mid fusion 0.2-0.6", 0.2 < fusion(0.8, 0.5)["score"] < 0.6)


def test_calibration_logic():
    print(f"\n--- Test 9: Calibration logic ---")
    b = [0, 0, 1, -1, 0, 0, 0, 1, -1, 0]
    m = [0, -3, -5, -4, -2, 0, 3, 5, 4, 2]
    b_abs = [abs(g) for g in b]
    m_abs = [abs(g) for g in m]

    # Best threshold
    best = max((t/10 for t in range(5, 40)),
              key=lambda th: (sum(1 for g in m_abs if g > th)/10 -
                              sum(1 for g in b_abs if g > th)/10))
    ok("Threshold > 0.5", best > 0.5, f"best={best:.1f}")

    # Auto-calibration
    # N updates → N-1 gradients (first update has no prior RSSI).
    # Need 16 updates to get 15+ gradients.
    d = GradientDetector()
    for _ in range(15): d.update({"rssi": -40})
    ok("15 updates → 14 grads → not calibrated", not d.calibrated,
       f"grad_hist len={len(d.grad_hist)}")
    _, info = d.update({"rssi": -40})
    ok("16 updates → 15 grads → calibrated", d.calibrated,
       f"cal={d.calibrated}, grads={len(d.grad_hist)}, info_cal={info.get('calibrated')}")
    ok("Baseline mean computed", d.baseline_grad_mean is not None,
       f"mean={d.baseline_grad_mean}")
    ok("Baseline std computed", d.baseline_grad_std is not None,
       f"std={d.baseline_grad_std}")


# ============================================================
# Main
# ============================================================

def main():
    random.seed(42)

    print("=" * 50)
    print("PRESENCE DETECTION — Algorithm Tests")
    print(f"  {sys.platform} / Python {sys.version.split()[0]}")
    print("=" * 50)

    tests = [
        test_gradient_empty,
        test_gradient_occupied,
        test_gradient_transitions,
        test_consecutive,
        test_edge_cases,
        test_msgpack,
        test_radiotap,
        test_fusion,
        test_calibration_logic,
    ]

    for fn in tests:
        fn()

    total = PASS + FAIL
    print(f"\n{'='*50}")
    print(f"RESULTS: {PASS}/{total} passed, {FAIL}/{total} failed")
    if FAIL:
        print("\u26a0\ufe0f  Some tests failed")
    else:
        print("\u2713 All passed. Core algorithms validated with synthetic data.")
        print("  Remaining: hardware validation on Arduino UNO Q.")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
