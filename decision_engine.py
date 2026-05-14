#!/usr/bin/env python3
"""
decision_engine.py — Main orchestrator for presence detection on Arduino UNO Q.

Combines:
  - GradientDetector: RSSI gradient + consecutive same-sign (from enhanced_presence.py)
  - MonitorDetector: 802.11 frame capture via monitor mode (from monitor_presence.py)
  - BridgeClient: STM32 sensor readings via Unix domain socket RPC
  - Actuator: relay / LED output
  - FusionEngine: weighted decision from all available inputs

Usage:
  # Quick start (auto-detect everything)
  python3 decision_engine.py

  # Dry-run mode (log only, no actuation)
  python3 decision_engine.py --dry-run

  # Manual control
  python3 decision_engine.py --set-relay on
  python3 decision_engine.py --set-relay off

  # Full daemon with config
  python3 decision_engine.py --config /path/to/config.json

  # Monitor mode integrated
  python3 decision_engine.py --monitor-iface mon0

Config file (JSON):
  {
    "wifi_iface": "wlan0",
    "monitor_iface": "mon0",
    "use_bridge": true,
    "bridge_socket": "/var/run/arduino-router.sock",
    "grad_threshold": 1.0,
    "consecutive_threshold": 3,
    "monitor_score_weight": 0.3,
    "gradient_score_weight": 0.5,
    "sensor_score_weight": 0.2,
    "presence_threshold": 0.5,
    "sample_interval": 0.05,
    "window_size": 20,
    "log_file": "/var/log/presence.csv",
    "actuate": true,
    "relay_gpio": "LED_BUILTIN",
    "cooldown_seconds": 10
  }
"""

import subprocess, time, json, sys, os, re, socket, struct, select, argparse
from datetime import datetime
from collections import deque
from statistics import mean, stdev
import signal

# ============================================================
# Constants
# ============================================================
DEFAULT_CONFIG = {
    "wifi_iface": "wlan0",
    "monitor_iface": "mon0",
    "use_bridge": True,
    "bridge_socket": "/var/run/arduino-router.sock",
    "grad_threshold": 1.0,
    "consecutive_threshold": 3,
    "monitor_score_weight": 0.3,
    "gradient_score_weight": 0.5,
    "sensor_score_weight": 0.2,
    "presence_threshold": 0.5,
    "sample_interval": 0.05,
    "window_size": 20,
    "log_file": "",
    "actuate": True,
    "relay_gpio": "LED_BUILTIN",
    "cooldown_seconds": 10,
    "use_monitor": True,
}

IW = "/usr/sbin/iw"


# ============================================================
# WiFi Metrics (da enhanced_presence.py)
# ============================================================

def detect_wifi_iface() -> str | None:
    """Auto-detect WiFi interface."""
    try:
        out = subprocess.check_output("ip link show", shell=True,
                                       timeout=5, stderr=subprocess.DEVNULL).decode()
        for m in re.finditer(r"^\d+:\s+(\S+):", out, re.MULTILINE):
            name = m.group(1).strip(":")
            if re.match(r"^(wlan|wlx|wlp)", name):
                return name
    except Exception:
        pass
    try:
        for entry in sorted(os.listdir("/sys/class/net")):
            if re.match(r"^(wlan|wlx)", entry):
                return entry
    except FileNotFoundError:
        pass
    return None


def get_wifi_metrics(iface: str) -> dict:
    """Collect RSSI and station dump metrics via iw."""
    m = {}
    try:
        out = subprocess.check_output(
            f"{IW} dev {iface} link", shell=True, timeout=2
        ).decode()
        ms = re.search(r"signal:\s*(-?\d+)", out)
        if ms: m["rssi"] = int(ms.group(1))
        ms = re.search(r"tx bitrate:\s*([\d.]+)", out)
        if ms: m["tx_rate"] = float(ms.group(1))
        ms = re.search(r"rx bitrate:\s*([\d.]+)", out)
        if ms: m["rx_rate"] = float(ms.group(1))
    except Exception:
        pass

    # station dump (signal_avg)
    try:
        out = subprocess.check_output(
            f"{IW} dev {iface} station dump", shell=True, timeout=2
        ).decode()
        ms = re.search(r"signal avg:\s*(-?\d+)", out)
        if ms: m["signal_avg"] = int(ms.group(1))
        ms = re.search(r"inactive time:\s*(\d+)", out)
        if ms: m["inactive_time"] = int(ms.group(1))
        ms = re.search(r"tx retries:\s*(\d+)", out)
        if ms: m["tx_retries"] = int(ms.group(1))
    except Exception:
        pass

    return m


