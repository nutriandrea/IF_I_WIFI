#!/usr/bin/env python3
"""
Feasibility Test — Arduino UNO Q WiFi Sensing

Valuta se l'Arduino UNO Q e in grado di:
  1. Campionare RSSI WiFi (via iw, nmcli, o /proc/net/wireless)
  2. Eseguire feature extraction in real-time
  3. Comunicare via UART con lo sketch MCU
  4. Mantenere CPU/memoria sotto controllo
  5. Rilevare presenza con accuratezza accettabile

Output: feasibility_report_<timestamp>.json + summary su stdout

Flags:
  --install-deps   Prova ad installare iw e pyserial automaticamente
"""

import subprocess, time, json, sys, os, re, shutil, argparse, socket, struct
from datetime import datetime
from collections import deque
from statistics import mean, stdev

# ============================================================
# Config
# ============================================================
SERIAL_BAUD = 9600
SAMPLING_WINDOW_S = 20
SAMPLING_INTERVAL = 0.5
PRESENCE_STD_THRESHOLD = 2.0
REPORT_FILE = f"feasibility_report_{datetime.now().strftime('%Y%m%d_%H%M')}.json"

RESULTS = {
    "timestamp": datetime.now().isoformat(),
    "board": "Arduino UNO Q",
    "tests": [],
    "system_info": {},
    "summary": {"pass": True, "fail_reasons": []}
}

def log_test(name, passed, details, metrics=None):
    entry = {"test": name, "passed": passed, "details": details, "metrics": metrics or {}}
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
# System & tool detection
# ============================================================
def _find_tool(name):
    """Cerca tool nel PATH e in /usr/sbin/ (dove apt installa su UNO Q)."""
    path = shutil.which(name)
    if path:
        return path
    # Alcuni tool (iw) finiscono in /usr/sbin/ non sempre nel PATH
    for prefix in ["/usr/sbin", "/sbin", "/usr/local/sbin"]:
        p = os.path.join(prefix, name)
        if os.path.exists(p):
            return p
    return None

def detect_tools():
    """Scopre quali tool WiFi e seriali sono installati."""
    tools = {}
    for cmd in ["iw", "nmcli", "ip", "ifconfig", "wpa_cli", "iwconfig"]:
        tools[cmd] = _find_tool(cmd)
    try:
        import serial
        tools["pyserial"] = True
    except ImportError:
        tools["pyserial"] = False
    return tools


def detect_interfaces():
    """Trova interfacce WiFi via ip link o /sys/class/net."""
    ifaces = {"wifi": [], "all": []}
    try:
        out = subprocess.check_output("ip link show", shell=True,
                                       timeout=5, stderr=subprocess.DEVNULL).decode()
        for m in re.finditer(r"^\d+:\s+(\S+):", out, re.MULTILINE):
            name = m.group(1).strip(":")
            ifaces["all"].append(name)
            if re.match(r"^(wlan|wlx|wlp)", name):
                ifaces["wifi"].append(name)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    try:
        for entry in os.listdir("/sys/class/net"):
            if re.match(r"^(wlan|wlx)", entry) and entry not in ifaces["wifi"]:
                ifaces["wifi"].append(entry)
                ifaces["all"].append(entry)
    except FileNotFoundError:
        pass
    return ifaces


def detect_serial_ports():
    """Trova porte seriali disponibili."""
    candidates = ["/dev/ttyACM0", "/dev/ttyACM1",
                  "/dev/ttyUSB0", "/dev/ttyUSB1",
                  "/dev/ttyS0", "/dev/ttyS1",
                  "/dev/ttyGS0", "/dev/ttyGS1"]  # USB gadget serial
    found = [p for p in candidates if os.path.exists(p)]
    # Also scan /dev/serial/ if it exists
    serial_dir = "/dev/serial/by-id"
    if os.path.isdir(serial_dir):
        for entry in sorted(os.listdir(serial_dir)):
            full = os.path.join(serial_dir, entry)
            real = os.path.realpath(full)
            if real not in found:
                found.append(real)
    return found


