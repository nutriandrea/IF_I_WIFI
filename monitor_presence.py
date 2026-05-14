#!/usr/bin/env python3
"""
monitor_presence.py — Presence detection via WiFi monitor mode (UNO Q).

Sfrutta il monitor mode supportato dalla UNO Q (Qualcomm QRB2210 + WCBN3536A)
per catturare frame 802.11 grezzi da TUTTI i dispositivi nel raggio d'azione.

Metriche:
  - device_count: dispositivi unici visti nella finestra
  - frame_rate: frame/sec totali (attivita' RF)
  - probe_req_count: smartphone che cercano reti (presenza umana)
  - rssi_per_device: mean/std per ogni MAC
  - rssi_variance: varianza complessiva multi-device

Usage:
  # Setup interfaccia monitor + cattura 5s
  python3 monitor_presence.py --capture 5

  # Solo setup
  python3 monitor_presence.py --setup

  # Solo teardown
  python3 monitor_presence.py --teardown

  # Daemon loop continuo (ogni 3s riporta metriche)
  python3 monitor_presence.py --daemon

  # Calibrazione rapida (30s baseline vuoto + 30s movimento)
  python3 monitor_presence.py --calibrate
"""

import struct
import time
import os
import sys
import socket
import argparse
import json
import subprocess
import re
from collections import defaultdict, deque
from datetime import datetime
from statistics import mean, stdev

# ============================================================
# Config
# ============================================================
PHY = "phy0"
MON_IFACE = "mon0"
WIFI_IFACE = "wlan0"       # interfaccia managed principale
CAPTURE_WINDOW = 3.0       # secondi per finestra di cattura
REPORT_DIR = os.path.dirname(os.path.abspath(__file__))
IW = "/usr/sbin/iw"

# ============================================================
# Radiotap header parsing (minimal: solo RSSI dBm)
# ============================================================
# Radiotap present flags:
# bit 5  (0x020) = Antenna signal (unsigned, 1 byte, .5 dBm units)
# bit 10 (0x400) = dBm antenna signal (signed, 1 byte, dBm)
# bit 16 (0x10000) = Extended presence
# bit 31 (0x80000000) = Next word is extended

RADIOTAP_TSFT      = 0x00000001  # 8 bytes
RADIOTAP_FLAGS     = 0x00000002  # 1 byte
RADIOTAP_RATE      = 0x00000004  # 1 byte
RADIOTAP_CHANNEL   = 0x00000008  # 4 bytes
RADIOTAP_FHSS      = 0x00000010  # 2 bytes
RADIOTAP_DBM_ANTSIGNAL = 0x00000020  # 1 byte (signed, dBm)
RADIOTAP_DBM_ANTNOISE  = 0x00000040  # 1 byte (signed, dBm)
RADIOTAP_ANT_SIGNAL    = 0x00000400  # 1 byte (signed, dBm) — usiamo questo
RADIOTAP_EXT        = 0x80000000

RADIOTAP_FIELD_SIZES = {
    RADIOTAP_TSFT: 8,
    RADIOTAP_FLAGS: 1,
    RADIOTAP_RATE: 1,
    RADIOTAP_CHANNEL: 4,
    RADIOTAP_FHSS: 2,
    RADIOTAP_DBM_ANTSIGNAL: 1,
    RADIOTAP_DBM_ANTNOISE: 1,
}


def parse_radiotap_rssi(frame: bytes) -> int | None:
    """Estrae RSSI in dBm dall'header radiotap."""
    if len(frame) < 8:
        return None
    hdr_len = struct.unpack_from('<H', frame, 2)[0]
    if hdr_len < 8 or len(frame) < hdr_len:
        return None

    present = struct.unpack_from('<I', frame, 4)[0]
    rssi = None
    offset = 8  # dopo header fisso di 8 byte

    bit = 0
    while bit < 32:
        if present & (1 << bit):
            if bit == 0:    # TSFT
                offset += 8
            elif bit == 1:  # Flags
                offset += 1
            elif bit == 2:  # Rate
                offset += 1
            elif bit == 3:  # Channel
                offset += 4
            elif bit == 4:  # FHSS
                offset += 2
            elif bit == 5:  # dBm antenna signal
                if offset + 1 <= hdr_len:
                    rssi = struct.unpack_from('<b', frame, offset)[0]
                offset += 1
            elif bit == 6:  # dBm antenna noise
                if offset + 1 <= hdr_len:
                    pass  # skip noise
                offset += 1
            elif bit == 10:  # Antenna signal (dBm) - signed
                if offset + 1 <= hdr_len:
                    rssi = struct.unpack_from('<b', frame, offset)[0]
                offset += 1
            elif bit == 31:  # Extended presence mask
                if offset + 4 <= hdr_len:
                    ext = struct.unpack_from('<I', frame, offset)[0]
                    present |= ext  # Add extended bits
                    offset += 4
                bit = -1  # reset to continue with extended bits
            else:
                # Skip unknown fixed fields (approximate size)
                # Most are 1-8 bytes, skip 1 for safety
                offset += 1
        bit += 1
        if offset >= hdr_len:
            break

    return rssi


