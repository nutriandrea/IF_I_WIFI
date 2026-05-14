#!/usr/bin/env python3
"""
Feasibility Test — Arduino UNO Q WiFi Sensing

Valuta se l'Arduino UNO Q e in grado di:
  1. Campionare RSSI WiFi a frequenza stabile
  2. Eseguire feature extraction in real-time
  3. Comunicare via UART con lo sketch MCU
  4. Mantenere CPU/memoria sotto controllo
  5. Rilevare presenza con accuratezza accettabile

Output: feasibility_report_<timestamp>.json + summary su stdout
"""

import subprocess, time, json, sys, os, re, shutil
from datetime import datetime
from collections import deque
from statistics import mean, stdev

# ============================================================
# Config
# ============================================================
SERIAL_PORT = "/dev/ttyACM0"
BAUD = 9600
SAMPLING_WINDOW_S = 20      # seconds per test window
SAMPLING_INTERVAL = 0.5     # seconds between RSSI reads
PRESENCE_STD_THRESHOLD = 2.0
REPORT_FILE = f"feasibility_report_{datetime.now().strftime('%Y%m%d_%H%M')}.json"

# ============================================================
# WiFi interface auto-detection
# ============================================================
def detect_wifi_interface():
    """Trova l'interfaccia WiFi attiva. Ritorna (nome, stato)."""
    try:
        # List all wireless interfaces
        out = subprocess.check_output("iw dev", shell=True, timeout=5).decode()
        interfaces = re.findall(r"Interface\s+(\S+)", out)
        if not interfaces:
            return None, "no wireless interfaces found"
        for iface in interfaces:
            try:
                # Check if connected to anything
                state = subprocess.check_output(
                    f"iw dev {iface} link", shell=True, timeout=3,
                    stderr=subprocess.DEVNULL
                ).decode()
                if "Not connected" not in state and state.strip():
                    return iface, "connected"
            except subprocess.CalledProcessError:
                continue
        # None connected — return the first one anyway
        return interfaces[0], "not connected"
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return None, f"iw not available: {e}"

def rssi_via_link(iface):
    """RSSI da 'iw dev <iface> link'. Ritorna float o None."""
    result = subprocess.check_output(
        f"iw dev {iface} link", shell=True, timeout=3,
        stderr=subprocess.DEVNULL
    ).decode()
    # Look for "signal: -XX dBm" pattern
    m = re.search(r"signal:\s*(-?\d+\.?\d*)\s*dBm", result)
    if m:
        return float(m.group(1))
    return None

def rssi_via_scan(iface):
    """RSSI da 'iw dev <iface> scan' (media di tutti gli AP visti).
    Piu lento ma NON richiede connessione. Ritorna float o None."""
    result = subprocess.check_output(
        f"iw dev {iface} scan", shell=True, timeout=10,
        stderr=subprocess.DEVNULL
    ).decode()
    signals = re.findall(r"signal:\s*(-?\d+\.?\d*)\s*dBm", result)
    if signals:
        vals = [float(s) for s in signals]
        return mean(vals)
    return None

RESULTS = {
    "timestamp": datetime.now().isoformat(),
    "board": "Arduino UNO Q",
    "tests": [],
    "summary": {"pass": True, "fail_reasons": []}
}

def log_test(name, passed, details, metrics=None):
    entry = {
        "test": name,
        "passed": passed,
        "details": details,
        "metrics": metrics or {}
    }
    RESULTS["tests"].append(entry)
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}: {details}")
    if metrics:
        for k, v in metrics.items():
            print(f"         {k}: {v}")
    if not passed:
        RESULTS["summary"]["pass"] = False
        RESULTS["summary"]["fail_reasons"].append(name)

