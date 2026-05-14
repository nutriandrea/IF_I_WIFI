#!/usr/bin/env python3
"""
CSI Processor — Elaborazione dati Channel State Information da ESP32

Legge frame CSI dall'ESP32 via arduino-router (MsgPack RPC).
Analizza ampiezza/varianza per subcarrier per rilevare presenza.

Architettura:
  ESP32 (CSI capture) ──UART──> UNO Q MCU (esp32_csi_bridge.ino)
                                    │ arduino-router
                                    ▼
                              UNO Q Linux (questo script)

Usage:
  # Test connessione ESP32
  python3 csi_processor.py --ping

  # Monitoraggio real-time CSI
  python3 csi_processor.py --monitor

  # Calibrazione baseline (stanza vuota, 30s)
  python3 csi_processor.py --calibrate --seconds 30

  # Analisi dati salvati
  python3 csi_processor.py --analyze <file.json>

Dipendenze: msgpack (apt: python3-msgpack, pip: msgpack)
"""

import socket
import msgpack
import time
import json
import sys
import os
import re
import argparse
from datetime import datetime
from collections import deque
from statistics import mean, stdev

SOCKET_PATH = "/var/run/arduino-router.sock"
RPC_TIMEOUT = 5
POLL_INTERVAL = 0.2  # 200ms = 5 Hz poll

# ============================================================
# Arduino Router RPC Client (MsgPack)
# ============================================================

class RouterClient:
    """Client MsgPack RPC per arduino-router."""

    def __init__(self, socket_path=SOCKET_PATH):
        self.socket_path = socket_path
        self.sock = None
        self.msg_counter = 0
        self.pending = {}

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(RPC_TIMEOUT)
        self.sock.connect(self.socket_path)
        return True

    def call(self, method, *args):
        self.msg_counter += 1
        msgid = self.msg_counter
        msg = [0, msgid, method, list(args)]
        self.sock.sendall(msgpack.packb(msg))

        # Read response
        unpacker = msgpack.Unpacker()
        while True:
            try:
                data = self.sock.recv(4096)
                if not data:
                    raise ConnectionError("Connection closed")
                unpacker.feed(data)
                for response in unpacker:
                    if not isinstance(response, (list, tuple)):
                        continue
                    if len(response) < 4:
                        continue
                    r_type, r_id, r_err, r_result = response[:4]
                    if r_type == 1 and r_id == msgid:
                        if r_err is not None:
                            raise RuntimeError(str(r_err))
                        return r_result
            except socket.timeout:
                raise TimeoutError(f"Timeout waiting for {method}")

    def notify(self, method, *args):
        msg = [2, method, list(args)]
        self.sock.sendall(msgpack.packb(msg))

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None


# ============================================================
# CSI Data Parser
# ============================================================

# ESP32-CSI-Toolkit CSV header fields (index -> name)
CSI_FIELDS = [
    "type", "role", "mac", "rssi", "rate", "sig_mode", "mcs",
    "bandwidth", "smoothing", "not_sounding", "aggregation", "stbc",
    "fec_coding", "sgi", "noise_floor", "ampdu_cnt", "channel",
    "local_timestamp", "ant", "sig_len", "rx_state", "len", "first_word",
]


def parse_csi_csv(line: str) -> dict | None:
    """Parser per CSI data in formato CSV ESP32-CSI-Toolkit.
    Restituisce dict con campi header + array complesso `csi`."""
    try:
        parts = line.split(",")
        if len(parts) < 24:
            return None
        if parts[0] != "CSI_DATA":
            return None

        result = {}
        for i, name in enumerate(CSI_FIELDS):
            val = parts[i + 1]
            if name in ("rssi", "rate", "sig_mode", "mcs", "bandwidth",
                        "smoothing", "not_sounding", "aggregation", "stbc",
                        "fec_coding", "sgi", "noise_floor", "ampdu_cnt",
                        "channel", "ant", "sig_len", "len", "first_word"):
                try:
                    result[name] = int(val)
                except ValueError:
                    result[name] = val
            elif name == "mac":
                result[name] = val
            elif name == "type":
                result[name] = val
            elif name == "local_timestamp":
                try:
                    result[name] = int(val)
                except ValueError:
                    result[name] = val
            elif name == "rx_state":
                result[name] = val
            else:
                result[name] = val

        # Parse CSI complex data: real0,imag0,real1,imag1,...
        raw_data = parts[24:]
        csi_len = len(raw_data) // 2
        csi = []
        for j in range(csi_len):
            try:
                real = float(raw_data[2 * j])
                imag = float(raw_data[2 * j + 1])
                ampl = (real ** 2 + imag ** 2) ** 0.5
                phase = __import__("math").atan2(imag, real)
                csi.append({
                    "subcarrier": j,
                    "real": real,
                    "imag": imag,
                    "ampl": round(ampl, 3),
                    "phase": round(phase, 4),
                })
            except (ValueError, IndexError):
                break

        result["csi"] = csi
        result["num_subcarriers"] = len(csi)

        # Compute aggregate metrics
        if csi:
            amps = [c["ampl"] for c in csi]
            result["ampl_mean"] = round(mean(amps), 3)
            result["ampl_std"] = round(stdev(amps), 3) if len(amps) >= 2 else 0
            result["ampl_max"] = round(max(amps), 3)
            result["ampl_min"] = round(min(amps), 3)

        return result

    except Exception:
        return None


