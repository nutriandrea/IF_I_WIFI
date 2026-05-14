#!/usr/bin/env python3
"""
CSI Processor — Elaborazione dati Channel State Information da ESP32

Legge frame CSI dall'ESP32 via arduino-router (MsgPack RPC) e supporta:

  --ping       Test connessione ESP32
  --monitor    Real-time display ampiezza per subcarrier
  --calibrate  Baseline (vuoto) + movement per calibrazione presenza
  --benchmark  Salva dati in formato .mat per xyanchen/wifi-csi-sensing-benchmark
  --analyze    Analisi file .mat/.json salvato

Architettura:
  ESP32 (csi_firmware.ino) ──UART──> UNO Q MCU (esp32_csi_bridge.ino)
                                          │ arduino-router
                                          ▼
                                    UNO Q Linux (questo script)

Formato CSI atteso:
  CSI:<seq>:<rssi>:<noise>:<rate>:<bw_MHz>:<sub_count>:<r0,i0,r1,i1,...>

Dipendenze:
  msgpack  (apt: python3-msgpack)
  numpy    (apt: python3-numpy,   per benchmark mode)
  scipy    (apt: python3-scipy,   per salvare .mat)
"""

import socket, msgpack, time, json, sys, os, re, argparse
from datetime import datetime
from collections import deque
from statistics import mean, stdev
from math import sqrt, atan2

SOCKET_PATH = "/var/run/arduino-router.sock"
RPC_TIMEOUT = 5
POLL_INTERVAL = 0.2  # 200ms = 5 Hz poll lato bridge

# ============================================================
# Arduino Router RPC Client (MsgPack)
# ============================================================

class RouterClient:
    """Client MsgPack RPC per arduino-router."""

    def __init__(self, socket_path=SOCKET_PATH):
        self.socket_path = socket_path
        self.sock = None
        self.msg_counter = 0

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
# Parser frame CSI
# ============================================================

# Nuovo formato: CSI:<seq>:<rssi>:<noise>:<rate>:<bw>:<sub_count>:<r0,i0,r1,i1,...>
_RE_CSI = re.compile(
    r"CSI:(\d+):(-?\d+):(-?\d+):(\d+):(\d+):(\d+):([\d,\-]*)"
)

# Vecchio formato ESP32-CSI-Toolkit (compatibilità)
_CSI_FIELDS = [
    "type", "role", "mac", "rssi", "rate", "sig_mode", "mcs",
    "bandwidth", "smoothing", "not_sounding", "aggregation", "stbc",
    "fec_coding", "sgi", "noise_floor", "ampdu_cnt", "channel",
    "local_timestamp", "ant", "sig_len", "rx_state", "len", "first_word",
]


def parse_csi_line(line: str) -> dict | None:
    """Parser per frame CSI da ESP32.
    Supporta sia il nuovo formato (CSI:<seq>:...) che il vecchio (CSI_DATA,...).
    Ritorna dict con campi base + array `csi` di dict per subcarrier."""
    line = line.strip()
    if not line:
        return None

    # --- Nuovo formato firmaware ESP32 ---
    m = _RE_CSI.match(line)
    if m:
        try:
            seq = int(m.group(1))
            rssi = int(m.group(2))
            noise = int(m.group(3))
            rate = int(m.group(4))
            bw = int(m.group(5))
            sub_count = int(m.group(6))
            raw_numbers = m.group(7).split(",")

            n_values = min(len(raw_numbers), sub_count * 2)
            csi_data = []
            for j in range(0, n_values, 2):
                if j + 1 >= n_values:
                    break
                real_v = float(raw_numbers[j])
                imag_v = float(raw_numbers[j + 1])
                ampl = sqrt(real_v ** 2 + imag_v ** 2)
                phase = atan2(imag_v, real_v)
                csi_data.append({
                    "subcarrier": j // 2,
                    "real": real_v,
                    "imag": imag_v,
                    "ampl": round(ampl, 3),
                    "phase": round(phase, 4),
                })

            result = {
                "seq": seq,
                "rssi": rssi,
                "noise_floor": noise,
                "rate": rate,
                "bandwidth": bw,
                "num_subcarriers": len(csi_data),
                "csi": csi_data,
            }

            if csi_data:
                amps = [c["ampl"] for c in csi_data]
                result["ampl_mean"] = round(mean(amps), 3)
                result["ampl_std"] = round(stdev(amps), 3) if len(amps) >= 2 else 0
                result["ampl_max"] = round(max(amps), 3)
                result["ampl_min"] = round(min(amps), 3)

            return result

        except (ValueError, IndexError):
            return None

    # --- Vecchio formato CSI_DATA (ESP32-CSI-Toolkit) ---
    parts = line.split(",")
    if len(parts) < 24 or parts[0] != "CSI_DATA":
        return None

    try:
        result = {}
        for i, name in enumerate(_CSI_FIELDS):
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

        raw_data = parts[24:]
        csi_len = len(raw_data) // 2
        csi_data = []
        for j in range(csi_len):
            try:
                real_v = float(raw_data[2 * j])
                imag_v = float(raw_data[2 * j + 1])
                ampl = sqrt(real_v ** 2 + imag_v ** 2)
                phase = atan2(imag_v, real_v)
                csi_data.append({
                    "subcarrier": j,
                    "real": real_v,
                    "imag": imag_v,
                    "ampl": round(ampl, 3),
                    "phase": round(phase, 4),
                })
            except (ValueError, IndexError):
                break

        result["csi"] = csi_data
        result["num_subcarriers"] = len(csi_data)

        if csi_data:
            amps = [c["ampl"] for c in csi_data]
            result["ampl_mean"] = round(mean(amps), 3)
            result["ampl_std"] = round(stdev(amps), 3) if len(amps) >= 2 else 0
            result["ampl_max"] = round(max(amps), 3)
            result["ampl_min"] = round(min(amps), 3)

        return result

    except Exception:
        return None