def parse_80211_header(frame: bytes, radiotap_len: int) -> dict:
    """Estrae campi base dal MAC header 802.11."""
    off = radiotap_len
    if len(frame) < off + 2:
        return {}

    # Frame Control
    fc = struct.unpack_from('<H', frame, off)[0]
    proto = fc & 0x3
    ftype = (fc >> 2) & 0x3       # 0=mgmt, 1=ctrl, 2=data
    subtype = (fc >> 4) & 0xf
    to_ds = (fc >> 8) & 1
    from_ds = (fc >> 9) & 1

    # Durations (2 bytes)
    off += 2
    off += 2  # duration

    # Address fields (6 byte each)
    # 802.11 MAC header is 24 bytes (without QoS control, HT control)
    # addr1 (RA), addr2 (TA), addr3, seq_ctrl (2), addr4 (if present)
    addr1 = frame[off:off+6] if off + 6 <= len(frame) else b''
    addr2 = frame[off+6:off+12] if off + 12 <= len(frame) else b''
    addr3 = frame[off+12:off+18] if off + 18 <= len(frame) else b''

    src_mac = None
    dst_mac = None
    bssid = None

    # Standard 802.11 addressing based on ToDS/FromDS
    if to_ds == 0 and from_ds == 0:
        # STA → STA (IBSS, Direct)
        dst_mac = addr1
        src_mac = addr2
        bssid = addr3
    elif to_ds == 1 and from_ds == 0:
        # STA → AP
        dst_mac = addr3  # BSSID is addr3 when to DS
        src_mac = addr2
        bssid = addr1
    elif to_ds == 0 and from_ds == 1:
        # AP → STA
        dst_mac = addr1
        src_mac = addr3
        bssid = addr2
    elif to_ds == 1 and from_ds == 1:
        # WDS (rare)
        src_mac = addr3
        dst_mac = addr1

    return {
        "type": ftype,
        "subtype": subtype,
        "src_mac": mac_str(src_mac) if src_mac else None,
        "dst_mac": mac_str(dst_mac) if dst_mac else None,
        "bssid": mac_str(bssid) if bssid else None,
        "to_ds": to_ds,
        "from_ds": from_ds,
    }


def mac_str(b: bytes) -> str:
    """bytes → MAC stringa."""
    if not b or len(b) < 6:
        return ""
    return ":".join(f"{x:02x}" for x in b)


# ============================================================
# Monitor interface management
# ============================================================

def monitor_exists(name: str = MON_IFACE) -> bool:
    """Verifica se l'interfaccia monitor esiste."""
    try:
        for entry in os.listdir("/sys/class/net"):
            if entry == name:
                return True
    except FileNotFoundError:
        pass
    return False


def setup_monitor(phy: str = PHY, name: str = MON_IFACE) -> bool:
    """Crea interfaccia monitor virtuale."""
    if monitor_exists(name):
        print(f"monitor: {name} esiste gia'")
        return True
    try:
        subprocess.check_call(
            f"{IW} phy {phy} interface add {name} type monitor",
            shell=True, timeout=5
        )
        subprocess.check_call(
            f"ip link set {name} up",
            shell=True, timeout=5
        )
        print(f"monitor: {name} creata e attiva")
        return True
    except Exception as e:
        print(f"monitor: errore setup {e}", file=sys.stderr)
        return False