# ============================================================
# CSI Presence Detector (basato su varianza ampiezza)
# ============================================================

class CSIDetector:
    """
    Rileva presenza basandosi sulla varianza dell'ampiezza CSI
    attraverso le subcarrier e nel tempo.

    Principio: un corpo in movimento crea multipath che altera
    l'ampiezza di specifiche subcarrier. La varianza spaziale
    (tra subcarrier) e temporale (tra campioni) aumenta.
    """

    def __init__(self, window_size: int = 50, ampl_threshold: float = 2.0,
                 var_threshold: float = 1.5):
        self.ampl_std_hist = deque(maxlen=window_size)
        self.ampl_mean_hist = deque(maxlen=window_size)
        self.rssi_hist = deque(maxlen=window_size)
        self.ampl_threshold = ampl_threshold
        self.var_threshold = var_threshold
        self.calibrated = False
        self.baseline_ampl_std = None
        self.baseline_ampl_std_std = None
        self.baseline_rssi_mean = None
        self.baseline_rssi_std = None
        self._t0 = 0

    def update(self, frame: dict) -> tuple[bool, dict]:
        """Processa un frame CSI. Ritorna (presenza, debug_info)."""
        now = time.time()
        if self._t0 == 0:
            self._t0 = now
        info = {"t": round(now - self._t0, 3)}

        rssi = frame.get("rssi")
        ampl_std = frame.get("ampl_std")
        ampl_mean = frame.get("ampl_mean")

        if rssi is not None:
            self.rssi_hist.append(rssi)

        if ampl_std is not None:
            self.ampl_std_hist.append(ampl_std)
            info["ampl_std"] = ampl_std

        if ampl_mean is not None:
            self.ampl_mean_hist.append(ampl_mean)
            info["ampl_mean"] = ampl_mean

        # Auto-calibration after collecting enough frames
        cal_window = 20
        if not self.calibrated and len(self.ampl_std_hist) >= cal_window:
            vals = list(self.ampl_std_hist)[:cal_window]
            self.baseline_ampl_std = mean(vals)
            self.baseline_ampl_std_std = stdev(vals) if len(vals) >= 2 else 0.5
            if self.rssi_hist:
                rssi_vals = list(self.rssi_hist)[:cal_window]
                self.baseline_rssi_mean = mean(rssi_vals)
                self.baseline_rssi_std = stdev(rssi_vals) if len(rssi_vals) >= 2 else 0.5
            self.calibrated = True
            info["calibrated"] = True
            info["baseline_ampl_std"] = round(self.baseline_ampl_std, 3)
            info["baseline_ampl_std_std"] = round(self.baseline_ampl_std_std, 3)

        # --- Presence decision ---
        presence = False
        score = 0.0
        reasons = []

        if self.calibrated:
            # 1. Amplitude std score (subcarrier diversity)
            if ampl_std is not None:
                as_ = (ampl_std - self.baseline_ampl_std) / max(self.baseline_ampl_std_std, 0.1)
                if as_ > self.ampl_threshold:
                    score += as_
                    reasons.append(f"ampl_std={ampl_std:.2f}")

            # 2. RSSI delta (complementare)
            if rssi is not None and self.baseline_rssi_mean is not None:
                rssi_delta = abs(rssi - self.baseline_rssi_mean) / max(self.baseline_rssi_std, 0.5)
                if rssi_delta > self.var_threshold:
                    score += rssi_delta * 0.5  # peso minore
                    reasons.append(f"rssi_delta={rssi_delta:.1f}")

            # 3. Temporal variance (ampl_mean changes over time)
            if len(self.ampl_mean_hist) >= 5:
                recent_means = list(self.ampl_mean_hist)[-5:]
                mean_var = stdev(recent_means) if len(recent_means) > 1 else 0
                if mean_var > 0.5:  # significativa variazione temporale
                    score += mean_var
                    reasons.append(f"temp_var={mean_var:.2f}")

            presence = score > 2.0

        info["score"] = round(score, 2)
        info["presence"] = presence
        info["reasons"] = reasons
        info["calibrated"] = self.calibrated
        return presence, info


