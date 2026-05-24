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

import socket, time, json, sys, os, re, argparse, struct, logging

logger = logging.getLogger(__name__)
# msgpack: import lazy, serve solo a RouterClient (UNO Q bridge RPC).
# Su Mac/host senza UNO Q importare csi_processor non deve richiedere msgpack.
from datetime import datetime
from collections import deque
from statistics import mean, stdev
from math import sqrt, atan2

# CSI ML Classifier: import lazy
try:
    from .csi_ml import CSIClassifier, CSI_CLASSES, CSI_LABELS, CSI_MODEL_PATH
    _CSI_ML_AVAILABLE = True
except ImportError:
    CSIClassifier = None
    CSI_CLASSES = {}
    CSI_LABELS = []
    CSI_MODEL_PATH = None
    _CSI_ML_AVAILABLE = False

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
        # Import msgpack on demand: chi non usa il bridge (es. csi_mac.py) non deve installarlo.
        global msgpack
        import msgpack  # noqa: F401
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

# Nuovo formato: CSI:<seq>:<mac_12hex>:<rssi>:<noise>:<rate>:<bw>:<sub_count>:<r0,i0,r1,i1,...>
_RE_CSI = re.compile(
    r"CSI:(\d+):(?:([0-9a-fA-F]{12}):)?(-?\d+):(-?\d+):(\d+):(\d+):(\d+):([\d,\-]*)"
)

# Multi-AP context (AP:<id> lines from firmware)
_RE_AP = re.compile(r"AP:(\d+)")
_RE_AP_SWITCH = re.compile(r"AP_SWITCH:(\d+)")
_AP_CONTEXT = 0

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

    global _AP_CONTEXT

    # AP context line (multi-AP mode)
    m_ap = _RE_AP.match(line)
    if m_ap:
        _AP_CONTEXT = int(m_ap.group(1))
        return {"ap_context": _AP_CONTEXT, "type": "ap_context"}

    # AP switch notification
    m_switch = _RE_AP_SWITCH.match(line)
    if m_switch:
        return {"ap_switch": int(m_switch.group(1)), "type": "ap_switch"}

    # --- Nuovo formato firmaware ESP32 ---
    m = _RE_CSI.match(line)
    if m:
        try:
            seq = int(m.group(1))
            mac_raw = m.group(2)  # 12-char hex o None se vecchio formato
            rssi = int(m.group(3))
            noise = int(m.group(4))
            rate = int(m.group(5))
            bw = int(m.group(6))
            sub_count = int(m.group(7))
            raw_numbers = m.group(8).split(",")

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
                "mac": mac_raw.upper() if mac_raw else None,
                "rssi": rssi,
                "noise_floor": noise,
                "rate": rate,
                "bandwidth": bw,
                "num_subcarriers": len(csi_data),
                "csi": csi_data,
                "ap_id": _AP_CONTEXT,
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

        result["ap_id"] = _AP_CONTEXT
        return result

    except Exception:
        return None


# ============================================================
# Parser frame binario ADR-018 (ispirato da RuView)
# ============================================================
# Formato:
#   [0..3]   Magic: 0xC5110001 (LE u32)
#   [4]      Node ID (u8)
#   [5]      Numero antenne (u8)
#   [6..7]   Numero subcarrier (LE u16)
#   [8..11]  Frequenza MHz (LE u32)
#   [12..15] Sequence number (LE u32)
#   [16]     RSSI (i8)
#   [17]     Noise floor (i8)
#   [18..19] Reserved
#   [20..]   I/Q pairs (i8, i8 per subcarrier)
CSI_BINARY_MAGIC = 0xC5110001
CSI_BINARY_HEADER_SIZE = 20

# ============================================================
# Radar3D cross-ping format — magic 0xC5110003 (LE u32)
#   [0..3]   Magic: 0xC5110003
#   [4]      TX node ID (0,1,2)        — chi ha trasmesso
#   [5]      RX node ID (0,1,2)        — chi sta ricevendo (NODE_ID locale)
#   [6..7]   Numero subcarrier (LE u16)
#   [8..11]  Sequence number (LE u32)
#   [12]     RSSI (i8)
#   [13]     Noise floor (i8)
#   [14..15] Reserved
#   [16..23] Timestamp microsecondi (LE i64)
#   [24..]   I/Q pairs (int8_t per subcarrier × 2 byte)
#
# 3 nodi che si pingano fra loro -> 9 coppie (tx_node, rx_node) -> 9 canali CSI.
# A differenza del formato ADR-018, qui la "sorgente" del canale e' la COPPIA
# (tx, rx), entrambe stabili (NODE_ID hardcoded, no MAC randomization).
CSI_RADAR3D_MAGIC = 0xC5110003
CSI_RADAR3D_HEADER_SIZE = 24


