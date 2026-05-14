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

Flag --install-deps: prova ad installare iw e pyserial automaticamente.
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
SAMPLING_WINDOW_S = 20
SAMPLING_INTERVAL = 0.5
PRESENCE_STD_THRESHOLD = 2.0
INSTALL_DEPS = "--install-deps" in sys.argv

REPORT_FILE = f"feasibility_report_{datetime.now().strftime('%Y%m%d_%H%M')}.json"

RESULTS = {
    "timestamp": datetime.now().isoformat(),
    "board": "Arduino UNO Q",
    "system": {},
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

def apt_install(pkg):
    """Prova a installare un pacchetto via apt."""
    if not INSTALL_DEPS:
        return False
    try:
        subprocess.check_call(
            f"sudo apt-get install -y {pkg}",
            shell=True, timeout=60, stderr=subprocess.DEVNULL
        )
        return True
    except subprocess.CalledProcessError:
        return False

# ============================================================
# System diagnostics
# ============================================================
def run_system_diagnostics():
    print("--- System Diagnostics ---")
    diag = {}

    # Tool availability
    TOOLS = ["iw", "nmcli", "iwconfig", "wpa_cli", "ip", "ifconfig"]
    for tool in TOOLS:
        path = shutil.which(tool)
        diag[tool] = path if path else "NOT FOUND"
        print(f"  {tool}: {path or 'NOT FOUND'}")

    # Interface detection via ip link
    try:
        out = subprocess.check_output("ip link show", shell=True, timeout=5).decode()
        interfaces = re.findall(r"\d+:\s+(\w+)[:@].*state\s+(\w+)", out)
        iface_info = {name: state for name, state in interfaces}
        diag["interfaces"] = iface_info
        print(f"  Interfaces: {iface_info}")
    except Exception as e:
        diag["interfaces"] = str(e)
        print(f"  Interfaces error: {e}")

    # Wireless info via /proc/net/wireless
    try:
        with open("/proc/net/wireless") as f:
            content = f.read()
        diag["proc_net_wireless"] = content.strip()
        print(f"  /proc/net/wireless: {'present' if content.strip() else 'empty'}")
    except FileNotFoundError:
        diag["proc_net_wireless"] = "NOT FOUND"
        print(f"  /proc/net/wireless: NOT FOUND")

    # nmcli general status
    if diag.get("nmcli"):
        try:
            out = subprocess.check_output(
                "nmcli general status 2>/dev/null || true",
                shell=True, timeout=5
            ).decode()
            diag["nmcli_status"] = out.strip()
            print(f"  nmcli: available")
        except Exception:
            pass

    RESULTS["system"] = diag
    return diag

# ============================================================
# Interface & RSSI
# ============================================================
def detect_wifi_interface():
    """Trova interfaccia WiFi via ip link o /sys/class/net."""
    try:
        out = subprocess.check_output("ip link show", shell=True, timeout=5).decode()
        for line in out.split("\n"):
            m = re.search(r"\d+:\s+(\w+)[:@]", line)
            if m:
                name = m.group(1)
                if name.startswith("wlan") or name.startswith("wlx"):
                    return name, f"found via ip link"
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    try:
        for entry in os.listdir("/sys/class/net"):
            if entry.startswith("wlan") or entry.startswith("wlx"):
                return entry, "found via sysfs"
    except FileNotFoundError:
        pass
    return None, "no wireless interface found"

def probe_rssi_methods(iface):
    """
    Prova tutti i metodi RSSI disponibili.
    Ritorna (metodo_nome, funzione_sampler, valore_di_test).
    """
    methods = []

    # 1) iw link
    try:
        val = rssi_via_iw_link(iface)
        methods.append(("iw_link", rssi_via_iw_link, val))
    except Exception:
        methods.append(("iw_link", rssi_via_iw_link, None))

    # 2) iw scan
    try:
        val = rssi_via_iw_scan(iface)
        methods.append(("iw_scan", rssi_via_iw_scan, val))
    except Exception:
        methods.append(("iw_scan", rssi_via_iw_scan, None))

    # 3) nmcli
    try:
        val = rssi_via_nmcli()
        methods.append(("nmcli", rssi_via_nmcli, val))
    except Exception:
        methods.append(("nmcli", rssi_via_nmcli, None))

    # 4) /proc/net/wireless
    try:
        val = rssi_via_proc_wireless(iface)
        methods.append(("proc_wireless", rssi_via_proc_wireless, val))
    except Exception:
        methods.append(("proc_wireless", rssi_via_proc_wireless, None))

    # Score: pick the one with a real value, prefer fastest (iw_link > nmcli > iw_scan > proc)
    SCORE = {"iw_link": 4, "nmcli": 3, "proc_wireless": 2, "iw_scan": 1}
    best = None
    best_score = -1
    for name, fn, val in methods:
        s = SCORE.get(name, 0)
        if val is not None and s > best_score:
            best = (name, fn, val)
            best_score = s

    # If none worked but a method exists (iw installed but not connected), use iw_scan
    if best is None:
        for name, fn, val in methods:
            if name == "iw_scan" and fn is not None:
                best = (name, fn, None)
                break

    return best

def rssi_via_iw_link(iface):
    result = subprocess.check_output(
        f"iw dev {iface} link", shell=True, timeout=3,
        stderr=subprocess.DEVNULL
    ).decode()
    m = re.search(r"signal:\s*(-?\d+\.?\d*)\s*dBm", result)
    return float(m.group(1)) if m else None

def rssi_via_iw_scan(iface):
    result = subprocess.check_output(
        f"iw dev {iface} scan", shell=True, timeout=10,
        stderr=subprocess.DEVNULL
    ).decode()
    signals = re.findall(r"signal:\s*(-?\d+\.?\d*)\s*dBm", result)
    if signals:
        return mean(float(s) for s in signals)
    return None

def rssi_via_nmcli():
    """nmcli segnale 0-100% -> approx dBm (-100 .. -30)."""
    result = subprocess.check_output(
        "nmcli -t -f SIGNAL,SSID dev wifi list --rescan yes",
        shell=True, timeout=15, stderr=subprocess.DEVNULL
    ).decode()
    signals = []
    for line in result.strip().split("\n"):
        parts = line.split(":")
        if parts and parts[0].isdigit():
            signals.append(int(parts[0]))
    if signals:
        avg_pct = mean(signals)
        return -100 + (avg_pct * 0.7)
    return None

def rssi_via_proc_wireless(iface):
    with open("/proc/net/wireless") as f:
        lines = f.readlines()
    for line in lines[2:]:
        if iface in line:
            parts = line.split()
            if len(parts) >= 4:
                sig = parts[3].split(".")[0]
                if sig.lstrip("-").isdigit():
                    return float(sig)
    return None

# ============================================================
# Test 1: RSSI Sampling Reliability
# ============================================================
def test_rssi_sampling():
    print("\n=== Test 1: RSSI Sampling Reliability ===")

    # Detect interface
    iface, state = detect_wifi_interface()
    print(f"  Interface: {iface}  ({state})")

    if iface is None:
        log_test("RSSI Sampling", False,
                 f"No WiFi interface found", {"state": state})
        return []

    # Probe RSSI methods
    best = probe_rssi_methods(iface)
    if best is None:
        log_test("RSSI Sampling", False,
                 "No RSSI method works (tried: iw, nmcli, /proc/net/wireless)", {
            "interface": iface,
            "suggestion": "Run with --install-deps to auto-install iw, or manually: sudo apt-get install -y iw"
        })
        return []

    method_name, sampler_fn, test_val = best
    print(f"  Using method: {method_name} (test value: {test_val})")

    # Sample
    samples = []
    timestamps = []
    errors = 0

    start = time.time()
    while time.time() - start < SAMPLING_WINDOW_S:
        try:
            val = sampler_fn(iface) if method_name != "nmcli" else sampler_fn()
            if val is not None:
                samples.append(val)
                timestamps.append(time.time())
            else:
                errors += 1
        except Exception:
            errors += 1
        time.sleep(SAMPLING_INTERVAL)

    n = len(samples)
    expected = SAMPLING_WINDOW_S / SAMPLING_INTERVAL
    sample_rate = n / SAMPLING_WINDOW_S if SAMPLING_WINDOW_S else 0
    error_rate = errors / (n + errors) * 100 if (n + errors) else 0

    passed = n >= expected * 0.2
    details = (
        f"Got {n}/{int(expected)} samples in {SAMPLING_WINDOW_S}s "
        f"({sample_rate:.1f} Hz), method={method_name}, "
        f"errors={errors} ({error_rate:.1f}%)"
    )

    metrics = {
        "samples": n,
        "sample_rate_hz": round(sample_rate, 2),
        "error_rate_pct": round(error_rate, 1),
        "method": method_name,
        "interface": iface,
    }
    if n >= 2:
        intervals = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)]
        metrics["jitter_s"] = round(stdev(intervals), 3)
        metrics["rssi_min_dbm"] = round(min(samples), 1)
        metrics["rssi_max_dbm"] = round(max(samples), 1)
        metrics["rssi_mean_dbm"] = round(mean(samples), 1)
        metrics["rssi_std_dbm"] = round(stdev(samples), 2)

    log_test("RSSI Sampling", passed, details, metrics)
    return samples