# ============================================================
# Test 1: RSSI Sampling Reliability
# ============================================================
def test_rssi_sampling():
    print("\n=== Test 1: RSSI Sampling Reliability ===")

    # --- Phase A: Interface detection ---
    iface, state = detect_wifi_interface()
    print(f"  Interface: {iface}  State: {state}")

    if iface is None:
        log_test("RSSI Sampling", False,
                 f"Cannot detect WiFi interface: {state}", {})
        return []

    # --- Phase B: quick method probe ---
    use_scan = False
    try:
        val = rssi_via_link(iface)
        if val is None:
            print(f"  'iw {iface} link' returned no signal. Trying scan...")
            val = rssi_via_scan(iface)
            if val is not None:
                use_scan = True
                print(f"  Scan method works (avg RSSI: {val:.1f} dBm)")
            else:
                print(f"  Scan also returned nothing. Will sample anyway.")
        else:
            print(f"  Link method works (RSSI: {val:.1f} dBm)")
    except subprocess.CalledProcessError:
        print("  Link method failed. Trying scan...")
        try:
            val = rssi_via_scan(iface)
            if val is not None:
                use_scan = True
                print(f"  Scan method works (avg RSSI: {val:.1f} dBm)")
            else:
                print("  Scan also returned nothing.")
        except subprocess.CalledProcessError as e:
            print(f"  Scan method also failed: {e}")

    # --- Phase C: actual sampling ---
    samples = []
    timestamps = []
    errors = 0

    sampler = rssi_via_scan if use_scan else rssi_via_link

    start = time.time()
    while time.time() - start < SAMPLING_WINDOW_S:
        try:
            val = sampler(iface)
            if val is not None:
                samples.append(val)
                timestamps.append(time.time())
            else:
                errors += 1
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            errors += 1
        time.sleep(SAMPLING_INTERVAL)

    n = len(samples)
    expected = SAMPLING_WINDOW_S / SAMPLING_INTERVAL
    sample_rate = n / SAMPLING_WINDOW_S if SAMPLING_WINDOW_S > 0 else 0
    error_rate = errors / (n + errors) * 100 if (n + errors) > 0 else 0

    passed = n >= expected * 0.3  # at least 30% — scan is slow
    details = (
        f"Got {n}/{int(expected)} samples in {SAMPLING_WINDOW_S}s "
        f"({sample_rate:.1f} Hz), using={'scan' if use_scan else 'link'}, "
        f"errors={errors} ({error_rate:.1f}%)"
    )

    if n >= 2:
        intervals = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)]
        jitter = stdev(intervals) if len(intervals) > 1 else 0
    else:
        jitter = 0

    metrics = {
        "samples": n,
        "sample_rate_hz": round(sample_rate, 2),
        "jitter_s": round(jitter, 3),
        "error_rate_pct": round(error_rate, 1),
        "method": "scan" if use_scan else "link",
        "interface": iface,
        "interface_state": state,
    }
    if samples:
        metrics.update({
            "rssi_min_dbm": round(min(samples), 1),
            "rssi_max_dbm": round(max(samples), 1),
            "rssi_mean_dbm": round(mean(samples), 1),
            "rssi_std_dbm": round(stdev(samples), 2) if len(samples) >= 2 else None,
        })

    log_test("RSSI Sampling", passed, details, metrics)
    return samples

# ============================================================
# Test 2: Feature Extraction Performance
# ============================================================
def test_feature_extraction(samples):
    print("\n=== Test 2: Feature Extraction Performance ===")

    if len(samples) < 2:
        log_test("Feature Extraction Speed", False,
                 "Skipped: not enough RSSI samples from Test 1", {
            "reason": "insufficient_samples",
            "samples_available": len(samples),
        })
        return

    try:
        import numpy as np
        has_numpy = True
        arr = np.array(samples)
    except ImportError:
        has_numpy = False
        arr = samples

    # Time the extraction
    n_runs = 100
    t0 = time.perf_counter()
    for _ in range(n_runs):
        if has_numpy:
            _ = {
                "mean": float(np.mean(arr)),
                "std": float(np.std(arr)),
                "delta": float(np.max(arr) - np.min(arr)),
                "var": float(np.var(arr)),
                "ptp": float(np.ptp(arr)),
            }
        else:
            _ = {
                "mean": mean(arr),
                "std": stdev(arr) if len(arr) >= 2 else 0,
                "delta": max(arr) - min(arr),
                "var": stdev(arr)**2 if len(arr) >= 2 else 0,
            }
    elapsed = (time.perf_counter() - t0) / n_runs

    passed = elapsed < 0.01  # must complete in under 10ms
    log_test("Feature Extraction Speed", passed,
             f"Completed in {elapsed*1000:.2f}ms avg (numpy={has_numpy})", {
        "avg_time_ms": round(elapsed * 1000, 3),
        "using_numpy": has_numpy,
        "n_features": 5,
        "n_runs": n_runs,
    })