def teardown_monitor(name: str = MON_IFACE):
    """Rimuove interfaccia monitor."""
    if not monitor_exists(name):
        return
    try:
        subprocess.check_call(
            f"ip link set {name} down",
            shell=True, timeout=5
        )
        subprocess.check_call(
            f"{IW} dev {name} del",
            shell=True, timeout=5
        )
        print(f"monitor: {name} rimossa")
    except Exception as e:
        print(f"monitor: errore teardown {e}", file=sys.stderr)


# ============================================================
# Frame capture & metrics
# ============================================================

class FrameCollector:
    """Cattura frame 802.11 via raw socket su interfaccia monitor."""

    def __init__(self, iface: str = MON_IFACE):
        self.iface = iface
        self.sock = None

    def open(self):
        """Apre raw socket in modalita' non-bloccante."""
        try:
            self.sock = socket.socket(
                socket.AF_PACKET,
                socket.SOCK_RAW,
                socket.ntohs(0x0003)  # Tutti i protocolli Ethernet
            )
            self.sock.bind((self.iface, 0))
            self.sock.settimeout(1.0)
            return True
        except Exception as e:
            print(f"monitor: errore socket {e}", file=sys.stderr)
            self.sock = None
            return False

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def capture(self, duration: float = CAPTURE_WINDOW) -> list:
        """Cattura frame per 'duration' secondi. Ritorna lista di dict."""
        if not self.sock:
            return []
        frames = []
        deadline = time.time() + duration
        while time.time() < deadline:
            try:
                data = self.sock.recv(65535)
            except socket.timeout:
                continue
            except Exception:
                break
            if not data:
                continue

            # sll header (14 bytes) per AF_PACKET cooked packets
            # skip se interfaccia raw
            ts = time.time()

            rssi = parse_radiotap_rssi(data)
            if rssi is None:
                continue  # nessun RSSI in questo frame

            # Radiotap length
            if len(data) < 4:
                continue
            rt_len = struct.unpack_from('<H', data, 2)[0]

            hdr = parse_80211_header(data, rt_len)
            src = hdr.get("src_mac")
            ftype = hdr.get("type")
            subtype = hdr.get("subtype")

            frames.append({
                "ts": ts,
                "rssi": rssi,
                "src_mac": src or "unknown",
                "type": ftype,
                "subtype": subtype,
                "is_probe_req": (ftype == 0 and subtype == 4),
                "is_beacon": (ftype == 0 and subtype == 8),
            })

        return frames

    @staticmethod
    def compute_metrics(frames: list, window: float) -> dict:
        """Calcola metriche di presenza dalla lista di frame."""
        if not frames:
            return {
                "device_count": 0,
                "frame_rate": 0,
                "probe_req_count": 0,
                "beacon_count": 0,
                "rssi_mean": None,
                "rssi_std": None,
                "rssi_per_device": {},
                "presence_score": 0.0,
                "duration": window,
            }

        # Dispositivi unici per source MAC
        devices = defaultdict(list)
        probe_req_count = 0
        beacon_count = 0
        all_rssi = []

        for f in frames:
            src = f["src_mac"]
            if src:
                devices[src].append(f["rssi"])
            all_rssi.append(f["rssi"])
            if f["is_probe_req"]:
                probe_req_count += 1
            if f["is_beacon"]:
                beacon_count += 1

        device_count = len(devices)
        frame_rate = len(frames) / max(window, 0.1)

        rssi_mean = mean(all_rssi) if all_rssi else None
        rssi_std = stdev(all_rssi) if len(all_rssi) > 1 else None

        rssi_per_device = {}
        for mac, vals in devices.items():
            rssi_per_device[mac] = {
                "mean": mean(vals),
                "std": stdev(vals) if len(vals) > 1 else 0,
                "count": len(vals),
            }

        # Score di presenza pesato:
        # - device_count: piu' dispositivi ≈ piu' persone
        # - probe_req_count: smartphone che cercano reti
        # - frame_rate: attivita' RF generale
        # - rssi_std: varianza indica movimento
        score = 0.0
        score += min(device_count / 3.0, 1.0) * 0.35      # 35%: device density
        score += min(probe_req_count / 5.0, 1.0) * 0.30    # 30%: probe requests
        score += min(frame_rate / 50.0, 1.0) * 0.20        # 20%: RF activity
        if rssi_std is not None and rssi_std > 0:
            score += min(rssi_std / 5.0, 1.0) * 0.15       # 15%: RSSI variance

        return {
            "device_count": device_count,
            "frame_rate": round(frame_rate, 1),
            "probe_req_count": probe_req_count,
            "beacon_count": beacon_count,
            "rssi_mean": round(rssi_mean, 1) if rssi_mean is not None else None,
            "rssi_std": round(rssi_std, 1) if rssi_std is not None else None,
            "rssi_per_device": rssi_per_device,
            "presence_score": round(min(score, 1.0), 3),
            "duration": window,
            "total_frames": len(frames),
        }