# ============================================================
# CSIDetector (presence detection via CSI amplitude variance)
# ============================================================

class CSIDetector:
    """
    Rileva presenza basandosi sulla varianza dell'ampiezza CSI
    attraverso le subcarrier e nel tempo.

    Principio: un corpo in movimento crea multipath che altera
    l'ampiezza di specifiche subcarrier.
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

        presence = False
        score = 0.0
        reasons = []

        if self.calibrated:
            if ampl_std is not None:
                as_ = (ampl_std - self.baseline_ampl_std) / max(self.baseline_ampl_std_std, 0.1)
                if as_ > self.ampl_threshold:
                    score += as_
                    reasons.append(f"ampl_std={ampl_std:.2f}")

            if rssi is not None and self.baseline_rssi_mean is not None:
                rssi_delta = abs(rssi - self.baseline_rssi_mean) / max(self.baseline_rssi_std, 0.5)
                if rssi_delta > self.var_threshold:
                    score += rssi_delta * 0.5
                    reasons.append(f"rssi_delta={rssi_delta:.1f}")

            if len(self.ampl_mean_hist) >= 5:
                recent_means = list(self.ampl_mean_hist)[-5:]
                mean_var = stdev(recent_means) if len(recent_means) > 1 else 0
                if mean_var > 0.5:
                    score += mean_var
                    reasons.append(f"temp_var={mean_var:.2f}")

            presence = score > 2.0

        info["score"] = round(score, 2)
        info["presence"] = presence
        info["reasons"] = reasons
        info["calibrated"] = self.calibrated
        return presence, info


# ============================================================
# Collezione dati
# ============================================================

def collect_csi(client: RouterClient, seconds: int, label: str,
                out_dir: str = ".") -> list[dict]:
    """Raccoglie frame CSI per N secondi."""
    print(f"\n  Raccolta '{label}' — {seconds}s")
    print(f"  {'MUOVITI' if label == 'movement' else 'RIMANI FERMO'}.\n")

    frames = []
    start = time.time()
    last_report = 0
    errors = 0

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
                    parsed = parse_csi_line(line)
                    if parsed:
                        parsed["_t"] = round(time.time() - start, 3)
                        parsed["_label"] = label
                        frames.append(parsed)
                    else:
                        errors += 1
        except Exception:
            errors += 1

        elapsed = time.time() - start
        if elapsed - last_report >= 5:
            rate = len(frames) / elapsed if elapsed > 0 else 0
            print(f"    {elapsed:.0f}s — {len(frames)} frame ({rate:.1f}/s)"
                  f"{f', {errors} errori' if errors else ''}")
            last_report = elapsed

        time.sleep(POLL_INTERVAL)

    # Ultima lettura
    try:
        raw = client.call("csi_read_all")
        if raw:
            for line in raw.split("\n"):
                line = line.strip()
                if not line:
                    continue
                parsed = parse_csi_line(line)
                if parsed:
                    parsed["_t"] = round(time.time() - start, 3)
                    parsed["_label"] = label
                    frames.append(parsed)
    except Exception:
        pass

    total_s = round(time.time() - start, 1)
    print(f"\n  Raccolti {len(frames)} frame in {total_s}s"
          f" ({len(frames)/total_s:.1f}/s, {errors} errori)")
    return frames


# ============================================================
# Benchmark — salva .mat per xyanchen/wifi-csi-sensing-benchmark
# ============================================================

_SUBCARRIER_NAMES = [
    "seq", "rssi", "noise_floor", "rate", "bandwidth",
]


def save_benchmark_mat(frames: list[dict], label: str, filename: str):
    """Salva frame CSI come .mat compatibile con wifi-csi-sensing-benchmark.

    Il benchmark si aspetta:
      - `CSIamp`: matrix (n_subcarriers × n_packets) float32
      - Opzionale: metadata (seq, rssi, etc.)

    Ogni colonna di CSIamp è l'ampiezza di un frame CSI su tutte le subcarrier.
    """
    try:
        import numpy as np
    except ImportError:
        print("  ERRORE: numpy richiesto. Installa: apt install python3-numpy")
        return

    try:
        import scipy.io as sio
    except ImportError:
        print("  ERRORE: scipy richiesto. Installa: apt install python3-scipy")
        return

    # Estrai ampiezze per ogni frame
    amp_vectors = []
    rssi_list = []
    seq_list = []

    for f in frames:
        csi = f.get("csi")
        if not csi:
            continue
        amps = [c["ampl"] for c in csi]
        amp_vectors.append(amps)
        rssi_list.append(f.get("rssi", 0))
        seq_list.append(f.get("seq", 0))

    if not amp_vectors:
        print("  Nessun frame CSI valido.")
        return

    # Allinea tutte allo stesso numero di subcarrier (padding/trunc)
    max_sub = max(len(v) for v in amp_vectors)
    aligned = []
    for v in amp_vectors:
        if len(v) < max_sub:
            v = list(v) + [0.0] * (max_sub - len(v))
        aligned.append(v[:max_sub])

    matrix = np.array(aligned, dtype=np.float32).T  # (sub, packets)
    csi_amp = matrix

    # Salva .mat
    md = {
        "CSIamp": csi_amp,
        "label": label,
        "num_frames": len(frames),
        "num_subcarriers": max_sub,
        "rssi_mean": float(np.mean(rssi_list)) if rssi_list else 0,
        "timestamp": datetime.now().isoformat(),
    }

    try:
        sio.savemat(filename, md)
        print(f"  Salvato .mat: {filename}")
        print(f"    Shape: {csi_amp.shape[0]} subcarrier × {csi_amp.shape[1]} pacchetti")
        print(f"    Label: {label}")
        print(f"    Usabile con: CSI_Dataset(root_dir='.', modal='CSIamp')")
    except Exception as e:
        print(f"  ERRORE salvataggio .mat: {e}")


# ============================================================
# Analisi
# ============================================================

def analyze_frames(baseline: list[dict], movement: list[dict]):
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

    for name, bv, mv in [
        ("ampl_std",
         [f.get("ampl_std", 0) for f in baseline],
         [f.get("ampl_std", 0) for f in movement]),
        ("ampl_mean",
         [f.get("ampl_mean", 0) for f in baseline],
         [f.get("ampl_mean", 0) for f in movement]),
        ("rssi",
         [f.get("rssi", 0) for f in baseline],
         [f.get("rssi", 0) for f in movement]),
    ]:
        bs = stats(bv)
        ms = stats(mv)
        print(f"\n  {name}:")
        for k in ("n", "mean", "std", "min", "max"):
            print(f"    {k:<12} baseline={bs.get(k, '-'):>10}  movement={ms.get(k, '-'):>10}")

    # Threshold sweep
    b_ampl_std = [f.get("ampl_std", 0) for f in baseline]
    m_ampl_std = [f.get("ampl_std", 0) for f in movement]
    if len(b_ampl_std) >= 5 and len(m_ampl_std) >= 5:
        print(f"\n  --- Strategia: Soglia ampl_std ---")
        print(f"  {'Soglia':>8} {'FP':>8} {'TP':>8} {'Score':>8} {'Verdetto':>15}")
        for th in [x / 10 for x in range(5, 51, 5)]:
            fp = sum(1 for v in b_ampl_std if v > th) / max(len(b_ampl_std), 1)
            tp = sum(1 for v in m_ampl_std if v > th) / max(len(m_ampl_std), 1)
            score = tp - fp
            if score > 0.5 and fp < 0.3:
                verdict = "OK"
            elif score < 0.1:
                verdict = "FALSI POS"
            else:
                verdict = "MARGINALE"
            print(f"  {th:>8.1f} {fp*100:>7.0f}% {tp*100:>7.0f}% {score:>8.2f} {verdict:>15}")


# ============================================================
# Comandi CLI
# ============================================================

def cmd_ping(client):
    try:
        resp = client.call("csi_ping")
        print(f"ESP32: {resp}")
    except Exception as e:
        print(f"Errore: {e}")
        print("  Verifica cablaggio, ESP32, e arduino-router")


def cmd_monitor(client):
    print("CSI Monitor — Ctrl+C per uscire\n")
    print(f"{'t(s)':>8} {'seq':>6} {'subc':>5} {'ampl_mean':>10} {'ampl_std':>9} {'RSSI':>6}")
    print("-" * 55)

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
                        parsed = parse_csi_line(line)
                        if parsed:
                            t = parsed.get("_t", 0)
                            print(f"{t:>8.1f} "
                                  f"{parsed.get('seq', 0):>6} "
                                  f"{parsed.get('num_subcarriers', 0):>5} "
                                  f"{parsed.get('ampl_mean', 0):>10.2f} "
                                  f"{parsed.get('ampl_std', 0):>9.2f} "
                                  f"{parsed.get('rssi', 0):>6}")
            except Exception:
                pass
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\nInterrotto.")


def cmd_collect(client, seconds, label, out_dir):
    """Raccoglie e opzionalmente salva come .mat per benchmark."""
    frames = collect_csi(client, seconds, label, out_dir)
    if frames:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        mat_path = os.path.join(out_dir, f"CSI_{label}_{ts}.mat")
        save_benchmark_mat(frames, label, mat_path)
    return frames


def cmd_analyze(filepath):
    if not os.path.exists(filepath):
        print(f"File non trovato: {filepath}")
        return

    try:
        import numpy as np
        import scipy.io as sio
        data = sio.loadmat(filepath)
        print(f"\n  File: {filepath}")
        for k, v in data.items():
            if k.startswith("__"):
                continue
            if hasattr(v, "shape"):
                print(f"  {k}: shape={v.shape}, dtype={v.dtype}")
            else:
                print(f"  {k}: {v}")
    except Exception:
        # Fallback: prova come JSON
        with open(filepath) as f:
            data = json.load(f)
        frames = data.get("frames", [])
        print(f"  File: {filepath}")
        print(f"  Frame: {len(frames)}")
        print(f"  Label: {data.get('label', '?')}")
        if frames:
            amps = [f.get("ampl_std", 0) for f in frames if f.get("ampl_std") is not None]
            rssis = [f.get("rssi", 0) for f in frames if f.get("rssi") is not None]
            if amps:
                print(f"  ampl_std: mean={mean(amps):.2f}, std={stdev(amps):.2f}")
            if rssis:
                print(f"  rssi:     mean={mean(rssis):.2f}, std={stdev(rssis):.2f}")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="CSI Processor — ESP32 + UNO Q")
    parser.add_argument("--ping", action="store_true", help="Test connessione ESP32")
    parser.add_argument("--monitor", action="store_true", help="Monitor real-time")
    parser.add_argument("--calibrate", action="store_true",
                        help="Calibrazione (baseline + movement)")
    parser.add_argument("--seconds", type=int, default=30,
                        help="Secondi per fase di calibrazione/benchmark")
    parser.add_argument("--benchmark", type=str, metavar="LABEL",
                        help="Salva .mat per benchmark (es. --benchmark sit)")
    parser.add_argument("--analyze", type=str, help="Analizza file .mat/.json")
    parser.add_argument("--socket", default=SOCKET_PATH, help="Percorso Unix socket router")
    parser.add_argument("--out-dir", default=".",
                        help="Directory output per .mat / .json (default: .)")
    args = parser.parse_args()

    if args.analyze:
        cmd_analyze(args.analyze)
        return

    # Connessione al router
    client = RouterClient(args.socket)
    try:
        client.connect()
    except Exception as e:
        print(f"Errore connessione arduino-router: {e}")
        sys.exit(1)

    try:
        if args.ping:
            cmd_ping(client)
        elif args.monitor:
            cmd_monitor(client)
        elif args.benchmark:
            cmd_collect(client, args.seconds, args.benchmark, args.out_dir)
        elif args.calibrate:
            baseline = cmd_collect(client, args.seconds, "baseline", args.out_dir)
            input("\nPremi INVIO per iniziare la fase MOVEMENT...")
            movement = cmd_collect(client, args.seconds, "movement", args.out_dir)
            analyze_frames(baseline, movement)
        else:
            parser.print_help()
    finally:
        client.close()


if __name__ == "__main__":
    main()