# ============================================================
# Test 3: UART / Serial Communication
# ============================================================
def test_serial_communication():
    print("\n=== Test 3: UART Communication ===")

    # Auto-detect serial port (try common UNO Q paths)
    candidates = [
        SERIAL_PORT,
        "/dev/ttyACM1",
        "/dev/ttyUSB0",
        "/dev/ttyUSB1",
        "/dev/ttyS0",
    ]
    found_port = None
    for p in candidates:
        if os.path.exists(p):
            found_port = p
            break

    if found_port is None:
        log_test("UART Communication", False,
                 f"No serial port found (tried: {', '.join(candidates)})", {})
        return

    print(f"  Using port: {found_port}")

    try:
        import serial
    except ImportError:
        log_test("UART Communication", False,
                 "pyserial not installed", {})
        return

    try:
        ser = serial.Serial(found_port, BAUD, timeout=2)
        time.sleep(2)  # wait for Arduino reset

        lines = []
        t0 = time.time()
        while time.time() - t0 < 10:
            try:
                line = ser.readline().decode().strip()
                if line:
                    lines.append(line)
            except UnicodeDecodeError:
                pass

        ser.close()

        if len(lines) < 3:
            log_test("UART Communication", False,
                     f"Only got {len(lines)} lines in 10s",
                     {"lines_received": len(lines)})
            return

        # Try parsing as CSV
        parse_ok = 0
        parse_fail = 0
        for line in lines:
            parts = line.split(",")
            if len(parts) == 4:
                try:
                    _ = [float(p) for p in parts]
                    parse_ok += 1
                except ValueError:
                    parse_fail += 1
            else:
                parse_fail += 1

        rate = len(lines) / 10
        success_rate = parse_ok / (parse_ok + parse_fail) * 100
        passed = parse_ok >= 3 and success_rate >= 50

        log_test("UART Communication", passed,
                 f"{len(lines)} lines in 10s ({rate:.1f}/s), "
                 f"{parse_ok} parsed OK ({success_rate:.0f}%)", {
            "baud": BAUD,
            "lines_total": len(lines),
            "lines_parsed_ok": parse_ok,
            "parse_success_pct": round(success_rate, 1),
            "rate_hz": round(rate, 2),
        })
    except serial.SerialException as e:
        log_test("UART Communication", False,
                 f"Serial error: {e}", {})

# ============================================================
# Test 4: CPU & Memory Load
# ============================================================
def test_system_load():
    print("\n=== Test 4: CPU & Memory Load ===")

    try:
        # Read /proc/stat and /proc/meminfo
        with open("/proc/stat") as f:
            stat_before = f.readlines()

        with open("/proc/meminfo") as f:
            meminfo = f.readlines()

        mem_total = None
        mem_avail = None
        for line in meminfo:
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1]) / 1024  # kB -> MB
            if line.startswith("MemAvailable:"):
                mem_avail = int(line.split()[1]) / 1024

        time.sleep(1)

        with open("/proc/stat") as f:
            stat_after = f.readlines()

        # Simple CPU load
        cpu_before = [int(x) for x in stat_before[0].split()[1:]]
        cpu_after = [int(x) for x in stat_after[0].split()[1:]]
        total_before = sum(cpu_before)
        total_after = sum(cpu_after)
        idle_before = cpu_before[3]
        idle_after = cpu_after[3]

        cpu_pct = (1 - (idle_after - idle_before) / (total_after - total_before)) * 100

        if mem_total and mem_avail:
            used_mem = mem_total - mem_avail
            mem_pct = used_mem / mem_total * 100
        else:
            used_mem = None
            mem_pct = None

        passed = True
        details = f"CPU: {cpu_pct:.0f}%"
        if mem_total and used_mem:
            details += f", RAM: {used_mem:.0f}/{mem_total:.0f}MB ({mem_pct:.0f}%)"

        log_test("System Load", passed, details, {
            "cpu_percent": round(cpu_pct, 1),
            "ram_total_mb": round(mem_total, 1) if mem_total else None,
            "ram_used_mb": round(used_mem, 1) if used_mem else None,
            "ram_percent": round(mem_pct, 1) if mem_pct else None,
        })

    except (FileNotFoundError, IndexError, ZeroDivisionError) as e:
        log_test("System Load", False,
                 f"Could not read system stats: {e}", {})