# ============================================================
# RSSI sampler — multiple backends
# ============================================================
class RSSISampler:
    """Prova vari metodi per leggere RSSI, sceglie il primo funzionante."""

    def __init__(self, tools, interfaces):
        self.iface = interfaces["wifi"][0] if interfaces["wifi"] else None
        self.method = None
        self.sampler_fn = None
        self.tools = tools

    def probe(self):
        """Trova il miglior metodo disponibile per RSSI."""
        # 1) iw dev link (veloce, richiede connessione)
        if self.tools.get("iw") and self.iface:
            try:
                val = self._rssi_iw_link()
                if val is not None:
                    self.method = "iw link"
                    self.sampler_fn = self._rssi_iw_link
                    return True
            except Exception:
                pass

        # 2) nmcli (funziona senza iw)
        if self.tools.get("nmcli"):
            try:
                val = self._rssi_nmcli()
                if val is not None:
                    self.method = "nmcli"
                    self.sampler_fn = self._rssi_nmcli
                    return True
            except Exception:
                pass

        # 3) /proc/net/wireless (nessun tool)
        if self.iface:
            try:
                val = self._rssi_proc_wireless()
                if val is not None:
                    self.method = "/proc/net/wireless"
                    self.sampler_fn = self._rssi_proc_wireless
                    return True
            except Exception:
                pass

        # 4) iw station dump
        if self.tools.get("iw") and self.iface:
            try:
                val = self._rssi_iw_station()
                if val is not None:
                    self.method = "iw station"
                    self.sampler_fn = self._rssi_iw_station
                    return True
            except Exception:
                pass

        # 5) iw scan (lento, non serve connessione)
        if self.tools.get("iw") and self.iface:
            try:
                val = self._rssi_iw_scan()
                if val is not None:
                    self.method = "iw scan"
                    self.sampler_fn = self._rssi_iw_scan
                    return True
            except Exception:
                pass

        return False

    @property
    def _iw(self):
        """Percorso del binario iw (o None)."""
        return self.tools.get("iw")

    def _rssi_iw_link(self):
        if not self._iw:
            return None
        r = subprocess.check_output(f"{self._iw} dev {self.iface} link", shell=True,
                                     timeout=3, stderr=subprocess.DEVNULL).decode()
        m = re.search(r"signal:\s*(-?\d+\.?\d*)\s*dBm", r)
        return float(m.group(1)) if m else None

    def _rssi_nmcli(self):
        """Segnale da 'nmcli device show' (dati cache, istantaneo).
        Signal e 0-100%, convertiamo in approx dBm."""
        if not self.iface:
            return None
        r = subprocess.check_output(
            f"nmcli -g GENERAL.SIGNAL device show {self.iface}",
            shell=True, timeout=5, stderr=subprocess.DEVNULL
        ).decode().strip()
        if r and r.isdigit():
            pct = float(r)
            return -30 + (pct - 100) * 0.6
        return None

    def _rssi_proc_wireless(self):
        with open("/proc/net/wireless") as f:
            for line in f.readlines()[2:]:
                parts = line.split(":")
                if parts and parts[0].strip() == self.iface:
                    stats = parts[1].strip().split()
                    if len(stats) >= 3:
                        level = stats[2].lstrip(".")
                        if level and level != "0":
                            return -float(level)
        return None

    def _rssi_iw_station(self):
        if not self._iw:
            return None
        r = subprocess.check_output(f"{self._iw} dev {self.iface} station dump",
                                     shell=True, timeout=3,
                                     stderr=subprocess.DEVNULL).decode()
        m = re.search(r"signal:\s*(-?\d+)", r)
        return float(m.group(1)) if m else None

    def _rssi_iw_scan(self):
        if not self._iw:
            return None
        r = subprocess.check_output(f"{self._iw} dev {self.iface} scan",
                                     shell=True, timeout=10,
                                     stderr=subprocess.DEVNULL).decode()
        signals = re.findall(r"signal:\s*(-?\d+\.?\d*)\s*dBm", r)
        return mean(float(s) for s in signals) if signals else None

    def sample(self):
        if self.sampler_fn:
            return self.sampler_fn()
        return None