def detect_gateway() -> str | None:
    """Find default gateway."""
    try:
        out = subprocess.check_output(
            "ip route | grep default", shell=True, timeout=3
        ).decode()
        m = re.search(r"default via (\S+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def get_ping_metrics(gw: str = None, _cache: dict = {}) -> dict:
    """Quick ping (3 packets) for latency + jitter. Cached 1s."""
    now = time.time()
    if _cache.get("_ts") and now - _cache["_ts"] < 1.0:
        return {k: v for k, v in _cache.items() if k != "_ts"}

    g = gw or detect_gateway()
    if not g:
        return {}

    try:
        out = subprocess.check_output(
            f"ping -c 3 -W 1 {g}", shell=True, timeout=3
        ).decode()
        d = {}
        m = re.search(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/[\d.]+/([\d.]+)", out)
        if m:
            d["ping_avg"] = float(m.group(1))
            d["ping_mdev"] = float(m.group(2))
        else:
            times = re.findall(r"time=([\d.]+)", out)
            if times:
                t = [float(x) for x in times]
                d["ping_avg"] = mean(t)
                d["ping_mdev"] = stdev(t) if len(t) > 1 else 0
        _cache.update(d)
        _cache["_ts"] = now
        return d
    except Exception:
        return {}


# ============================================================
# GradientDetector (da enhanced_presence.py)
# ============================================================

class GradientDetector:
    """
    Rileva presenza tramite:
      - RSSI gradient (derivata prima)
      - Consecutive same-sign gradient
      - Ping jitter (opzionale)

    Calibrazione automatica dopo 15 gradienti.
    """

    def __init__(self, window_size: int = 20, grad_threshold: float = 1.0,
                 consecutive_threshold: int = 3):
        self.rssi_hist = deque(maxlen=window_size)
        self.grad_hist = deque(maxlen=window_size)
        self.ping_hist = deque(maxlen=window_size)
        self.grad_threshold = grad_threshold
        self.consecutive_threshold = consecutive_threshold
        self.baseline_grad_mean = None
        self.baseline_grad_std = None
        self.calibrated = False
        self._t0 = 0

    def update(self, metrics: dict) -> tuple[bool, dict]:
        """Feed metrics, return (presence, info)."""
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
            info["ping_mdev"] = pm

        # Auto-calibration after 15 gradients
        if not self.calibrated and len(self.grad_hist) >= 15:
            grads = list(self.grad_hist)
            self.baseline_grad_mean = mean(grads)
            self.baseline_grad_std = stdev(grads) if len(grads) >= 2 else 0.5
            self.calibrated = True
            info["calibrated"] = True

        # Presence decision
        presence = False
        score = 0.0
        reasons = []

        if self.calibrated:
            # 1. Gradient magnitude (recent 5)
            recent_grads = list(self.grad_hist)[-5:] if len(self.grad_hist) >= 5 else list(self.grad_hist)
            if recent_grads:
                max_abs_grad = max(abs(g) for g in recent_grads)
                gs = (max_abs_grad - self.baseline_grad_mean) / max(self.baseline_grad_std, 0.1)
                if gs > self.grad_threshold:
                    score += gs
                    reasons.append(f"grad={max_abs_grad:.1f}")

            # 2. Consecutive same-sign
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
                    if cons / self.consecutive_threshold >= 0.8:
                        score += (cons / self.consecutive_threshold) * 2
                        reasons.append(f"cons={cons}")

            # 3. Ping jitter
            if len(self.ping_hist) >= 3:
                recent_ping = list(self.ping_hist)[-3:]
                jitter = stdev(recent_ping) if len(recent_ping) > 1 else 0
                if jitter > 5:
                    score += 1
                    reasons.append(f"jitter={jitter:.1f}ms")

            presence = score > 1.5

        info["score"] = round(score, 2)
        info["presence"] = presence
        info["reasons"] = reasons
        info["calibrated"] = self.calibrated
        return presence, info


# ============================================================
# MonitorDetector (da monitor_presence.py — inline minimale)
# ============================================================

def interface_exists(name: str) -> bool:
    return os.path.exists(f"/sys/class/net/{name}")


class MonitorDetector:
    """
    Cattura frame 802.11 su interfaccia monitor.
    Ritorna device_count, probe_count, frame_rate, presenza.

    Fallback graceful: se mon0 non esiste, report vuoto.
    """

    RADIOTAP_FIELD_SIZES = {
        0: 8, 1: 1, 2: 1, 3: 4, 4: 2, 5: 1, 6: 1,
        7: 2, 8: 2, 9: 2, 10: 1, 11: 1, 12: 1, 13: 2,
        14: 2, 15: 1, 16: 2, 17: 2,
    }

    def __init__(self, iface: str = "mon0", window: float = 2.0):
        self.iface = iface
        self.window = window
        self.sock = None

    @staticmethod
    def _parse_radiotap(data: bytes) -> dict | None:
        """Minimal radiotap parser — extract RSSI."""
        if len(data) < 8:
            return None
        hdr_len = struct.unpack_from('<H', data, 2)[0]
        present = struct.unpack_from('<I', data, 4)[0]
        if len(data) < hdr_len:
            return None
        result = {"length": hdr_len}
        offset = 8
        bit = 0
        while bit < 32:
            if present & (1 << bit):
                if bit == 0: offset += 8
                elif bit == 1: offset += 1
                elif bit == 2: offset += 1
                elif bit == 3: offset += 4
                elif bit == 4: offset += 2
                elif bit == 5:
                    if offset + 1 <= len(data):
                        result["rssi"] = struct.unpack_from('<b', data, offset)[0]
                    offset += 1
                elif bit == 6: offset += 1
                elif bit == 10:
                    if offset + 1 <= len(data):
                        result["rssi"] = struct.unpack_from('<b', data, offset)[0]
                    offset += 1
                elif bit == 11:
                    offset += 1
                elif bit == 12: offset += 1
                else:
                    sz = MonitorDetector.RADIOTAP_FIELD_SIZES.get(bit, 0)
                    offset += sz if sz > 0 else 1
            bit += 1
            if offset >= hdr_len:
                break
        return result if "rssi" in result else None

    def open(self) -> bool:
        if not interface_exists(self.iface):
            return False
        try:
            self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                                      socket.htons(0x0003))
            self.sock.bind((self.iface, 0))
            self.sock.settimeout(self.window)
            return True
        except Exception:
            return False

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def capture(self, timeout: float = None) -> list:
        """Capture frames, return list of (ts, rssi, src_mac, is_probe)."""
        if not self.sock and not self.open():
            return []
        frames = []
        deadline = time.time() + (timeout or self.window)
        self.sock.settimeout(0.5)
        while time.time() < deadline:
            try:
                data = self.sock.recv(4096)
                if len(data) < 24:
                    continue
                rt = MonitorDetector._parse_radiotap(data)
                if not rt:
                    continue
                rt_len = rt["length"]
                off = rt_len
                if len(data) < off + 2:
                    continue

                fc = struct.unpack_from('<H', data, off)[0]
                ftype = (fc >> 2) & 0x3
                subtype = (fc >> 4) & 0xf
                is_probe = (ftype == 0 and subtype == 4)
                is_beacon = (ftype == 0 and subtype == 8)

                # Extract addr2 (source) at off + 10
                src_mac = "??:??:??:??:??:??"
                if len(data) >= off + 16:
                    src_mac = ":".join(f"{data[off+10+i]:02x}" for i in range(6))

                frames.append({
                    "ts": time.time(),
                    "rssi": rt["rssi"],
                    "src": src_mac,
                    "is_probe": is_probe,
                    "is_beacon": is_beacon,
                    "ftype": ftype,
                    "subtype": subtype,
                })
            except socket.timeout:
                continue
            except Exception:
                continue
        return frames

    def scan(self, timeout: float = None) -> dict:
        """Convenience: capture + compute metrics in one call."""
        frames = self.capture(timeout)
        devices = set(f["src"] for f in frames if f["src"])
        probes = sum(1 for f in frames if f["is_probe"])
        beacons = sum(1 for f in frames if f["is_beacon"])
        rssi_vals = [f["rssi"] for f in frames if f["rssi"] is not None]

        # Presence score from monitor metrics
        score = 0.0
        n_devices = len(devices)
        n_probes = probes
        n_frames = len(frames)
        frame_rate = n_frames / max(timeout or self.window, 0.1)

        if n_devices <= 1:
            dev_score = 0.0
        elif n_devices <= 3:
            dev_score = 0.5
        else:
            dev_score = min(1.0, n_devices / 10)
        score += dev_score * 0.4

        if n_probes <= 2:
            probe_score = 0.0
        elif n_probes <= 10:
            probe_score = 0.3
        else:
            probe_score = min(1.0, n_probes / 30)
        score += probe_score * 0.35

        if frame_rate <= 5:
            act_score = 0.0
        else:
            act_score = min(1.0, frame_rate / 100)
        score += act_score * 0.25

        return {
            "monitor_frames": n_frames,
            "monitor_devices": n_devices,
            "monitor_probes": n_probes,
            "monitor_beacons": beacons,
            "monitor_rssi_mean": round(mean(rssi_vals), 1) if rssi_vals else None,
            "monitor_presence_score": round(score, 3),
            "monitor_grade": "high" if score > 0.5 else ("medium" if score > 0.3 else "low"),
        }


# ============================================================
# BridgeClient (da bridge_client.py — inline minimale)
# ============================================================

def _msgpack_encode(obj):
    """Minimal msgpack encoder."""
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
        return b'\xce' + struct.pack('>I', obj)
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
        if n <= 0x0f:
            buf = bytes([0x90 | n])
        else:
            buf = b'\xdc' + struct.pack('>H', n)
        for item in obj:
            buf += _msgpack_encode(item)
        return buf
    return b''


def _msgpack_decode(data, pos=0):
    """Minimal msgpack decoder."""
    if pos >= len(data):
        raise ValueError("truncated")
    b = data[pos]
    pos += 1
    if b <= 0x7f:
        return b, pos
    if b >= 0xe0:
        return b - 256, pos
    if 0xa0 <= b <= 0xbf:
        n = b & 0x1f
        return data[pos:pos+n].decode(), pos+n
    if b == 0xc0:
        return None, pos
    if b == 0xc2:
        return False, pos
    if b == 0xc3:
        return True, pos
    if b == 0xca:
        return struct.unpack('>f', data[pos:pos+4])[0], pos+4
    if b == 0xcb:
        return struct.unpack('>d', data[pos:pos+8])[0], pos+8
    if b == 0xcc:
        return data[pos], pos+1
    if b == 0xcd:
        return struct.unpack('>H', data[pos:pos+2])[0], pos+2
    if b == 0xce:
        return struct.unpack('>I', data[pos:pos+4])[0], pos+4
    if b == 0xd0:
        return struct.unpack('>b', data[pos:pos+1])[0], pos+1
    if 0x90 <= b <= 0x9f:
        n = b & 0x0f
        result = [None] * n
        for i in range(n):
            val, pos = _msgpack_decode(data, pos)
            result[i] = val
        return result, pos
    if b == 0xdc:
        n = struct.unpack('>H', data[pos:pos+2])[0]
        pos += 2
        result = [None] * n
        for i in range(n):
            val, pos = _msgpack_decode(data, pos)
            result[i] = val
        return result, pos
    if b == 0xd9:
        n = data[pos]
        pos += 1
        return data[pos:pos+n].decode(), pos+n
    raise ValueError(f"unknown msgpack byte 0x{b:02x}")


def bridge_rpc(socket_path: str, method: str, *params, timeout: float = 3.0):
    """Call an RPC method on the bridge (Unix socket). Returns result or None."""
    request = [0, 1, method, list(params)]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(socket_path)
            s.sendall(_msgpack_encode(request))
            resp = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                resp += chunk
                # Try to decode after each chunk
                try:
                    result, _ = _msgpack_decode(resp)
                    return result[3] if isinstance(result, list) and len(result) >= 4 else result
                except (ValueError, IndexError):
                    continue
    except (socket.timeout, ConnectionRefusedError, OSError):
        return None
    except ValueError:
        return None


# ============================================================
# Logger
# ============================================================

class CsvLogger:
    """CSV logger with headers and flush-every-write."""

    def __init__(self, path: str = "", prefix: str = "presence"):
        self.path = path
        self.file = None
        self.prefix = prefix
        self._headers_written = False

    def _get_file(self):
        if self.path:
            if not self.file:
                try:
                    self.file = open(self.path, "a")
                except Exception:
                    self.path = ""
            return self.file
        # Auto-rotate per day
        fname = f"{self.prefix}_{datetime.now().strftime('%Y%m%d')}.csv"
        if not self.file or self.file.name != os.path.abspath(fname):
            if self.file:
                self.file.close()
            try:
                self.file = open(fname, "a")
            except Exception:
                return None
        return self.file

    def write(self, data: dict):
        f = self._get_file()
        if not f:
            return
        ts = data.get("timestamp", datetime.now().isoformat())
        # Build row in consistent order
        keys = ["timestamp", "rssi", "signal_avg", "grad_score", "presence",
                "reasons", "monitor_devices", "monitor_score", "device_count",
                "monitor_probes", "ping_avg", "ping_mdev", "sensor_temp", "sensor_hum"]
        if not self._headers_written:
            f.write(",".join(keys) + "\n")
            self._headers_written = True
        row = []
        for k in keys:
            v = data.get(k, "")
            row.append(str(v).replace(",", ";").replace("\n", " "))
        f.write(",".join(row) + "\n")
        f.flush()

    def close(self):
        if self.file:
            self.file.close()
            self.file = None


# ============================================================
# Config loader
# ============================================================

def load_config(path: str | None) -> dict:
    """Load config from JSON file, overlay defaults."""
    cfg = dict(DEFAULT_CONFIG)
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                user = json.load(f)
                cfg.update(user)
            print(f"[*] Loaded config: {path}")
        except Exception as e:
            print(f"[!] Config error: {e}")
    return cfg


def save_config(path: str, cfg: dict):
    """Save current config as JSON."""
    try:
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2)
        print(f"[✓] Config saved: {path}")
    except Exception as e:
        print(f"[!] Failed to save config: {e}")