def parse_csi_radar3d(data: bytes) -> dict | None:
    """Parser per frame Radar 3D cross-ping (magic 0xC5110003).

    Restituisce un dict frame compatibile con CSIClassifier / BlobRegressor:
    - `mac` viene riempito con `pair_id` = "rx{rx}-tx{tx}" cosi' che le
      pipeline esistenti (che raggruppano per `source_id` o `mac`)
      vedono 9 sorgenti stabili.
    - `ap_id` = rx_node (ricevitore), per compatibilita' Multi-AP classifier.

    Args:
        data: bytes del frame (min 24 byte header + I/Q pairs).

    Returns:
        dict con campi CSI, o None se formato non valido.
    """
    if len(data) < CSI_RADAR3D_HEADER_SIZE:
        return None
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic != CSI_RADAR3D_MAGIC:
        return None

    tx_node = data[4]
    rx_node = data[5]
    n_sub = struct.unpack_from("<H", data, 6)[0]
    seq = struct.unpack_from("<I", data, 8)[0]
    rssi = struct.unpack_from("<b", data, 12)[0]
    noise = struct.unpack_from("<b", data, 13)[0]
    # data[14..15] = reserved
    ts_us = struct.unpack_from("<q", data, 16)[0]

    if n_sub < 1 or n_sub > 512:
        return None
    expected_iq = n_sub * 2  # 1 I + 1 Q per subcarrier (single antenna)
    if len(data) < CSI_RADAR3D_HEADER_SIZE + expected_iq:
        return None

    csi_data = []
    for i in range(n_sub):
        offset = CSI_RADAR3D_HEADER_SIZE + i * 2
        real_v = float(struct.unpack_from("<b", data, offset)[0])
        imag_v = float(struct.unpack_from("<b", data, offset + 1)[0])
        ampl = sqrt(real_v ** 2 + imag_v ** 2)
        phase = atan2(imag_v, real_v)
        csi_data.append({
            "subcarrier": i,
            "real": real_v,
            "imag": imag_v,
            "ampl": round(ampl, 3),
            "phase": round(phase, 4),
        })

    amps = [c["ampl"] for c in csi_data]

    # Identificatore stabile della COPPIA tx->rx. Le pipeline a valle (che
    # raggruppano per source_id/mac) lo trattano come fosse un singolo
    # "trasmettitore virtuale" stabile.
    pair_id = f"rx{rx_node}-tx{tx_node}"

    return {
        "seq": seq,
        "mac": pair_id,           # stable, no randomization
        "source_id": pair_id,     # preferito da CSIClassifier.train_custom
        "rssi": rssi,
        "noise_floor": noise,
        "rate": 0,
        "bandwidth": 20,
        "num_subcarriers": n_sub,
        "csi": csi_data,
        "ampl_mean": round(mean(amps), 3),
        "ampl_std": round(stdev(amps), 3) if len(amps) >= 2 else 0,
        "ampl_max": round(max(amps), 3),
        "ampl_min": round(min(amps), 3),
        "ap_id": rx_node,         # per compatibilita' MultiAPCSIClassifier
        "tx_node": tx_node,
        "rx_node": rx_node,
        "_radar3d": True,
        "_node_id": rx_node,
        "_pair_id": pair_id,
        "_timestamp_us": ts_us,
    }