# ============================================================
# Test 1: RSSI Sampling Reliability
# ============================================================
def test_rssi_sampling(tools, interfaces):
    print("\n=== Test 1: RSSI Sampling Reliability ===")
    print(f"  Interfaces: {interfaces}")
    print(f"  Tools: iw={tools.get('iw')} nmcli={tools.get('nmcli')}")

    sampler = RSSISampler(tools, interfaces)
    if not interfaces["wifi"]:
        log_test("RSSI Sampling", False, "No WiFi interfaces found",
                 {"interfaces_found": interfaces})
        return [], sampler

    ok = sampler.probe()
    if not ok:
        log_test("RSSI Sampling", False,
                 f"No RSSI method works (iface={sampler.iface})",
                 {"interface": sampler.iface, "tools": tools})
        return [], sampler

    print(f"  Using: {sampler.method} on {sampler.iface}")
    test_val = sampler.sample()
    if test_val is not None:
        print(f"  Test sample: {test_val:.1f} dBm")

    samples, timestamps, errors = [], [], 0
    start = time.time()
    while time.time() - start < SAMPLING_WINDOW_S:
        try:
            val = sampler.sample()
            if val is not None and -120 <= val <= 0:
                samples.append(val)
                timestamps.append(time.time())
            else:
                errors += 1
        except Exception:
            errors += 1
        time.sleep(SAMPLING_INTERVAL)

    n, expected = len(samples), SAMPLING_WINDOW_S / SAMPLING_INTERVAL
    sample_rate = n / SAMPLING_WINDOW_S if SAMPLING_WINDOW_S else 0
    err_rate = errors / (n + errors) * 100 if (n + errors) else 0
    passed = n >= expected * 0.2

    metrics = {
        "samples": n, "sample_rate_hz": round(sample_rate, 2),
        "error_rate_pct": round(err_rate, 1),
        "method": sampler.method, "interface": sampler.iface,
    }
    if n >= 2:
        intervals = [timestamps[i+1]-timestamps[i] for i in range(len(timestamps)-1)]
        metrics["jitter_s"] = round(stdev(intervals), 3)
        metrics["rssi_min_dbm"] = round(min(samples), 1)
        metrics["rssi_max_dbm"] = round(max(samples), 1)
        metrics["rssi_mean_dbm"] = round(mean(samples), 1)
        metrics["rssi_std_dbm"] = round(stdev(samples), 2)

    details = (f"Got {n}/{int(expected)} samples ({sample_rate:.1f} Hz), "
               f"method={sampler.method}, errors={errors} ({err_rate:.1f}%)")
    log_test("RSSI Sampling", passed, details, metrics)
    return samples, sampler


# ============================================================
# Test 2: Feature Extraction Performance
# ============================================================
def test_feature_extraction(samples):
    print("\n=== Test 2: Feature Extraction Performance ===")
    if len(samples) < 2:
        log_test("Feature Extraction Speed", False,
                 "Skipped: not enough RSSI samples",
                 {"samples_available": len(samples)})
        return

    try:
        import numpy as np
        arr = np.array(samples)
        has_numpy = True
    except ImportError:
        arr = samples
        has_numpy = False

    n_runs = 100
    t0 = time.perf_counter()
    for _ in range(n_runs):
        if has_numpy:
            _ = {"mean": float(np.mean(arr)), "std": float(np.std(arr)),
                 "delta": float(np.max(arr)-np.min(arr)), "var": float(np.var(arr))}
        else:
            _ = {"mean": mean(arr), "std": stdev(arr) if len(arr)>=2 else 0,
                 "delta": max(arr)-min(arr), "var": stdev(arr)**2 if len(arr)>=2 else 0}
    elapsed = (time.perf_counter() - t0) / n_runs

    passed = elapsed < 0.01
    log_test("Feature Extraction Speed", passed,
             f"{elapsed*1000:.2f}ms avg (numpy={has_numpy})",
             {"avg_time_ms": round(elapsed*1000, 3), "using_numpy": has_numpy,
              "n_features": 4, "n_runs": n_runs})