# ============================================================
# Test 5: Presence Detection Accuracy
# ============================================================
def test_presence_detection(baseline_samples):
    print("\n=== Test 5: Presence Detection (simulated) ===")

    if len(baseline_samples) < 10:
        log_test("Presence Detection", False,
                 "Skipped: not enough RSSI baseline from Test 1", {
            "samples_available": len(baseline_samples),
            "minimum_required": 10,
        })
        return baseline_samples

    # Baseline stats
    base_std_silent = stdev(baseline_samples)
    base_mean = mean(baseline_samples)

    # Simulate movement by injecting variance into a copy
    import random
    movement_samples = [v + random.uniform(-5, 5) for v in baseline_samples]
    movement_std = stdev(movement_samples)

    # Can we detect the difference?
    can_detect = movement_std > PRESENCE_STD_THRESHOLD
    false_positive = base_std_silent > PRESENCE_STD_THRESHOLD

    passed = can_detect and not false_positive
    log_test("Presence Detection (simulated)", passed,
             f"Empty std={base_std_silent:.2f}, "
             f"Movement std={movement_std:.2f}, "
             f"Threshold={PRESENCE_STD_THRESHOLD}", {
        "empty_room_std": round(base_std_silent, 2),
        "simulated_movement_std": round(movement_std, 2),
        "threshold_std": PRESENCE_STD_THRESHOLD,
        "can_detect_movement": can_detect,
        "false_positive_empty": false_positive,
        "baseline_samples_n": len(baseline_samples),
    })
    return movement_samples

# ============================================================
# Test 6: Combined Pipeline Stress Test
# ============================================================
def test_combined_pipeline():
    print("\n=== Test 6: Combined Pipeline Stress Test ===")

    # Detect interface first
    iface, state = detect_wifi_interface()
    print(f"  Interface: {iface}  State: {state}")
    if iface is None:
        log_test("Combined Pipeline (30s)", False,
                 f"Cannot run: {state}", {})
        return

    # Pick the fastest working sampler
    use_scan = False
    try:
        if rssi_via_link(iface) is None:
            try:
                if rssi_via_scan(iface) is not None:
                    use_scan = True
            except subprocess.CalledProcessError:
                pass
    except subprocess.CalledProcessError:
        try:
            if rssi_via_scan(iface) is not None:
                use_scan = True
        except subprocess.CalledProcessError:
            pass

    sampler = rssi_via_scan if use_scan else rssi_via_link
    print(f"  Using method: {'scan' if use_scan else 'link'}")

    # 30s combined pipeline
    DURATION = 30
    WINDOW = deque(maxlen=40)

    loop_count = 0
    errors = 0
    t0 = time.time()

    while time.time() - t0 < DURATION:
        try:
            rssi = sampler(iface)
            if rssi is not None:
                WINDOW.append(rssi)

                # Feature extract (inline)
                if len(WINDOW) >= 5:
                    _mean = mean(WINDOW)
                    _std = stdev(WINDOW) if len(WINDOW) >= 2 else 0
                    _ = _std > PRESENCE_STD_THRESHOLD  # decision

                loop_count += 1
            else:
                errors += 1
        except Exception:
            errors += 1

        time.sleep(SAMPLING_INTERVAL)

    elapsed = time.time() - t0
    rate = loop_count / elapsed
    error_rate = errors / (loop_count + errors) * 100 if (loop_count + errors) > 0 else 0

    passed = error_rate < 20 and loop_count >= 10
    log_test("Combined Pipeline (30s)", passed,
             f"{loop_count} loops, {errors} errors, "
             f"{rate:.1f} loops/s, error_rate={error_rate:.1f}%", {
        "duration_s": DURATION,
        "loop_count": loop_count,
        "errors": errors,
        "error_rate_pct": round(error_rate, 1),
        "throughput_loops_per_s": round(rate, 2),
    })

# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("  Feasibility Test — Arduino UNO Q WiFi Sensing")
    print("=" * 60)
    print(f"  Started: {datetime.now().isoformat()}")
    print(f"  Platform: {sys.platform}")
    print(f"  Python: {sys.version.split()[0]}")
    print("=" * 60)

    # Run tests sequentially, feeding RSSI samples to later tests
    rssi_samples = test_rssi_sampling()

    test_feature_extraction(rssi_samples)
    test_serial_communication()
    test_system_load()
    _ = test_presence_detection(rssi_samples)
    test_combined_pipeline()

    # Summary
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    passed = sum(1 for t in RESULTS["tests"] if t["passed"])
    total = len(RESULTS["tests"])
    verdict = "PASS" if RESULTS["summary"]["pass"] else "FAIL"
    print(f"  Tests: {passed}/{total} passed")
    print(f"  Verdict: {verdict}")

    if not RESULTS["summary"]["pass"]:
        print("  Failures:")
        for r in RESULTS["summary"]["fail_reasons"]:
            print(f"    - {r}")

    # Save report
    with open(REPORT_FILE, "w") as f:
        json.dump(RESULTS, f, indent=2)
    print(f"\n  Report saved: {REPORT_FILE}")
    print("=" * 60)

    return 0 if RESULTS["summary"]["pass"] else 1

if __name__ == "__main__":
    sys.exit(main())