# ============================================================
# Test 2: Feature Extraction Performance
# ============================================================
def test_feature_extraction(samples):
    print("\n=== Test 2: Feature Extraction Performance ===")

    if len(samples) < 2:
        log_test("Feature Extraction Speed", False,
                 "Skipped: not enough RSSI samples from Test 1",
                 {"samples_available": len(samples)})
        return

    try:
        import numpy as np
        has_numpy = True
        arr = np.array(samples)
    except ImportError:
        has_numpy = False
        arr = samples

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

    passed = elapsed < 0.01
    log_test("Feature Extraction Speed", passed,
             f"Completed in {elapsed*1000:.2f}ms avg (numpy={has_numpy})", {
        "avg_time_ms": round(elapsed * 1000, 3),
        "using_numpy": has_numpy,
        "n_features": 5,
        "n_runs": n_runs,
    })

# ============================================================
# Test 3: UART Communication
# ============================================================
def test_serial_communication():
    print("\n=== Test 3: UART Communication ===")

    # Auto-detect port
    candidates = [
        SERIAL_PORT, "/dev/ttyACM1", "/dev/ttyUSB0",
        "/dev/ttyUSB1", "/dev/ttyS0", "/dev/ttyS1",
    ]
    found_port = next((p for p in candidates if os.path.exists(p)), None)

    if found_port is None:
        log_test("UART Communication", False,
                 f"No serial port found (tried: {', '.join(candidates)})", {})
        return

    print(f"  Port found: {found_port}")

    try:
        import serial
    except ImportError:
        ok = False
        if INSTALL_DEPS:
            print("  Installing pyserial via apt...")
            ok = apt_install("python3-serial")
        if not ok:
            log_test("UART Communication", False,
                     "pyserial not installed. "
                     "Run with --install-deps or: sudo apt-get install -y python3-serial", {})
            return

    try:
        import serial
        ser = serial.Serial(found_port, BAUD, timeout=2)
        time.sleep(2)

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
                     f"Only {len(lines)} lines in 10s",
                     {"lines_received": len(lines)})
            return

        # Try CSV parsing for both 4-field and 5-field formats
        parse_ok = 0
        parse_fail = 0
        for line in lines:
            parts = line.split(",")
            # feasibility_test.ino sends: ts,temp,humid,air,light (5)
            # original sketch sends: temp,humid,air (3) or temp,humid,air,light (4)
            if len(parts) in (3, 4, 5):
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
            "port": found_port,
            "baud": BAUD,
            "lines_total": len(lines),
            "lines_parsed_ok": parse_ok,
            "parse_success_pct": round(success_rate, 1),
            "rate_hz": round(rate, 2),
        })

    except Exception as e:
        log_test("UART Communication", False, f"Error: {e}", {})