def parse_csi_binary(data: bytes) -> dict | None:
    """Parser per frame CSI binario in formato ADR-018.

    Args:
        data: bytes del frame (min 20 byte header + I/Q pairs).

    Returns:
        dict con campi CSI, o None se formato non valido.
    """
    if len(data) < CSI_BINARY_HEADER_SIZE:
        return None

    magic = struct.unpack_from("<I", data, 0)[0]
    if magic != CSI_BINARY_MAGIC:
        return None

    node_id = data[4]
    n_antennas = data[5]
    n_sub = struct.unpack_from("<H", data, 6)[0]
    freq_mhz = struct.unpack_from("<I", data, 8)[0]
    seq = struct.unpack_from("<I", data, 12)[0]
    rssi = struct.unpack_from("<b", data, 16)[0]
    noise = struct.unpack_from("<b", data, 17)[0]

    # Validazione
    if n_sub < 1 or n_sub > 512:
        return None
    expected_iq = n_sub * n_antennas * 2
    if len(data) < CSI_BINARY_HEADER_SIZE + expected_iq:
        return None

    # Leggi I/Q pairs
    csi_data = []
    for i in range(n_sub):
        offset = CSI_BINARY_HEADER_SIZE + i * 2
        real_v = float(struct.unpack_from("<b", data, offset)[0])
        imag_v = float(struct.unpack_from("<b", data, offset + 1)[0])
        ampl = sqrt(real_v ** 2 + imag_v ** 2)
        phase = atan2(imag_v, real_v)
        csi_data.append({
            "subcarrier": i,
            "real": real_v,
            "imag": imag_v,
            "ampl": round(ampl, 3),
            "phase": round(phase, 4),
        })

    amps = [c["ampl"] for c in csi_data]

    result = {
        "seq": seq,
        "mac": None,
        "rssi": rssi,
        "noise_floor": noise,
        "rate": 0,
        "bandwidth": 20,
        "num_subcarriers": n_sub,
        "csi": csi_data,
        "ampl_mean": round(mean(amps), 3),
        "ampl_std": round(stdev(amps), 3) if len(amps) >= 2 else 0,
        "ampl_max": round(max(amps), 3),
        "ampl_min": round(min(amps), 3),
        "ap_id": node_id,  # per compatibilità MultiAPCSIClassifier
        "_binary": True,
        "_node_id": node_id,
        "_n_antennas": n_antennas,
        "_freq_mhz": freq_mhz,
    }
    return result


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
        logger.debug("csi_clear fallito (normale se nessun dato in buffer)")

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
        logger.debug("Errore durante raccolta frame (timeout o interruzione)")

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


def cmd_monitor(client, use_ml: bool = False, ml_model_path: str | None = None):
    ml_clf = None
    if use_ml:
        if not _CSI_ML_AVAILABLE or CSIClassifier is None:
            print("  [ML] sklearn non installato. Uso modalità raw.")
            use_ml = False
        else:
            assert CSIClassifier is not None
            ml_clf = CSIClassifier(window_frames=30)
            model_file = ml_model_path or CSI_MODEL_PATH
            if model_file and os.path.exists(model_file):
                ml_clf.load(model_file)
            else:
                print(f"  Modello ML non trovato in: {model_file}")
                print("  Uso modalità raw. Esegui --calibrate --train-ml prima.")
                use_ml = False

    if use_ml:
        print("CSI ML Monitor — Ctrl+C per uscire\n")
        print(f"{'t(s)':>6} {'RSSI':>6} {'EMPTY':>7} {'STILL':>7} {'MOTION':>7} {'Classe':>14}")
        print("-" * 55)
    else:
        print("CSI Monitor — Ctrl+C per uscire\n")
        print(f"{'t(s)':>8} {'seq':>6} {'subc':>5} {'ampl_mean':>10} {'ampl_std':>9} {'RSSI':>6}")
        print("-" * 55)

    try:
        client.call("csi_clear")
    except Exception:
        logger.debug("csi_clear fallito (normale se nessun dato in buffer)")

    start = time.time()
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
                            parsed["_t"] = round(time.time() - start, 3)
                            t = parsed["_t"]

                            if use_ml:
                                assert ml_clf is not None
                                ml_clf.add_frame(parsed)
                                probas = ml_clf.predict_proba()
                                cls = ml_clf.predict()
                                print(f"{t:>6.1f} "
                                      f"{parsed.get('rssi', 0):>6} "
                                      f"{probas.get('EMPTY', 0):>7.3f} "
                                      f"{probas.get('STILL', 0):>7.3f} "
                                      f"{probas.get('MOTION', 0):>7.3f} "
                                      f"{cls:>14}")
                            else:
                                print(f"{t:>8.1f} "
                                      f"{parsed.get('seq', 0):>6} "
                                      f"{parsed.get('num_subcarriers', 0):>5} "
                                      f"{parsed.get('ampl_mean', 0):>10.2f} "
                                      f"{parsed.get('ampl_std', 0):>9.2f} "
                                       f"{parsed.get('rssi', 0):>6}")
            except Exception:
                logger.debug("Errore parsing frame in monitor live", exc_info=True)
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