# ============================================================
# Main Engine
# ============================================================

class DecisionEngine:
    """
    Main decision loop.
    Combines gradient detection + monitor scan + bridge sensors.
    """

    def __init__(self, config: dict):
        self.cfg = config
        self.logger = CsvLogger(
            path=config.get("log_file", ""),
            prefix="presence"
        )
        self.detector = GradientDetector(
            window_size=config.get("window_size", 20),
            grad_threshold=config.get("grad_threshold", 1.0),
            consecutive_threshold=config.get("consecutive_threshold", 3),
        )
        self.monitor = MonitorDetector(
            iface=config.get("monitor_iface", "mon0"),
            window=config.get("monitor_window", 2.0),
        ) if config.get("use_monitor") else None

        self.iface = config.get("wifi_iface") or detect_wifi_iface()
        self.gateway = detect_gateway()
        self.running = False
        self.presence_state = False
        self.last_actuation = 0
        self.cooldown = config.get("cooldown_seconds", 10)
        self.sample_interval = config.get("sample_interval", 0.05)
        self.threshold = config.get("presence_threshold", 0.5)

        # Fusion scores from latest cycle
        self.gradient_fusion = 0.0
        self.monitor_fusion = 0.0
        self.sensor_fusion = 0.0

        # Grace period for calibration
        self.samples_collected = 0
        self.calibration_samples = 300  # ~15s at 20Hz

    def log(self, data: dict):
        data["timestamp"] = datetime.now().isoformat()
        self.logger.write(data)

    def actuate(self, state: bool):
        """Control relay/LED based on presence state."""
        now = time.time()
        if state == self.presence_state:
            return  # already in this state
        if now - self.last_actuation < self.cooldown:
            return  # cooldown

        self.presence_state = state
        self.last_actuation = now
        tag = "ON" if state else "OFF"
        print(f"\n  >>> ACTUATION: {tag}")

        if self.cfg.get("actuate") and self.iface:
            relay = self.cfg.get("relay_gpio", "LED_BUILTIN")
            # Try bridge RPC for relay control
            if self.cfg.get("use_bridge"):
                result = bridge_rpc(
                    self.cfg["bridge_socket"],
                    "setRelay", 1 if state else 0,
                )
                if result is not None:
                    print(f"  >>> Bridge relay set to {1 if state else 0}")
                    return

            # Fallback: GPIO via sysfs
            try:
                gpio = relay
                if gpio.startswith("LED_"):
                    # On UNO Q, LED_BUILTIN might be GPIO indicator
                    subprocess.run(
                        f"echo {'255' if state else '0'} > /sys/class/leds/{gpio}/brightness",
                        shell=True, timeout=2, stderr=subprocess.DEVNULL
                    )
            except Exception:
                pass

    def get_sensor_data(self) -> dict:
        """Read STM32 sensors via bridge."""
        if not self.cfg.get("use_bridge"):
            return {}
        result = bridge_rpc(self.cfg["bridge_socket"], "getSensors")
        if isinstance(result, dict):
            return {
                "sensor_temp": result.get("temperature"),
                "sensor_hum": result.get("humidity"),
                "sensor_gas": result.get("gas"),
                "sensor_ldr": result.get("ldr"),
            }
        return {}

    def cycle(self, log_data: dict = None) -> dict:
        """
        Single decision cycle.
        Returns full data dict with all metrics and decision.
        """
        data = {}
        gradient_score = 0.0

        # 1. Wi-Fi poll (GradientDetector)
        metrics = get_wifi_metrics(self.iface or "")
        ping = get_ping_metrics(self.gateway)
        metrics.update(ping)

        presence, debug = self.detector.update(metrics)
        gradient_score = debug.get("score", 0.0)
        data["rssi"] = metrics.get("rssi")
        data["signal_avg"] = metrics.get("signal_avg")
        data["ping_avg"] = metrics.get("ping_avg")
        data["ping_mdev"] = metrics.get("ping_mdev")
        data["grad_score"] = gradient_score
        data["grad_calibrated"] = self.detector.calibrated
        data["reasons"] = ";".join(debug.get("reasons", []))

        # 2. Monitor mode scan (every N cycles or periodic)
        use_monitor = (self.samples_collected % 400 == 0) and self.monitor is not None
        if use_monitor:
            mon_data = self.monitor.scan()
            data.update(mon_data)

        # 3. Sensor data (periodic)
        if self.samples_collected % 200 == 0:
            sensor_data = self.get_sensor_data()
            data.update(sensor_data)

        # 4. Fusion: combine gradient + monitor + sensor
        mon_score = data.get("monitor_presence_score", 0.0) if "monitor_presence_score" in data else 0.5

        w_grad = self.cfg.get("gradient_score_weight", 0.5)
        w_mon = self.cfg.get("monitor_score_weight", 0.3)
        w_sens = self.cfg.get("sensor_score_weight", 0.2)

        # Normalize gradient score to 0-1 range
        grad_norm = min(1.0, gradient_score / 3.0)

        fusion_score = grad_norm * w_grad + mon_score * w_mon

        # Sensor bonus if available
        temp = data.get("sensor_temp")
        if temp is not None:
            # Large temperature change = possible human presence
            # For now, small bonus if we have valid readings
            fusion_score += 0.1 * w_sens

        final_presence = fusion_score > self.threshold
        data["fusion_score"] = round(fusion_score, 3)
        data["presence"] = 1 if final_presence else 0
        data["device_count"] = data.get("monitor_devices", 0)

        # Grade
        if fusion_score < 0.2:
            data["grade"] = "none"
        elif fusion_score < 0.4:
            data["grade"] = "low"
        elif fusion_score < 0.6:
            data["grade"] = "medium"
        else:
            data["grade"] = "high"

        # Merge external log data
        if log_data:
            data.update(log_data)

        # 5. Actuation
        if self.cfg.get("actuate"):
            self.actuate(final_presence)

        self.samples_collected += 1
        return data

    def run(self, dry_run: bool = False, max_cycles: int = None):
        """Main loop."""
        self.running = True
        print(f"[*] Decision Engine starting")
        print(f"[*] Wi-Fi iface: {self.iface}")
        print(f"[*] Gateway: {self.gateway or 'N/D'}")
        print(f"[*] Monitor: {'enabled' if self.monitor else 'disabled'}")
        print(f"[*] Bridge: {'enabled' if self.cfg.get('use_bridge') else 'disabled'}")
        print(f"[*] Threshold: {self.threshold}")
        print(f"[*] Sampling: {self.sample_interval*1000:.0f}ms")
        print(f"[*] Dry run: {dry_run}")
        print()

        if dry_run:
            self.cfg["actuate"] = False

        # Header
        print(f"{'time':>7} {'RSSI':>5} {'Grad':>5} {'Fusion':>7} {'Devs':>5} {'Probes':>6} {'Grade':>7} {'Status'}")
        print("-" * 55)

        cycles = 0
        start_t = time.time()

        try:
            while self.running:
                data = self.cycle()

                # Live output
                t = time.time() - start_t
                rssi_s = str(data.get("rssi", "-")) if data.get("rssi") is not None else "-"
                grad_s = f"{data.get('grad_score', 0):.1f}"
                fusion_s = f"{data['fusion_score']:.3f}"
                dev_s = str(data.get("device_count", "-"))
                probes_s = str(data.get("monitor_probes", "-"))
                grade_s = data.get("grade", "none")
                status_s = "P!" if data["presence"] else "-"
                print(f"{t:>7.1f} {rssi_s:>5} {grad_s:>5} {fusion_s:>7} {dev_s:>5} {probes_s:>6} {grade_s:>7} {status_s:>6}")

                # Log
                self.log(data)

                cycles += 1
                if max_cycles and cycles >= max_cycles:
                    print(f"\n[*] Reached {max_cycles} cycles, stopping")
                    break

                time.sleep(self.sample_interval)

        except KeyboardInterrupt:
            print(f"\n[*] Stopped after {cycles} cycles ({time.time()-start_t:.0f}s)")
        finally:
            self.stop()

    def stop(self):
        self.running = False
        self.logger.close()
        if self.monitor:
            self.monitor.close()
        print("[*] Decision Engine stopped")