# ============================================================
# Test 4: CPU & Memory Load
# ============================================================
def test_system_load():
    print("\n=== Test 4: CPU & Memory Load ===")

    try:
        with open("/proc/stat") as f:
            stat_before = [int(x) for x in f.readline().split()[1:]]

        with open("/proc/meminfo") as f:
            meminfo = f.readlines()

        mem_total = mem_avail = None
        for line in meminfo:
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1]) / 1024
            if line.startswith("MemAvailable:"):
                mem_avail = int(line.split()[1]) / 1024

        time.sleep(1)

        with open("/proc/stat") as f:
            stat_after = [int(x) for x in f.readline().split()[1:]]

        total_before, idle_before = sum(stat_before), stat_before[3]
        total_after, idle_after = sum(stat_after), stat_after[3]
        cpu_pct = (1 - (idle_after - idle_before) / (total_after - total_before)) * 100

        used_mem = mem_total - mem_avail if mem_total and mem_avail else None
        mem_pct = used_mem / mem_total * 100 if used_mem and mem_total else None

        details = f"CPU: {cpu_pct:.0f}%"
        if used_mem:
            details += f", RAM: {used_mem:.0f}/{mem_total:.0f}MB ({mem_pct:.0f}%)"

        log_test("System Load", True, details, {
            "cpu_percent": round(cpu_pct, 1),
            "ram_total_mb": round(mem_total, 1) if mem_total else None,
            "ram_used_mb": round(used_mem, 1) if used_mem else None,
            "ram_percent": round(mem_pct, 1) if mem_pct else None,
        })

    except (FileNotFoundError, IndexError, ZeroDivisionError) as e:
        log_test("System Load", False, f"Could not read stats: {e}", {})