# ============================================================
# Collection & Analysis
# ============================================================

def collect_csi(client: RouterClient, seconds: int, label: str) -> list[dict]:
    """Raccoglie frame CSI per N secondi."""
    print(f"\n  Raccolta '{label}' — {seconds}s")
    print(f"  {'MUOVITI' if label == 'movement' else 'RIMANI FERMO'}.\n")

    frames = []
    start = time.time()
    last_report = 0
    errors = 0

    # Svuota buffer MCU all'inizio
    try:
        client.call("csi_clear")
    except Exception:
        pass

    while time.time() - start < seconds:
        try:
            raw = client.call("csi_read_all")
            if raw:
                for line in raw.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    parsed = parse_csi_csv(line)
                    if parsed:
                        parsed["_t"] = round(time.time() - start, 3)
                        parsed["_label"] = label
                        frames.append(parsed)
                    else:
                        errors += 1
        except Exception:
            errors += 1

        # Report ogni 5s
        elapsed = time.time() - start
        if elapsed - last_report >= 5:
            rate = len(frames) / elapsed if elapsed > 0 else 0
            print(f"    {elapsed:.0f}s — {len(frames)} frame ({rate:.1f}/s)"
                  f"{f', {errors} errori' if errors else ''}")
            last_report = elapsed

        time.sleep(POLL_INTERVAL)

    # Leggi eventuali frame rimasti
    try:
        raw = client.call("csi_read_all")
        if raw:
            for line in raw.split("\n"):
                line = line.strip()
                if not line:
                    continue
                parsed = parse_csi_csv(line)
                if parsed:
                    parsed["_t"] = round(time.time() - start, 3)
                    parsed["_label"] = label
                    frames.append(parsed)
    except Exception:
        pass

    total_s = round(time.time() - start, 1)
    print(f"\n  Raccolti {len(frames)} frame in {total_s}s"
          f" ({len(frames)/total_s:.1f}/s, {errors} errori)")

    # Salva su file
    filename = f"csi_{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(filename, "w") as f:
        json.dump({
            "label": label,
            "duration_s": total_s,
            "num_frames": len(frames),
            "frames": frames,
        }, f, indent=2)
    print(f"  Salvato: {filename}")
    return frames


def analyze(baseline: list[dict], movement: list[dict]):
    """Analisi comparativa baseline vs movement."""
    print("\n" + "=" * 60)
    print("  ANALISI CSI")
    print("=" * 60)

    def stats(vals):
        if not vals:
            return {"n": 0}
        return {
            "n": len(vals),
            "mean": round(mean(vals), 3),
            "std": round(stdev(vals), 3) if len(vals) >= 2 else 0,
            "min": round(min(vals), 3),
            "max": round(max(vals), 3),
        }

    b_ampl_std = [f.get("ampl_std", 0) for f in baseline]
    m_ampl_std = [f.get("ampl_std", 0) for f in movement]
    b_ampl_mean = [f.get("ampl_mean", 0) for f in baseline]
    m_ampl_mean = [f.get("ampl_mean", 0) for f in movement]
    b_rssi = [f.get("rssi", 0) for f in baseline]
    m_rssi = [f.get("rssi", 0) for f in movement]

    print(f"\n  {'Metrica':<20} {'Baseline':>20} {'Movement':>20}")
    print(f"  {'-'*20} {'-'*20} {'-'*20}")

    for name, bv, mv in [("ampl_std", b_ampl_std, m_ampl_std),
                          ("ampl_mean", b_ampl_mean, m_ampl_mean),
                          ("rssi", b_rssi, m_rssi)]:
        bs = stats(bv)
        ms = stats(mv)
        print(f"  {'n':<20} {bs['n']:>20} {ms['n']:>20}")
        print(f"  {name+'_mean':<20} {bs.get('mean', '-'):>20} {ms.get('mean', '-'):>20}")
        print(f"  {name+'_std':<20} {bs.get('std', '-'):>20} {ms.get('std', '-'):>20}")
        print(f"  {name+'_min':<20} {bs.get('min', '-'):>20} {ms.get('min', '-'):>20}")
        print(f"  {name+'_max':<20} {bs.get('max', '-'):>20} {ms.get('max', '-'):>20}")
        print()

    # Threshold sweep per ampl_std
    if len(b_ampl_std) >= 5 and len(m_ampl_std) >= 5:
        print("\n  --- Strategia: Soglia ampl_std ---")
        print(f"  {'Soglia':>8} {'FP':>8} {'TP':>8} {'Score':>8} {'Verdetto':>15}")
        for th in [x / 10 for x in range(5, 51, 5)]:
            fp = sum(1 for v in b_ampl_std if v > th) / max(len(b_ampl_std), 1)
            tp = sum(1 for v in m_ampl_std if v > th) / max(len(m_ampl_std), 1)
            score = tp - fp
            verdict = "OK" if score > 0.5 and fp < 0.3 else "FALSI POS" if score < 0.1 else "MARGINALE"
            print(f"  {th:>8.1f} {fp*100:>7.0f}% {tp*100:>7.0f}% {score:>8.2f} {verdict:>15}")