def cmd_train_csi_ml(client, seconds: int, out_dir: str, stationary_seconds: int = 0):
    """Raccoglie dati EMPTY + MOTION (opz. STILL) e addestra CSIClassifier."""
    if not _CSI_ML_AVAILABLE or CSIClassifier is None:
        print("\n  [ML] sklearn non installato. Installa con:")
        print("    UNO Q: sudo apt install python3-sklearn python3-joblib")
        print("    Mac:   pip install scikit-learn joblib")
        return

    print(f"\n{'='*60}")
    print(f"  TRAINING CSI ML CLASSIFIER")
    print(f"{'='*60}")

    # Fase 1: EMPTY
    input("\nFase 1/3: STANZA VUOTA (allontanati). Premi INVIO...")
    empty = collect_csi(client, seconds, "baseline", out_dir)

    # Fase 2: STILL (opzionale)
    stationary = None
    if stationary_seconds > 0:
        input(f"\nFase 2/3: SEDUTO FERMO (respiro normale). Premi INVIO...")
        stationary = collect_csi(client, stationary_seconds, "stationary", out_dir)

    # Fase 3: MOTION
    n_phase = "3/4" if stationary else "2/3"
    next_phase = "4/4" if stationary else "3/3"
    input(f"\nFase {next_phase}: CAMMINA NELLA STANZA. Premi INVIO...")
    movement = collect_csi(client, seconds, "movement", out_dir)

    print(f"\n  Training CSIClassifier...")
    print(f"    EMPTY:      {len(empty)} frame")
    if stationary:
        print(f"    STILL: {len(stationary)} frame")
    print(f"    MOTION:   {len(movement)} frame")

    try:
        clf = CSIClassifier(window_frames=30)
        metrics = clf.train(empty, stationary, movement)
        clf.save()

        print(f"\n  Metriche training:")
        print(f"    Campioni: {metrics['n_train']}")
        print(f"    Feature:  {metrics['n_features']}")
        print(f"    Classi:   {metrics['n_classes']}")

        # Salva anche i frame come JSON per rianalisi
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        for label, frames in [("empty", empty), ("stationary", stationary), ("movement", movement)]:
            if frames:
                path = os.path.join(out_dir, f"CSI_ML_{label}_{ts}.json")
                with open(path, "w") as f:
                    json.dump({"label": label, "frames": frames}, f, indent=2)
                print(f"    Salvato: {path}")

        return clf
    except Exception as e:
        print(f"\n  ERRORE training ML: {e}")
        return None


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
    parser.add_argument("--use-ml", action="store_true",
                        help="Usa CSIClassifier (ML) in modalità monitor")
    parser.add_argument("--train-ml", action="store_true",
                        help="Addestra CSIClassifier dopo la calibrazione")
    parser.add_argument("--ml-model", type=str, default=None,
                        help="Percorso modello CSI .joblib (default: csi_model.joblib)")
    parser.add_argument("--stationary-seconds", type=int, default=0,
                        help="Secondi per fase STILL (0=salta, default: 0)")
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
            cmd_monitor(client, use_ml=args.use_ml, ml_model_path=args.ml_model)
        elif args.benchmark:
            cmd_collect(client, args.seconds, args.benchmark, args.out_dir)
        elif args.calibrate:
            baseline = cmd_collect(client, args.seconds, "baseline", args.out_dir)
            input("\nPremi INVIO per iniziare la fase MOTION...")
            movement = cmd_collect(client, args.seconds, "movement", args.out_dir)
            analyze_frames(baseline, movement)

            if args.train_ml:
                print("\nTraining ML Classifier sui dati raccolti...")
                if not _CSI_ML_AVAILABLE or CSIClassifier is None:
                    print("  sklearn non installato. pip install scikit-learn joblib")
                else:
                    assert CSIClassifier is not None
                    stationary = None
                    if args.stationary_seconds > 0:
                        input(f"\nFase STILL: siediti fermo. Premi INVIO...")
                        stationary = cmd_collect(client, args.stationary_seconds, "stationary", args.out_dir)
                    try:
                        clf = CSIClassifier(window_frames=30)
                        clf.train(baseline, stationary, movement)
                        save_path = args.ml_model or CSI_MODEL_PATH or "csi_model.joblib"
                        clf.save(save_path)
                    except Exception as e:
                        print(f"ERRORE training ML: {e}")
        elif args.train_ml:
            cmd_train_csi_ml(client, args.seconds, args.out_dir, args.stationary_seconds)
        else:
            parser.print_help()
    finally:
        client.close()


if __name__ == "__main__":
    main()