# ============================================================
# Test 3: UART Communication
# ============================================================
def test_serial_communication():
    print("\n=== Test 3: UART Communication ===")
    ports = detect_serial_ports()
    if not ports:
        log_test("UART Communication", False, "No serial ports found", {})
        return

    print(f"  Ports found: {ports}")
    try:
        import serial
    except ImportError:
        log_test("UART Communication", False,
                 "pyserial not installed (sudo apt-get install -y python3-serial)", {})
        return

    # Check user permissions
    try:
        import grp, pwd
        user = pwd.getpwuid(os.getuid()).pw_name
        dialout = [g.gr_name for g in grp.getgrall() if user in g.gr_mem]
        print(f"  User: {user}, groups with user: {dialout}")
    except ImportError:
        pass

    # Try each port
    for port in ports:
        print(f"  Trying: {port} ...")
        # Check permissions
        try:
            st = os.stat(port)
            print(f"    permissions: {oct(st.st_mode)[-3:]}, "
                  f"owner: {st.st_uid}, group: {st.st_gid}")
        except OSError:
            pass

        try:
            ser = serial.Serial(port, SERIAL_BAUD, timeout=2)
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
                         f"Only {len(lines)} lines on {port} in 10s",
                         {"port": port, "lines_received": len(lines),
                          "hint": "Is the MCU sketch uploaded? (feasibility_test.ino)"})
                continue

            parse_ok = sum(1 for l in lines if len(l.split(",")) in (4, 5))
            rate = len(lines) / 10
            success = parse_ok / len(lines) * 100
            passed = parse_ok >= 3 and success >= 50
            log_test("UART Communication", passed,
                     f"{len(lines)} lines on {port} ({rate:.1f}/s), "
                     f"{parse_ok} OK ({success:.0f}%)",
                     {"port": port, "lines": len(lines), "parsed_ok": parse_ok,
                      "rate_hz": round(rate, 2), "success_pct": round(success, 1)})
            return  # success on first working port

        except serial.SerialException as e:
            err = str(e)
            if "Permission" in err or "denied" in err:
                print(f"    Permission denied — try: sudo usermod -a -G dialout $USER")
            elif "Input/output error" in err:
                print(f"    I/O error — {port} exists but can't configure (wrong port?)")
            elif "device" in err.lower():
                print(f"    Device error")
            else:
                print(f"    Error: {e}")
            continue

    log_test("UART Communication", False,
             f"None of {ports} worked — upload feasibility_test.ino via Arduino IDE")


# ============================================================
# Test 4: CPU & Memory Load
# ============================================================
def test_system_load():
    print("\n=== Test 4: CPU & Memory Load ===")
    try:
        with open("/proc/stat") as f:
            cpu_before = [int(x) for x in f.readline().split()[1:]]
        with open("/proc/meminfo") as f:
            mem = f.readlines()
        mem_total = mem_avail = None
        for line in mem:
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1]) / 1024
            if line.startswith("MemAvailable:"):
                mem_avail = int(line.split()[1]) / 1024
        time.sleep(1)
        with open("/proc/stat") as f:
            cpu_after = [int(x) for x in f.readline().split()[1:]]

        td = sum(cpu_after) - sum(cpu_before)
        id_ = cpu_after[3] - cpu_before[3]
        cpu_pct = (1 - id_/td) * 100 if td > 0 else 0
        used = mem_total - mem_avail if mem_total and mem_avail else None
        mem_pct = used/mem_total*100 if used and mem_total else None
        details = f"CPU: {cpu_pct:.0f}%"
        if used is not None:
            details += f", RAM: {used:.0f}/{mem_total:.0f}MB ({mem_pct:.0f}%)"
        log_test("System Load", True, details,
                 {"cpu_percent": round(cpu_pct, 1),
                  "ram_total_mb": round(mem_total, 1) if mem_total else None,
                  "ram_used_mb": round(used, 1) if used else None,
                  "ram_percent": round(mem_pct, 1) if mem_pct else None})
    except Exception as e:
        log_test("System Load", False, f"Cannot read stats: {e}", {})


# ============================================================
# Test 5: Presence Detection (simulated)
# ============================================================
def test_presence_detection(baseline_samples):
    print("\n=== Test 5: Presence Detection (simulated) ===")
    if len(baseline_samples) < 10:
        log_test("Presence Detection", False,
                 "Skipped: not enough RSSI baseline",
                 {"available": len(baseline_samples), "needed": 10})
        return

    import random
    base_std = stdev(baseline_samples)
    movement = [v + random.uniform(-5, 5) for v in baseline_samples]
    mov_std = stdev(movement)

    detect = mov_std > PRESENCE_STD_THRESHOLD
    false_pos = base_std > PRESENCE_STD_THRESHOLD
    passed = detect and not false_pos

    log_test("Presence Detection (simulated)", passed,
             f"Empty std={base_std:.2f}, Movement std={mov_std:.2f}",
             {"empty_std": round(base_std, 2), "movement_std": round(mov_std, 2),
              "threshold": PRESENCE_STD_THRESHOLD, "can_detect": detect,
              "false_positive": false_pos})