# ============================================================
# CLI
# ============================================================

def cmd_ping(client):
    try:
        resp = client.call("csi_ping")
        print(f"ESP32: {resp}")
    except Exception as e:
        print(f"Errore: {e}")
        print("  La ESP32 non risponde. Verifica:")
        print("  - Cablaggio: GND, 5V, TX→D0, RX→D1")
        print("  - ESP32 ha il firmware corretto (test_esp32_uart.ino)")
        print("  - arduino-router attivo: systemctl status arduino-router")


def cmd_monitor(client):
    print("CSI Monitor — Ctrl+C per uscire\n")
    print(f"{'t(s)':>8} {'subc':>5} {'ampl_mean':>10} {'ampl_std':>9} {'RSSI':>6} {'presenza':>9}")
    print("-" * 55)

    det = CSIDetector()
    try:
        client.call("csi_clear")
    except Exception:
        pass

    try:
        while True:
            try:
                raw = client.call("csi_read_all")
                if raw:
                    for line in raw.split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        parsed = parse_csi_csv(line)
                        if parsed:
                            presence, info = det.update(parsed)
                            p_str = "PRESENTE" if presence else "vuoto   "
                            print(f"{info['t']:>8.1f} "
                                  f"{parsed.get('num_subcarriers', 0):>5} "
                                  f"{parsed.get('ampl_mean', 0):>10.2f} "
                                  f"{parsed.get('ampl_std', 0):>9.2f} "
                                  f"{parsed.get('rssi', 0):>6} "
                                  f"{p_str}")
            except Exception:
                pass
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\nInterrotto.")


def cmd_analyze(filepath):
    if not os.path.exists(filepath):
        print(f"File non trovato: {filepath}")
        return

    with open(filepath) as f:
        data = json.load(f)

    frames = data.get("frames", [])
    label = data.get("label", "sconosciuto")
    print(f"Analisi: {filepath}")
    print(f"  Label: {label}")
    print(f"  Frame: {len(frames)}")
    print(f"  Durata: {data.get('duration_s', '?')}s")

    # Parse CSI
    parsed = []
    for f in frames:
        if "csi" not in f:
            continue
        parsed.append(f)

    if parsed:
        _show_csi_stats(parsed)


def _show_csi_stats(frames):
    amps = [f.get("ampl_std", 0) for f in frames]
    means = [f.get("ampl_mean", 0) for f in frames]
    rssis = [f.get("rssi", 0) for f in frames]

    print(f"\n  {'Metrica':<20} {'Media':>10} {'Std':>10} {'Min':>10} {'Max':>10}")
    print(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")

    for name, vals in [("ampl_std", amps), ("ampl_mean", means), ("rssi", rssis)]:
        if vals:
            print(f"  {name:<20} {mean(vals):>10.2f} {stdev(vals):>10.2f} "
                  f"{min(vals):>10.2f} {max(vals):>10.2f}")


def main():
    parser = argparse.ArgumentParser(description="CSI Processor — ESP32 + UNO Q")
    parser.add_argument("--ping", action="store_true", help="Test connessione ESP32")
    parser.add_argument("--monitor", action="store_true", help="Monitor real-time")
    parser.add_argument("--calibrate", action="store_true",
                        help="Calibrazione (baseline + movement)")
    parser.add_argument("--seconds", type=int, default=30,
                        help="Secondi per fase di calibrazione")
    parser.add_argument("--analyze", type=str, help="Analizza file JSON salvato")
    parser.add_argument("--socket", default=SOCKET_PATH,
                        help="Percorso Unix socket arduino-router")
    args = parser.parse_args()

    if args.analyze:
        cmd_analyze(args.analyze)
        return

    # Tutti gli altri comandi necessitano connessione al router
    client = RouterClient(args.socket)
    try:
        client.connect()
    except Exception as e:
        print(f"Errore connessione arduino-router: {e}")
        print(f"  Socket: {args.socket}")
        sys.exit(1)

    try:
        if args.ping:
            cmd_ping(client)
        elif args.monitor:
            cmd_monitor(client)
        elif args.calibrate:
            baseline = collect_csi(client, args.seconds, "baseline")
            input("\nPremi INVIO per iniziare la fase MOVEMENT...")
            movement = collect_csi(client, args.seconds, "movement")
            analyze(baseline, movement)
        else:
            parser.print_help()
    finally:
        client.close()


if __name__ == "__main__":
    main()