# ============================================================
# Calibration (30s baseline + 30s movement)
# ============================================================

def calibrate():
    """Calibrazione: 30s ambiente vuoto + 30s movimento."""
    print("=== Monitor Mode Calibration ===")
    if not monitor_exists(MON_IFACE):
        print("Setup interfaccia monitor...")
        if not setup_monitor():
            print("ERRORE: impossibile creare interfaccia monitor")
            return

    collector = FrameCollector(MON_IFACE)
    if not collector.open():
        return

    baseline_file = os.path.join(REPORT_DIR, "monitor_baseline.json")
    movement_file = os.path.join(REPORT_DIR, "monitor_movement.json")

    # Fase 1: baseline (vuoto)
    print("\nFASE 1: Baseline — nessuno si muove (30s)")
    for i in range(30, 0, -1):
        print(f"\r  Cattura in corso... {i}s", end="", flush=True)
        time.sleep(1)
    print()
    frames_baseline = collector.capture(3.0)
    metrics_baseline = FrameCollector.compute_metrics(frames_baseline, 3.0)
    with open(baseline_file, "w") as f:
        json.dump(metrics_baseline, f, indent=2)
    print(f"  Baseline salvata: {metrics_baseline['device_count']} dispositivi, "
          f"score={metrics_baseline['presence_score']:.3f}")

    # Fase 2: movimento
    print("\nFASE 2: Movimento — cammina nella stanza (30s)")
    for i in range(30, 0, -1):
        print(f"\r  Cattura in corso... {i}s", end="", flush=True)
        time.sleep(1)
    print()
    frames_movement = collector.capture(3.0)
    metrics_movement = FrameCollector.compute_metrics(frames_movement, 3.0)
    with open(movement_file, "w") as f:
        json.dump(metrics_movement, f, indent=2)
    print(f"  Movimento salvato: {metrics_movement['device_count']} dispositivi, "
          f"score={metrics_movement['presence_score']:.3f}")

    collector.close()

    # Analisi
    print("\n=== Risultati Calibrazione ===")
    b = metrics_baseline
    m = metrics_movement
    print(f"  Baseline:   devices={b['device_count']}, frame_rate={b['frame_rate']}/s, "
          f"rssi_std={b['rssi_std']}, score={b['presence_score']:.3f}")
    print(f"  Movimento:  devices={m['device_count']}, frame_rate={m['frame_rate']}/s, "
          f"rssi_std={m['rssi_std']}, score={m['presence_score']:.3f}")

    delta_devices = m['device_count'] - b['device_count']
    delta_score = m['presence_score'] - b['presence_score']
    print(f"  Delta devices: +{delta_devices}, Delta score: +{delta_score:.3f}")

    soglia_score = (b['presence_score'] + m['presence_score']) / 2
    # Soglia adattiva: midpoint tra baseline e movimento
    print(f"\n  Soglia raccomandata (presence_score > {soglia_score:.3f} = presenza)")

    config = {
        "presence_threshold": round(soglia_score, 3),
        "baseline_score": b['presence_score'],
        "movement_score": m['presence_score'],
        "min_devices": b['device_count'] + 1,
    }
    config_path = os.path.join(REPORT_DIR, "monitor_config.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    print(f"  Config salvata: {config_path}")


# ============================================================
# Daemon loop
# ============================================================

def daemon_loop(threshold: float = 0.3, interval: float = 5.0):
    """Loop continuo di cattura + reporting."""
    if not monitor_exists(MON_IFACE):
        print("Setup interfaccia monitor...")
        if not setup_monitor():
            print("ERRORE: impossibile creare interfaccia monitor")
            return

    collector = FrameCollector(MON_IFACE)
    if not collector.open():
        return

    print(f"Monitor daemon started (threshold={threshold}, interval={interval}s)")
    print("timestamp,devices,frame_rate,probe_req,presence_score,presence")

    try:
        while True:
            frames = collector.capture(interval)
            metrics = FrameCollector.compute_metrics(frames, interval)
            present = metrics["presence_score"] > threshold
            ts = datetime.now().strftime("%H:%M:%S")

            # Linea CSV
            print(f"{ts},{metrics['device_count']},{metrics['frame_rate']},"
                  f"{metrics['probe_req_count']},{metrics['presence_score']:.3f},"
                  f"{'PRESENT' if present else 'empty'}")

            # Se presenza, mostra dettagli dispositivi
            if present and metrics['device_count'] > 0:
                devices_info = []
                for mac, info in metrics['rssi_per_device'].items():
                    if info['count'] >= 2:
                        devices_info.append(
                            f"{mac[-8:]}:{info['mean']:.0f}dBm"
                        )
                if devices_info:
                    print(f"  devices: {' '.join(devices_info)}")

            sys.stdout.flush()
            time.sleep(0.5)  # pausa tra finestre

    except KeyboardInterrupt:
        print("\nArresto...")
    finally:
        collector.close()
        teardown_monitor()


# ============================================================
# CLI
# ============================================================

def main():
    ap = argparse.ArgumentParser(
        description="Monitor-mode presence detection (UNO Q)"
    )
    ap.add_argument("--setup", action="store_true", help="Crea interfaccia monitor")
    ap.add_argument("--teardown", action="store_true", help="Rimuovi interfaccia monitor")
    ap.add_argument("--capture", type=float, nargs="?", const=CAPTURE_WINDOW,
                    default=0, help="Cattura N secondi e mostra metriche")
    ap.add_argument("--daemon", action="store_true",
                    help="Loop continuo ogni --interval secondi")
    ap.add_argument("--threshold", type=float, default=0.3,
                    help="Soglia presence_score per --daemon")
    ap.add_argument("--interval", type=float, default=3.0,
                    help="Secondi per finestra di cattura (default: 3)")
    ap.add_argument("--calibrate", action="store_true",
                    help="Calibrazione 30s baseline + 30s movimento")
    ap.add_argument("--json", action="store_true",
                    help="Output JSON (solo --capture)")

    args = ap.parse_args()

    if args.setup:
        setup_monitor()
        return

    if args.teardown:
        teardown_monitor()
        return

    if args.calibrate:
        calibrate()
        return

    if args.daemon:
        daemon_loop(threshold=args.threshold, interval=args.interval)
        return

    if args.capture > 0:
        if not monitor_exists(MON_IFACE):
            print(f"ERRORE: interfaccia {MON_IFACE} non trovata. Esegui --setup prima.")
            return

        collector = FrameCollector(MON_IFACE)
        if not collector.open():
            return

        print(f"Cattura in corso ({args.capture}s)...", file=sys.stderr)
        frames = collector.capture(args.capture)
        metrics = FrameCollector.compute_metrics(frames, args.capture)
        collector.close()

        if args.json:
            print(json.dumps(metrics, indent=2))
        else:
            print(f"  Frame catturati: {metrics['total_frames']}")
            print(f"  Durata: {metrics['duration']:.1f}s")
            print(f"  Frame rate: {metrics['frame_rate']}/s")
            print(f"  Dispositivi unici: {metrics['device_count']}")
            print(f"  Probe request: {metrics['probe_req_count']}")
            print(f"  Beacon: {metrics['beacon_count']}")
            print(f"  RSSI medio: {metrics['rssi_mean']} dBm")
            print(f"  RSSI std: {metrics['rssi_std']} dBm")
            print(f"  Presence score: {metrics['presence_score']:.3f}")

            if metrics['device_count'] > 0:
                print(f"\n  Dispositivi:")
                for mac, info in sorted(
                    metrics['rssi_per_device'].items(),
                    key=lambda x: x[1]['count'], reverse=True
                )[:10]:
                    print(f"    {mac}: mean={info['mean']:.0f} dBm, "
                          f"std={info['std']:.1f}, frames={info['count']}")
        return

    # Default: mostra help
    ap.print_help()


if __name__ == "__main__":
    main()