# ============================================================
# CLI
# ============================================================

def main():
    ap = argparse.ArgumentParser(
        description="Decision Engine — Presence detection orchestrator (UNO Q)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--config", help="Path to JSON config file")
    ap.add_argument("--save-config", help="Save default config to path and exit")
    ap.add_argument("--dry-run", action="store_true", help="Log only, no actuation")
    ap.add_argument("--set-relay", choices=["on", "off"], help="Set relay and exit")
    ap.add_argument("--get-sensors", action="store_true", help="Read STM32 sensors and exit")
    ap.add_argument("--threshold", type=float, help="Presence threshold (override)")
    ap.add_argument("--cycles", type=int, help="Max cycles, then exit")

    args = ap.parse_args()

    # Save default config
    if args.save_config:
        save_config(args.save_config, DEFAULT_CONFIG)
        return

    # Load config
    cfg = load_config(args.config)
    if args.threshold:
        cfg["presence_threshold"] = args.threshold

    # One-shot relay control
    if args.set_relay:
        state = args.set_relay == "on"
        if cfg.get("use_bridge"):
            result = bridge_rpc(cfg["bridge_socket"], "setRelay", 1 if state else 0)
            print(f"Relay set to {'ON' if state else 'OFF'}: {result}")
        else:
            print("Bridge not configured")
        return

    # One-shot sensor read
    if args.get_sensors:
        if cfg.get("use_bridge"):
            result = bridge_rpc(cfg["bridge_socket"], "getSensors")
            print(json.dumps(result, indent=2) if result else "No data")
        else:
            print("Bridge not configured")
        return

    # Main loop
    engine = DecisionEngine(cfg)
    engine.run(dry_run=args.dry_run, max_cycles=args.cycles)


if __name__ == "__main__":
    # Handle signals gracefully
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    main()