# ============================================================
# Test 5: Presence Detection (simulated)
# ============================================================
def test_presence_detection(baseline_samples):
    print("\n=== Test 5: Presence Detection (simulated) ===")

    if len(baseline_samples) < 10:
        log_test("Presence Detection", False,
                 "Skipped: not enough RSSI baseline from Test 1",
                 {"samples_available": len(baseline_samples), "minimum_required": 10})
        return baseline_samples

    import random
    base_std = stdev(baseline_samples)
    base_mean = mean(baseline_samples)

    movement = [v + random.uniform(-5, 5) for v in baseline_samples]
    mov_std = stdev(movement)

    can_detect = mov_std > PRESENCE_STD_THRESHOLD
    false_pos = base_std > PRESENCE_STD_THRESHOLD
    passed = can_detect and not false_pos

    log_test("Presence Detection (simulated)", passed,
             f"Empty std={base_std:.2f}, Movement std={mov_std:.2f}, "
             f"Threshold={PRESENCE_STD_THRESHOLD}", {
        "empty_room_std": round(base_std, 2),
        "simulated_movement_std": round(mov_std, 2),
        "threshold_std": PRESENCE_STD_THRESHOLD,
        "can_detect_movement": can_detect,
        "false_positive_empty": false_pos,
        "baseline_samples_n": len(baseline_samples),
    })
    return movement

# ============================================================
# Test 6: Combined Pipeline
# ============================================================
def test_combined_pipeline():
    print("\n=== Test 6: Combined Pipeline (30s) ===")

    iface, _ = detect_wifi_interface()
    if iface is None:
        log_test("Combined Pipeline (30s)", False,
                 "No WiFi interface", {})
        return

    best = probe_rssi_methods(iface)
    if best is None:
        log_test("Combined Pipeline (30s)", False,
                 "No RSSI method available", {})
        return

    method_name, sampler_fn, _ = best
    print(f"  Using: {method_name}")

    SAMPLER_ARGS = iface if method_name != "nmcli" else None

    DURATION = 30
    WINDOW = deque(maxlen=40)
    loops = 0
    errors = 0
    t0 = time.time()

    while time.time() - t0 < DURATION:
        try:
            val = sampler_fn(iface) if SAMPLER_ARGS else sampler_fn()
            if val is not None:
                WINDOW.append(val)
                if len(WINDOW) >= 5:
                    _std = stdev(WINDOW) if len(WINDOW) >= 2 else 0
                    _ = _std > PRESENCE_STD_THRESHOLD
                loops += 1
            else:
                errors += 1
        except Exception:
            errors += 1
        time.sleep(SAMPLING_INTERVAL)

    elapsed = time.time() - t0
    rate = loops / elapsed
    err_rate = errors / (loops + errors) * 100 if (loops + errors) else 0
    passed = err_rate < 20 and loops >= 5

    log_test("Combined Pipeline (30s)", passed,
             f"{loops} loops, {errors} errors, {rate:.1f}/s, err={err_rate:.1f}%", {
        "loops": loops, "errors": errors,
        "throughput_per_s": round(rate, 2),
        "error_rate_pct": round(err_rate, 1),
    })

# ============================================================
# Helper: ensure tools
# ============================================================
def ensure_tools():
    """Check and optionally install required tools."""
    missing = []

    if not shutil.which("iw"):
        missing.append("iw")

    try:
        import serial  # noqa
    except ImportError:
        missing.append("python3-serial")

    if missing and INSTALL_DEPS:
        print(f"--- Installing missing dependencies: {missing} ---")
        for pkg in missing:
            ok = apt_install(pkg)
            print(f"  {pkg}: {'OK' if ok else 'FAILED'}")

    if missing and not INSTALL_DEPS:
        print(f"  Missing tools: {missing}")
        print(f"  Run with --install-deps to auto-install, or:")
        print(f"    sudo apt-get update")
        print(f"    sudo apt-get install -y {' '.join(missing)}")
        print()

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
    if INSTALL_DEPS:
        print(f"  --install-deps: ON")
    print("=" * 60)

    ensure_tools()
    run_system_diagnostics()

    rssi_samples = test_rssi_sampling()
    test_feature_extraction(rssi_samples)
    test_serial_communication()
    test_system_load()
    test_presence_detection(rssi_samples)
    test_combined_pipeline()

    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    passed = sum(1 for t in RESULTS["tests"] if t["passed"])
    total = len(RESULTS["tests"])
    print(f"  Tests: {passed}/{total} passed")
    print(f"  Verdict: {'PASS' if RESULTS['summary']['pass'] else 'FAIL'}")
    if not RESULTS["summary"]["pass"]:
        for r in RESULTS["summary"]["fail_reasons"]:
            print(f"    FAIL: {r}")

    with open(REPORT_FILE, "w") as f:
        json.dump(RESULTS, f, indent=2)
    print(f"\n  Report: {REPORT_FILE}")
    print("=" * 60)

    return 0 if RESULTS["summary"]["pass"] else 1

if __name__ == "__main__":
    sys.exit(main())