# ============================================================
# Test 6: Combined Pipeline Stress Test
# ============================================================
def test_combined_pipeline(sampler):
    print("\n=== Test 6: Combined Pipeline (30s) ===")
    if sampler is None or sampler.sampler_fn is None:
        log_test("Combined Pipeline (30s)", False, "No RSSI sampler", {})
        return

    window = deque(maxlen=40)
    loops = errors = 0
    t0 = time.time()
    while time.time() - t0 < 30:
        try:
            rssi = sampler.sample()
            if rssi is not None and -120 <= rssi <= 0:
                window.append(rssi)
                if len(window) >= 5:
                    _ = stdev(window) if len(window) >= 2 else 0 > PRESENCE_STD_THRESHOLD
                loops += 1
            else:
                errors += 1
        except Exception:
            errors += 1
        time.sleep(SAMPLING_INTERVAL)

    rate = loops / (time.time() - t0)
    err_r = errors/(loops+errors)*100 if (loops+errors) else 0
    passed = err_r < 20 and loops >= 5
    log_test("Combined Pipeline (30s)", passed,
             f"{loops} loops, {errors} errors, {rate:.1f}/s, err={err_r:.1f}%",
             {"loops": loops, "errors": errors,
              "throughput": round(rate, 2), "error_rate": round(err_r, 1)})


# ============================================================
# Dependency installer
# ============================================================
def install_deps(tools):
    print("\n--- Installing missing dependencies ---")
    installed, failed = [], []

    if not _find_tool("iw"):
        print("  Installing iw...")
        rc = subprocess.call("sudo apt-get update -qq && sudo apt-get install -y -qq iw",
                             shell=True, timeout=60)
        (installed if rc == 0 else failed).append("iw")

    if not tools.get("pyserial"):
        print("  Installing python3-serial...")
        rc = subprocess.call("sudo apt-get install -y -qq python3-serial",
                             shell=True, timeout=30)
        if rc == 0:
            installed.append("pyserial")
        else:
            failed.append("pyserial")
            rc = subprocess.call("pip3 install pyserial", shell=True, timeout=30)
            if rc == 0:
                installed.append("pyserial")

    if installed:
        print(f"  Installed: {', '.join(installed)}")
    if failed:
        print(f"  Failed: {', '.join(failed)}")


# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="UNO Q Feasibility Test")
    parser.add_argument("--install-deps", action="store_true",
                        help="Install missing deps (iw, pyserial)")
    args = parser.parse_args()

    print("=" * 60)
    print("  Feasibility Test — Arduino UNO Q WiFi Sensing")
    print("=" * 60)
    print(f"  Started: {datetime.now().isoformat()}")
    print(f"  Platform: {sys.platform}")
    print(f"  Python: {sys.version.split()[0]}")
    if args.install_deps:
        print("  --install-deps: ON")
    print("=" * 60)

    # System detection
    print("\n--- System Detection ---")
    tools = detect_tools()
    interfaces = detect_interfaces()
    serial_ports = detect_serial_ports()
    RESULTS["system_info"] = {"tools": {k: bool(v) for k, v in tools.items()},
                               "interfaces": interfaces, "serial_ports": serial_ports}
    print(f"  Tools: {json.dumps({k: bool(v) for k, v in tools.items()})}")
    print(f"  WiFi interfaces: {interfaces['wifi']}")
    print(f"  Serial ports: {serial_ports}")

    if args.install_deps:
        install_deps(tools)
        tools = detect_tools()
        RESULTS["system_info"]["tools"] = {k: bool(v) for k, v in tools.items()}

    # Run tests
    rssi_samples, sampler = test_rssi_sampling(tools, interfaces)
    test_feature_extraction(rssi_samples)
    test_serial_communication()
    test_system_load()
    test_presence_detection(rssi_samples)
    test_combined_pipeline(sampler)

    # Summary
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    passed = sum(1 for t in RESULTS["tests"] if t["passed"])
    total = len(RESULTS["tests"])
    verdict = "PASS" if RESULTS["summary"]["pass"] else "FAIL"
    print(f"  Tests: {passed}/{total} passed")
    print(f"  Verdict: {verdict}")
    if RESULTS["summary"]["fail_reasons"]:
        for r in RESULTS["summary"]["fail_reasons"]:
            print(f"    FAIL: {r}")

    with open(REPORT_FILE, "w") as f:
        json.dump(RESULTS, f, indent=2)
    print(f"\n  Report: {REPORT_FILE}")

    if not rssi_samples:
        print("\n  >>> For RSSI: nmcli works! Re-run the test.")
        if not tools.get("iw"):
            print("  >>> Install iw:  sudo apt-get install iw  (then open new shell)")
    print("=" * 60)
    return 0 if RESULTS["summary"]["pass"] else 1

if __name__ == "__main__":
    sys.exit(main())
