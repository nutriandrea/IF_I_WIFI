#!/usr/bin/env python3
"""
seat_mapper.py — CSI fingerprint per sedie in aula.

Addestra un Random Forest a riconoscere quale sedia è occupata usando
CSI multi-AP (3 AP). Training interattivo: persona si siede su ogni
sedia per N secondi. Live: predice in tempo reale e invia a browser.

Demo in aula:
  # Training (una volta, prima della demo)
  python3 -m csi.seat_mapper --mode train --num-seats 10 --seconds 30

  # Live prediction con WebSocket (demo)
  python3 -m csi.seat_mapper --mode live --port 8080
  # Browser → http://localhost:8080/classroom_heatmap.html

Architettura:
  SeatClassifier — estende il pattern MultiAPCSIClassifier con N classi
  arbitrarie (sedie + EMPTY). Feature vector = feature_ap0(81) +
  feature_ap1(81) + feature_ap2(81) per un totale di 243 feature.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from statistics import mean, stdev
from typing import Optional

# ── CSI pipeline già esistente ────────────────────────────
from .csi_processor import parse_csi_line
from .csi_ml import (
    CSI_FEATURE_SIZE,
    csi_window_to_vector,
)

# ── Dipendenze opzionali ──────────────────────────────────

_SERIAL_AVAILABLE = False
try:
    import serial
    _SERIAL_AVAILABLE = True
except ImportError:
    serial = None  # type: ignore

_WS_AVAILABLE = False
try:
    import asyncio
    import websockets
    _WS_AVAILABLE = True
except ImportError:
    asyncio = None  # type: ignore
    websockets = None  # type: ignore

_SKLEARN_AVAILABLE = False
try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import accuracy_score, confusion_matrix
    _SKLEARN_AVAILABLE = True
except ImportError:
    RandomForestClassifier = None  # type: ignore

_JOBBLIB_AVAILABLE = False
try:
    import joblib
    _JOBBLIB_AVAILABLE = True
except ImportError:
    joblib = None  # type: ignore

# BLE reader (opzionale)
_BLE_READER_AVAILABLE = False
try:
    from .csi_ble import ble_reader as _ble_reader_func
    _BLE_READER_AVAILABLE = True
except ImportError:
    _ble_reader_func = None  # type: ignore

# ── Costanti ──────────────────────────────────────────────

MODEL_DIR = os.path.dirname(os.path.abspath(__file__))
SEAT_MODEL_PATH = os.path.join(MODEL_DIR, "seat_model.joblib")
SEAT_FINGERPRINT_PATH = os.path.join(MODEL_DIR, "seat_fingerprints.json")

NUM_SUBCARRIERS = 64
NUM_APS = 3
FEATURES_PER_AP = CSI_FEATURE_SIZE  # 81
TOTAL_FEATURES = FEATURES_PER_AP * NUM_APS  # 243

# Porte di default
DEFAULT_WS_PORT = 8765
DEFAULT_HTTP_PORT = 8080
DEFAULT_BAUD = 921600

LABEL_EMPTY = "EMPTY"

# ============================================================
# SeatClassifier
# ============================================================

class SeatClassifier:
    """
    Random Forest multi-classe per riconoscimento sedia occupata da CSI.

    Mantiene 3 buffer separati (uno per AP), estrae feature per-AP
    e concatena in un vettore da TOTAL_FEATURES (= 1152).

    Usage:
        clf = SeatClassifier(window_frames=30, num_aps=3)

        # Training
        clf.train({"EMPTY": frames_empty,
                    "S0": frames_s0, "S1": frames_s1, ...})
        clf.save()

        # Live inference
        clf.add_frame(csi_frame_dict)
        if clf.ready:
            probs = clf.predict_proba()  # {"EMPTY": 0.1, "S0": 0.8, ...}
            label = clf.predict()        # "S0"
    """

    def __init__(self, window_frames: int = 30, num_aps: int = NUM_APS):
        self.window_size = window_frames
        self.num_aps = num_aps
        self.ap_windows: dict = {
            i: deque(maxlen=window_frames) for i in range(num_aps)
        }
        self._model = None
        self._classes: list[str] = []
        self._trained = False
        self._last_probas: dict[str, float] = {}
        self._last_label: str = "UNKNOWN"
        self._feature_importance: dict = {}

    # ── Proprietà ──────────────────────────────────────────

    @property
    def ready(self) -> bool:
        """Pronto per inferenza: tutti gli AP hanno finestra piena e modello
        addestrato."""
        if not self._trained or self._model is None:
            return False
        return all(len(w) == self.window_size for w in self.ap_windows.values())

    @property
    def trained(self) -> bool:
        return self._trained

    # ── Data ingestion ─────────────────────────────────────

    def add_frame(self, frame: dict):
        """Aggiunge frame CSI. ap_id determina il buffer."""
        ap_id = frame.get("ap_id", 0)
        if 0 <= ap_id < self.num_aps:
            self.ap_windows[ap_id].append(frame)

    def _build_feature_vector(self) -> list | None:
        """Concatena feature vector da tutti gli AP (1152 feature)."""
        vec: list[float] = []
        for ap_id in range(self.num_aps):
            window = list(self.ap_windows[ap_id])
            if len(window) < 2:
                return None
            ap_vec = csi_window_to_vector(window)
            if ap_vec is None:
                return None
            vec.extend(ap_vec)
        if len(vec) != TOTAL_FEATURES:
            return None
        return vec

    # ── Training ───────────────────────────────────────────

    @staticmethod
    def _check_sklearn():
        if not _SKLEARN_AVAILABLE:
            raise RuntimeError(
                "scikit-learn non installato.\n"
                "  pip install scikit-learn joblib"
            )

    def train(self, labeled_frames: dict[str, list]) -> dict:
        """Addestra il Random Forest multi-classe.

        Args:
            labeled_frames: dict {label_str: [list_of_CSI_frames]}
                            Es: {"EMPTY": [...], "S0": [...], "S1": [...]}

        Returns:
            dict con metriche di training.
        """
        self._check_sklearn()
        assert RandomForestClassifier is not None

        if len(labeled_frames) < 2:
            raise ValueError("Servono almeno 2 classi (es. EMPTY + almeno una sedia)")

        X: list[list[float]] = []
        y: list[str] = []

        for label, frames in labeled_frames.items():
            if not frames:
                print(f"  ⚠ Classe '{label}' non ha frame, saltata")
                continue

            # Per-AP buffer per estrarre feature multi-AP
            ap_buf: dict = {
                i: deque(maxlen=self.window_size) for i in range(self.num_aps)
            }
            count = 0

            for frame in frames:
                ap_id = frame.get("ap_id", 0)
                if 0 <= ap_id < self.num_aps:
                    ap_buf[ap_id].append(frame)

                # Quando tutti gli AP hanno finestra piena, estrai feature
                if all(len(w) == self.window_size for w in ap_buf.values()):
                    vec: list[float] = []
                    for aid in range(self.num_aps):
                        av = csi_window_to_vector(list(ap_buf[aid]))
                        if av:
                            vec.extend(av)
                    if len(vec) == TOTAL_FEATURES:
                        X.append(vec)
                        y.append(label)
                        count += 1

            print(f"  {label:>8}: {count} feature vector")

        if len(X) < 10:
            raise ValueError(
                f"Pochi feature vector: {len(X)} (servono >=10). "
                f"Servono almeno {self.window_size} frame per ciascuno dei "
                f"{self.num_aps} AP."
            )

        print(f"\n  Feature matrix: {len(X)} righe × {len(X[0])} feature")
        print(f"  Classi: {sorted(set(y))}")

        # Addestra
        self._model = RandomForestClassifier(
            n_estimators=100,
            max_depth=12,
            class_weight="balanced",
            random_state=42,
            n_jobs=1,
        )
        self._model.fit(X, y)
        self._trained = True
        self._classes = sorted(set(y))

        # Metriche
        y_pred = self._model.predict(X)
        acc = accuracy_score(y, y_pred)
        print(f"  Accuracy (train set): {acc:.3f}")
        print(f"\n  Matrice di confusione:\n{confusion_matrix(y, y_pred)}")

        # Feature importance
        if hasattr(self._model, "feature_importances_") and self._model.feature_importances_ is not None:
            fi_names = self._generate_feature_names()
            self._feature_importance = dict(
                sorted(
                    (
                        (n, round(v, 4))
                        for n, v in zip(fi_names, self._model.feature_importances_)
                    ),
                    key=lambda kv: -kv[1],
                )
            )

        # Stampa top-15 feature importance con label AP
        print(f"\n  Top-15 feature importance:")
        for name, imp in list(self._feature_importance.items())[:15]:
            bar = "█" * max(1, int(imp * 60))
            print(f"    {name:>36}: {imp:.4f}  {bar}")

        metrics = {
            "n_train": len(X),
            "n_features": len(X[0]),
            "n_classes": len(self._classes),
            "classes": self._classes,
            "accuracy": round(acc, 4),
            "feature_importance": dict(list(self._feature_importance.items())[:15]),
        }
        return metrics

    @staticmethod
    def _generate_feature_names() -> list[str]:
        """Genera nomi feature per 3 AP × 384 feature/AP."""
        names: list[str] = []
        for ap in range(NUM_APS):
            for feat_idx in range(CSI_FEATURE_SIZE):
                names.append(f"AP{ap}_F{feat_idx}")
        return names

    # ── Persistence ────────────────────────────────────────

    def save(self, path: str = SEAT_MODEL_PATH) -> str:
        """Salva modello e classi."""
        if not self._trained:
            raise RuntimeError("Modello non addestrato, chiama train() prima")
        if not _JOBBLIB_AVAILABLE:
            raise RuntimeError("joblib non installato (pip install joblib)")

        model_data = {
            "model": self._model,
            "classes": self._classes,
            "window_size": self.window_size,
            "num_aps": self.num_aps,
        }
        joblib.dump(model_data, path)
        print(f"  [SeatClassifier] Modello salvato: {path}")
        return path

    def load(self, path: str = SEAT_MODEL_PATH) -> bool:
        """Carica modello."""
        if not _JOBBLIB_AVAILABLE:
            raise RuntimeError("joblib non installato (pip install joblib)")
        if not os.path.exists(path):
            print(f"  [SeatClassifier] Modello non trovato: {path}")
            return False

        model_data = joblib.load(path)
        self._model = model_data["model"]
        self._classes = model_data["classes"]
        self.window_size = model_data.get("window_size", self.window_size)
        self.num_aps = model_data.get("num_aps", self.num_aps)
        # Riallinea buffer
        self.ap_windows = {
            i: deque(maxlen=self.window_size) for i in range(self.num_aps)
        }
        self._trained = True
        print(f"  [SeatClassifier] Modello caricato: {path}")
        print(f"  Classi: {self._classes}, window={self.window_size}, AP={self.num_aps}")
        return True

    # ── Inference ──────────────────────────────────────────

    def predict_proba(self) -> dict[str, float]:
        """Probabilità per ogni classe (sedia + EMPTY)."""
        if not self.ready:
            return {}

        assert self._model is not None
        vec = self._build_feature_vector()
        if vec is None:
            return {}

        probas = self._model.predict_proba([vec])[0]
        result: dict[str, float] = {}
        for i, cls_name in enumerate(self._model.classes_):
            result[str(cls_name)] = round(float(probas[i]), 4)

        self._last_probas = result
        return result

    def predict(self) -> str:
        """Classe predetta."""
        if not self.ready:
            return "UNKNOWN"
        assert self._model is not None
        vec = self._build_feature_vector()
        if vec is None:
            return "UNKNOWN"
        self._last_label = str(self._model.predict([vec])[0])
        return self._last_label

    def get_info(self) -> dict:
        return {
            "trained": self._trained,
            "window_size": self.window_size,
            "num_aps": self.num_aps,
            "classes": self._classes,
            "buffers_ready": [len(w) for w in self.ap_windows.values()],
            "ready": self.ready,
            "last_prediction": self._last_label,
            "last_probas": dict(self._last_probas),
        }


# ============================================================
# Serial reader
# ============================================================

def _autodetect_port() -> Optional[str]:
    """Autodetect ESP32 serial port (USB or Bluetooth)."""
    import glob as _glob
    patterns = [
        "/dev/cu.ESP32_CSI*",           # Bluetooth SPP
        "/dev/cu.ESP32*Bluetooth*",
        "/dev/cu.usbserial*",           # USB serial (CH340/CP210x)
        "/dev/cu.usbmodem*",            # Arduino UNO Q / board USB
        "/dev/cu.wchusbserial*",
        "/dev/ttyUSB*",
        "/dev/ttyACM*",
        "/dev/cu.SLAB*",
    ]
    for pattern in patterns:
            candidates = sorted(_glob.glob(pattern))
            if candidates:
                return candidates[0]
    return None


def serial_reader(port: str, baud: int, callback, stop_event: threading.Event):
    """Legge CSI da seriale in un thread separato.

    Args:
        port: path della porta seriale
        baud: baud rate
        callback: chiamata per ogni frame CSI parsato (dict)
        stop_event: threading.Event per fermare
    """
    if serial is None:
        print("  pyserial non installato. Impossibile leggere da seriale.")
        return

    try:
        ser = serial.Serial(port, baud, timeout=0.1)
        print(f"  [Serial] Aperta {port} @ {baud}")
    except serial.SerialException as e:
        print(f"  [Serial] ERRORE: {e}")
        return

    try:
        while not stop_event.is_set():
            try:
                line = ser.readline().decode("utf-8", errors="replace").strip()
            except serial.SerialException:
                break
            if not line:
                continue
            parsed = parse_csi_line(line)
            if parsed and "csi" in parsed:
                callback(parsed)
    finally:
        ser.close()
        print("  [Serial] Chiusa")


# ============================================================
# Record da file (replay)
# ============================================================

def file_reader(filepath: str, callback, stop_event: threading.Event,
                speed: float = 1.0):
    """Legge CSI da file registrato (replay)."""
    lines = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line:
                lines.append(line)

    print(f"  [Replay] {len(lines)} righe da {filepath}")
    timestamps: list[float] = []
    data_lines: list[str] = []

    # Separa timestamp da line
    for line in lines:
        # CSI line
        if line.startswith("CSI:") or line.startswith("CSI_DATA"):
            data_lines.append(line)
        elif line.startswith("#AP") or line.startswith("#SWITCH"):
            data_lines.append(line)
        else:
            data_lines.append(line)

    t0 = time.time()
    for i, line in enumerate(data_lines):
        if stop_event.is_set():
            break

        parsed = parse_csi_line(line)
        if parsed and "csi" in parsed:
            callback(parsed)

        # Rate limiting simulato (~40 Hz)
        elapsed = (time.time() - t0) / speed
        target = i / 40.0
        if target > elapsed:
            time.sleep(target - elapsed)

    print(f"  [Replay] Completato ({len(data_lines)} linee)")


# ============================================================
# Collettore di training interattivo
# ============================================================

class SeatCollector:
    """
    Collezione interattiva di CSI per ogni sedia.

    Usage:
        collector = SeatCollector(num_seats=10, seconds=30)
        collector.collect_all()
        # → returns {"EMPTY": [...], "S0": [...], "S1": [...], ...}
    """

    def __init__(self, num_seats: int, seconds: int = 30,
                 port: Optional[str] = None, baud: int = DEFAULT_BAUD,
                 use_ble: bool = False):
        self.num_seats = num_seats
        self.seconds = seconds
        self.port = port
        self.baud = baud
        self.use_ble = use_ble
        self._frames: dict[str, list] = {}
        self._current_buffer: list[dict] = []

    def _start_collection(self, label: str):
        """Avvia collezione per una classe."""
        self._current_buffer = []
        stop_event = threading.Event()

        if self.use_ble:
            if not _BLE_READER_AVAILABLE:
                print("  ERRORE: bleak non installato. pip install bleak")
                return
            reader = threading.Thread(
                target=_ble_reader_func,
                args=("ESP32_CSI",
                      lambda line: self._on_csi_line(line),
                      stop_event),
                daemon=True,
            )
            reader.start()
        elif self.port:
            reader = threading.Thread(
                target=serial_reader,
                args=(self.port, self.baud,
                      lambda f: self._current_buffer.append(f),
                      stop_event),
                daemon=True,
            )
            reader.start()
        else:
            print("  ERRORE: nessuna porta seriale specificata. Usa --port o --ble")
            return

        # Attesa progress bar + stop
        self._collection_progress(self.seconds, stop_event, self._current_buffer)
        self._frames[label] = list(self._current_buffer)
        print(f"  ✅ Collezionati {len(self._frames[label])} frame per '{label}'")

    def _on_csi_line(self, line: str):
        """Callback per linea CSI da BLE."""
        parsed = parse_csi_line(line)
        if parsed and "csi" in parsed:
            self._current_buffer.append(parsed)

    @staticmethod
    def _collection_progress(seconds: int, stop_event, buffer: list) -> None:
        """Mostra progress bar e ferma reader allo scadere."""
        print(f"\n  📡 Collezione per {seconds}s...")
        progress_interval = max(1, seconds // 10)

        for remaining in range(seconds, 0, -1):
            if remaining % progress_interval == 0 or remaining <= 3:
                print(f"    ⏳ {remaining}s  (frames: {len(buffer)})")
            time.sleep(1)

        stop_event.set()
        time.sleep(0.2)

    def collect_all(self) -> dict[str, list]:
        """Collezione interattiva guidata."""
        print(f"\n{'='*60}")
        print(f"  TRAINING SEAT MAPPER")
        print(f"  Sedie: {self.num_seats}  Durata: {self.seconds}s per sedia")
        print(f"{'='*60}\n")

        self.port = self.port or _autodetect_port()
        if not self.port:
            print("  Porta seriale non trovata. Specifica con --port")
            print("  Oppure usa --mode replay per usare un file registrato.")
            sys.exit(1)

        # 1. EMPTY (stanza vuota)
        input(f"  ▶ Sgombera l'aula. Premi Invio per collezionare {LABEL_EMPTY}...")
        self._start_collection(LABEL_EMPTY)

        # 2. Per ogni sedia
        for seat_idx in range(self.num_seats):
            label = f"S{seat_idx}"
            input(f"\n  ▶ Posiziona persona sulla sedia {label} e premi Invio...")
            self._start_collection(label)

        print(f"\n{'='*60}")
        print(f"  Raccolta completa: {len(self._frames)} classi")
        for label, frames in self._frames.items():
            print(f"    {label}: {len(frames)} frame")
        print(f"{'='*60}")

        return self._frames


# ============================================================
# WebSocket + HTTP server
# ============================================================

SEAT_RESULTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "mapping"
)


def _start_http_server(http_port: int, directory: str):
    """Avvia HTTP server in thread separato."""
    class _Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)
        def log_message(self, fmt, *args):
            pass

    server = HTTPServer(("0.0.0.0", http_port), _Handler)
    print(f"  HTTP:    http://localhost:{http_port}/classroom_heatmap.html")
    server.serve_forever()


async def _ws_handler(websocket):
    """Handler WebSocket — accetta connessioni."""
    async for message in websocket:
        if message == "ping":
            await websocket.send(json.dumps({"type": "pong"}))


async def _ws_broadcast(server, data: dict):
    """Broadcast JSON a tutti i client WebSocket."""
    if not server or not hasattr(server, "websockets"):
        return
    msg = json.dumps(data)
    dead: list = []
    for ws in server.websockets:
        try:
            await ws.send(msg)
        except websockets.exceptions.ConnectionClosed:
            dead.append(ws)
    for ws in dead:
        server.websockets.remove(ws)


async def _run_live_server(args):
    """Loop principale live: seriale → classifier → WebSocket."""
    # HTTP server per file statici
    http_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "mapping"
    )
    http_thread = threading.Thread(
        target=_start_http_server,
        args=(args.http_port, http_dir),
        daemon=True,
    )
    http_thread.start()

    # Carica modello
    model_path = args.model or SEAT_MODEL_PATH
    clf = SeatClassifier()
    if not clf.load(model_path):
        print("  ERRORE: caricamento modello fallito. Addestra prima con --mode train")
        sys.exit(1)

    print(f"  Classi: {clf._classes}")
    print(f"  Finestra: {clf.window_size} frame × {clf.num_aps} AP")

    # Data source: BLE / seriale / file replay
    stop_event = threading.Event()
    frame_queue: deque = deque(maxlen=5000)

    def _on_frame(frame: dict):
        frame_queue.append(frame)

    if args.replay:
        print(f"  Replay: {args.replay}")
        reader_thread = threading.Thread(
            target=file_reader,
            args=(args.replay, _on_frame, stop_event, args.speed),
            daemon=True,
        )
    elif args.ble:
        if not _BLE_READER_AVAILABLE:
            print("  ERRORE: bleak non installato. pip install bleak")
            sys.exit(1)
        print("  BLE: ESP32_CSI")
        def _ble_callback(line: str):
            parsed = parse_csi_line(line)
            if parsed and "csi" in parsed:
                _on_frame(parsed)
        reader_thread = threading.Thread(
            target=_ble_reader_func,
            args=("ESP32_CSI", _ble_callback, stop_event),
            daemon=True,
        )
    elif args.port or _autodetect_port():
        port = args.port or _autodetect_port() or ""
        print(f"  Seriale: {port} @ {args.baud}")
        reader_thread = threading.Thread(
            target=serial_reader,
            args=(port, args.baud, _on_frame, stop_event),
            daemon=True,
        )
    else:
        print("  Porta seriale non trovata. Usa --port, --ble o --replay")
        sys.exit(1)

    reader_thread.start()

    # WebSocket server
    ws_server = None
    if _WS_AVAILABLE:
        ws_server = await websockets.serve(
            _ws_handler, "0.0.0.0", args.ws_port,
            ping_interval=30, ping_timeout=10,
        )
        print(f"  WebSocket: ws://localhost:{args.ws_port}")
    else:
        print("  websockets non installato: nessuna UI browser disponibile")
        print("  pip install websockets")

    print(f"\n  🟢 Live! Premi Ctrl+C per fermare\n")

    # Loop di inferenza
    last_prediction = 0.0
    predict_interval = 1.0 / max(args.rate, 0.1)

    try:
        while True:
            # Svuota coda nel classifier
            while frame_queue:
                clf.add_frame(frame_queue.popleft())

            # Predizione periodica
            now = time.time()
            if now - last_prediction >= predict_interval and clf.ready:
                last_prediction = now

                label = clf.predict()
                probas = clf.predict_proba()

                # Prendi ultimo frame per RSSI
                last_frame = None
                if clf.ap_windows:
                    for ap_id in range(clf.num_aps):
                        w = list(clf.ap_windows[ap_id])
                        if w:
                            last_frame = w[-1]

                result = {
                    "type": "seat_prediction",
                    "prediction": label,
                    "confidence": max(probas.values()) if probas else 0.0,
                    "probabilities": probas,
                    "ap_id": last_frame.get("ap_id", -1) if last_frame else -1,
                    "rssi": last_frame.get("rssi", 0) if last_frame else 0,
                    "timestamp": now,
                }

                if ws_server:
                    await _ws_broadcast(ws_server, result)

            await asyncio.sleep(0.05)

    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        stop_event.set()
        if ws_server:
            ws_server.close()
        print("\n  Fermato.")


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Seat Mapper — CSI fingerprinting per sedie in aula"
    )

    # Modalità
    parser.add_argument(
        "--mode", choices=["train", "live", "info"],
        required=True,
        help="train: colleziona dati per ogni sedia e addestra. "
             "live: predizione in tempo reale con WebSocket. "
             "info: mostra info modello salvato."
    )

    # Parametri training
    parser.add_argument("--num-seats", type=int, default=10,
                        help="Numero di sedie (default: 10)")
    parser.add_argument("--seconds", type=int, default=30,
                        help="Secondi di collezione per sedia (default: 30)")
    parser.add_argument("--model", type=str, default=SEAT_MODEL_PATH,
                        help="Percorso modello (default: seat_model.joblib)")
    parser.add_argument("--window", type=int, default=30,
                        help="Frame per finestra (default: 30)")

    # Parametri seriale
    parser.add_argument("--port", type=str, default=None,
                        help="Porta seriale (es. /dev/cu.usbserial-XXX)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD,
                        help=f"Baud rate (default: {DEFAULT_BAUD})")
    parser.add_argument("--ble", action="store_true",
                        help="Usa BLE invece di seriale (ESP32 BLE firmware)")

    # Live mode
    parser.add_argument("--ws-port", type=int, default=DEFAULT_WS_PORT,
                        help=f"WebSocket port (default: {DEFAULT_WS_PORT})")
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT,
                        help=f"HTTP port (default: {DEFAULT_HTTP_PORT})")
    parser.add_argument("--rate", type=float, default=3.0,
                        help="Predizioni al secondo (default: 3.0)")
    parser.add_argument("--replay", type=str, default=None,
                        help="File registrato per replay (invece di seriale)")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="Velocità replay (default: 1.0)")

    args = parser.parse_args()

    if args.mode == "train":
        # Collezione interattiva
        collector = SeatCollector(
            num_seats=args.num_seats,
            seconds=args.seconds,
            port=args.port,
            baud=args.baud,
            use_ble=args.ble,
        )
        labeled = collector.collect_all()

        # Training
        print(f"\n  Addestramento RandomForest...")
        clf = SeatClassifier(window_frames=args.window)
        try:
            metrics = clf.train(labeled)
        except (ValueError, RuntimeError) as e:
            print(f"  ERRORE training: {e}")
            sys.exit(1)

        # Salva
        clf.save(args.model)

        # Salva fingerprint anche come JSON (per backup)
        fp_path = args.model.replace(".joblib", "_fingerprints.json")
        try:
            # Salva solo conteggi, non i frame interi
            summary = {k: len(v) for k, v in labeled.items()}
            with open(fp_path, "w") as f:
                json.dump(metrics, f, indent=2)
            print(f"  Metriche salvate: {fp_path}")
        except Exception:
            pass

        print(f"\n{'='*60}")
        print(f"  Training completato! {metrics['n_train']} campioni × "
              f"{metrics['n_features']} feature")
        print(f"  Accuracy: {metrics.get('accuracy', 'N/A')}")
        print(f"\n  Per la demo live:")
        print(f"    python3 -m csi.seat_mapper --mode live")
        if args.http_port != DEFAULT_HTTP_PORT:
            print(f"    http://localhost:{args.http_port}/classroom_heatmap.html")
        else:
            print(f"    http://localhost:{DEFAULT_HTTP_PORT}/classroom_heatmap.html")
        print(f"{'='*60}")

    elif args.mode == "live":
        if not _WS_AVAILABLE:
            print("  websockets non installato.")
            print("  pip install websockets")
            sys.exit(1)
        if args.ble and not _BLE_READER_AVAILABLE:
            print("  bleak non installato. pip install bleak")
            sys.exit(1)
        if not args.ble and not _SERIAL_AVAILABLE and not args.replay:
            print("  pyserial non installato e --replay non specificato.")
            print("  pip install pyserial o usa --ble")
            sys.exit(1)

        asyncio.run(_run_live_server(args))

    elif args.mode == "info":
        model_path = args.model or SEAT_MODEL_PATH
        if not os.path.exists(model_path):
            print(f"  Modello non trovato: {model_path}")
            sys.exit(1)
        clf = SeatClassifier()
        if clf.load(model_path):
            info = clf.get_info()
            print(f"  Addestrato: {info['trained']}")
            print(f"  Finestra: {info['window_size']} frame")
            print(f"  AP: {info['num_aps']}")
            print(f"  Classi ({len(info['classes'])}): {info['classes']}")
            print(f"  Buffer: {info['buffers_ready']}")
            print(f"  Pronto: {info['ready']}")


if __name__ == "__main__":
    main()
