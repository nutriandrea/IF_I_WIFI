#!/usr/bin/env python3
"""
CSI direct from ESP32 USB — bypass UNO Q.

Legge il flusso CSI dall'ESP32 connesso in USB al Mac (o a un qualsiasi
host con pyserial), senza passare dalla UNO Q / arduino-router.

L'ESP32 deve avere caricato esp32_csi_firmware/esp32_csi_firmware.ino
che stampa "CSI:<seq>:<rssi>:<noise>:<rate>:<bw>:<sub>:<r0,i0,...>" a 115200 baud.

Modalita':
  --monitor             Real-time presence detection con CSIDetector
  --capture N           Salva N secondi su file (csi_capture_*.txt)
  --calibrate           Baseline 30s + movement 30s + analisi soglie
  --num-aps N           Modalita multi-AP (channel hopping tra N hotspot).
                        Con --use-ml usa MultiAPCSIClassifier.

Esempi:
    python3 -m csi.csi_mac --monitor
    python3 -m csi.csi_mac --capture 60
    python3 -m csi.csi_mac --calibrate --seconds 30
    python3 -m csi.csi_mac --port /dev/cu.usbserial-1140 --monitor
    python3 -m csi.csi_mac --monitor --use-ml --num-aps 3   # multi-AP + ML

Dipendenze: pip install pyserial
"""

from __future__ import annotations
import argparse
import glob
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from statistics import mean, stdev
from typing import Iterator, Optional
from queue import Queue

try:
    import serial
except ImportError:
    sys.exit("Manca pyserial. Installa con:  pip install pyserial")

# Riusa parser e detector esistenti
from .csi_processor import parse_csi_line, CSIDetector

# CSI ML Classifier: import lazy
try:
    from .csi_ml import CSIClassifier, CSI_MODEL_PATH
    from .csi_ml import POSITIONS_MODEL_PATH, POSITIONS_LABELS_PATH
    _CSI_ML_AVAILABLE = True
except Exception:
    CSIClassifier = None
    CSI_MODEL_PATH = None
    _CSI_ML_AVAILABLE = False

DEFAULT_BAUD = 115200

# BLE reader (opzionale)
_BLE_READER_CLASS = None
try:
    from .csi_ble import BleReader as _BleReader
    _BLE_READER_CLASS = _BleReader
except ImportError:
    pass


# ============================================================
# Data source factory: serial OR BLE
# ============================================================

def line_source(args) -> Iterator[tuple[str, str | None]]:
    """Restituisce un iteratore di (linea, source_id).

    source_id è None per singola porta seriale o BLE,
    oppure "rx0", "rx1", ... per multi-rx.
    """
    multi_rx = getattr(args, "multi_rx", None)
    if multi_rx:
        ports = [p.strip() for p in multi_rx.split(",")]
        print(f"# multi-rx: {len(ports)} porte", file=sys.stderr)
        for p in ports:
            print(f"  {p}", file=sys.stderr)
        yield from _multi_rx_source(ports, args.baud)
    elif getattr(args, "ble", False):
        if _BLE_READER_CLASS is None:
            sys.exit("BLE non disponibile. pip install bleak")
        reader = _BLE_READER_CLASS()
        if not reader.connect():
            sys.exit("Connessione BLE fallita")
        for line in reader.iter_lines():
            yield (line, None)
    else:
        ser = open_port(args.port, args.baud)
        for line in iter_lines(ser):
            yield (line, None)


# ============================================================
# Serial helpers
# ============================================================
def autodetect_port() -> Optional[str]:
    cands: list[str] = []
    # Bluetooth SPP (ESP32_CSI)
    cands += sorted(glob.glob("/dev/cu.ESP32_CSI*"))
    cands += sorted(glob.glob("/dev/cu.ESP32*Bluetooth*"))
    # USB serial
    cands += sorted(glob.glob("/dev/cu.usbserial*"))
    cands += sorted(glob.glob("/dev/cu.usbmodem*"))
    cands += sorted(glob.glob("/dev/cu.SLAB_USBtoUART*"))
    cands += sorted(glob.glob("/dev/cu.wchusbserial*"))
    cands += sorted(glob.glob("/dev/ttyUSB*"))
    cands += sorted(glob.glob("/dev/ttyACM*"))
    seen: set[str] = set()
    return next((c for c in cands if not (c in seen or seen.add(c))), None)


def iter_lines(ser: serial.Serial) -> Iterator[str]:
    buf = bytearray()
    while True:
        chunk = ser.read(ser.in_waiting or 1)
        if not chunk:
            continue
        buf.extend(chunk)
        while b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            buf = bytearray(rest)
            yield line.decode("utf-8", errors="replace").rstrip("\r")


def _multi_rx_source(ports: list[str], baud: int) -> Iterator[tuple[str, str | None]]:
    """Legge da N porte seriali in parallelo usando thread.

    Ogni thread legge una porta e mette (linea, source_id) in una coda.
    source_id è "rx0", "rx1", ... in ordine di ports.
    """
    q: Queue = Queue()
    stop = threading.Event()

    def _reader(idx: int, port: str):
        try:
            ser = open_port(port, baud)
            buf = bytearray()
            while not stop.is_set():
                try:
                    if ser.in_waiting:
                        buf.extend(ser.read(ser.in_waiting))
                        while b"\n" in buf:
                            line, _, rest = buf.partition(b"\n")
                            buf = bytearray(rest)
                            q.put((line.decode("utf-8", errors="replace").rstrip("\r"),
                                   f"rx{idx}"))
                except serial.SerialException:
                    break
        except Exception as e:
            print(f"  ERRORE reader {idx} ({port}): {e}", file=sys.stderr)

    threads = []
    for i, p in enumerate(ports):
        t = threading.Thread(target=_reader, args=(i, p), daemon=True)
        t.start()
        threads.append(t)

    try:
        alive = len(threads)
        while alive > 0:
            line, sid = q.get()
            yield (line, sid)
            # check thread health
            alive = sum(1 for t in threads if t.is_alive())
            if alive < len(threads):
                break
    except GeneratorExit:
        stop.set()


def open_port(port: Optional[str], baud: int) -> serial.Serial:
    p = port or autodetect_port()
    if not p:
        sys.exit("Nessuna porta serial trovata. Specifica con --port.")
    print(f"# port={p} baud={baud}", file=sys.stderr)
    try:
        return serial.Serial(p, baud, timeout=0.1)
    except serial.SerialException as e:
        sys.exit(f"Impossibile aprire {p}: {e}")


# ============================================================
# Modalita': monitor live
# ============================================================
def cmd_monitor(args) -> int:
    ml_clf = None
    det = None
    use_ml = args.use_ml

    # Preparazione heatmap
    track_heatmap = {"fig": None, "ax": None, "im": None, "plt": None}
    grid_rows, grid_cols = 0, 0
    heatmap_enabled = args.heatmap

    if use_ml:
        if not _CSI_ML_AVAILABLE or CSIClassifier is None:
            print("  [ML] sklearn non installato. Uso CSIDetector classico.",
                  file=sys.stderr)
            use_ml = False
        else:
            assert CSIClassifier is not None
            if args.num_aps > 1:
                ml_clf = MultiAPCSIClassifier(window_frames=30, num_aps=args.num_aps)
            else:
                ml_clf = CSIClassifier(window_frames=args.window)
            # Prova modello posizioni prima, poi standard
            if ml_clf.load_custom():
                print(f"  Modello posizioni caricato.", file=sys.stderr)
                # Verifica se è modello griglia per heatmap
                labels = ml_clf._class_labels or []
                if heatmap_enabled and _is_grid_labels(labels):
                    grid_rows, grid_cols = _parse_grid_dims(labels)
                    if grid_rows > 0 and grid_cols > 0:
                        print(f"  Heatmap griglia {grid_rows}x{grid_cols} attivata.",
                              file=sys.stderr)
            else:
                model_file = args.ml_model or CSI_MODEL_PATH
                if model_file and os.path.exists(model_file):
                    ml_clf.load(model_file)
                    print(f"  Modello ML caricato da: {model_file}", file=sys.stderr)
                else:
                    print(f"  Modello ML non trovato, uso CSIDetector classico.",
                          file=sys.stderr)
                    use_ml = False

    if not use_ml:
        det = CSIDetector(
            window_size=args.window,
            ampl_threshold=args.ampl_th,
            var_threshold=args.var_th,
        )

    # Inizializza heatmap matplotlib
    if heatmap_enabled and grid_rows > 0:
        try:
            import matplotlib
            matplotlib.use("TkAgg")
            import matplotlib.pyplot as plt
            track_heatmap["plt"] = plt
            fig, ax = plt.subplots(figsize=(5, 4))
            fig.suptitle("CSI Posizioni Heatmap", fontsize=12)
            ax.set_xlabel("Colonna")
            ax.set_ylabel("Riga")
            ax.set_xticks(range(grid_cols))
            ax.set_yticks(range(grid_rows))
            ax.set_xticklabels([f"c{c}" for c in range(grid_cols)])
            ax.set_yticklabels([f"r{r}" for r in range(grid_rows)])
            im = ax.imshow(
                [[0.0] * grid_cols for _ in range(grid_rows)],
                vmin=0.0, vmax=1.0, cmap="YlOrRd", aspect="auto", origin="upper"
            )
            plt.colorbar(im, ax=ax, label="Probabilità")
            track_heatmap["fig"] = fig
            track_heatmap["ax"] = ax
            track_heatmap["im"] = im
            plt.ion()
            plt.show(block=False)
        except Exception as e:
            print(f"  Heatmap non disponibile: {e}", file=sys.stderr)
            heatmap_enabled = False

    if not use_ml:
        det = CSIDetector(
            window_size=args.window,
            ampl_threshold=args.ampl_th,
            var_threshold=args.var_th,
        )

    if use_ml:
        # Header dipende dal tipo di modello
        is_positions = ml_clf is not None and ml_clf._class_labels is not None
        if is_positions:
            source_info = ""
            if ml_clf._known_sources:
                if ml_clf._source_key == "source_id":
                    sources_str = ", ".join(ml_clf._known_sources)
                    source_info = f"  Sorgenti: {sources_str}"
                else:
                    # MAC mode: short names
                    mac_prefix_start = max(0, len(ml_clf._known_sources[0]) - 8) if ml_clf._known_sources else 0
                    sources_str = ", ".join(s[-8:] for s in ml_clf._known_sources if len(s) >= 8)
                    source_info = f"  MAC: {sources_str}"
            print(f"\n  CSI Posizioni Monitor — Ctrl+C per uscire{source_info}")
            print(f"  {'t(s)':>5} {'RSSI':>5} {'Conf':>7}  {'Posizione':>20}")
            print(f"  {'-'*42}")
        else:
            print(f"\n  CSI ML Monitor — Ctrl+C per uscire")
            print(f"  {'t(s)':>5} {'RSSI':>5} {'EMPTY':>7} {'STILL':>7} "
                  f"{'MOVE':>7} {'Classe':>12}")
            print(f"  {'-'*48}")
    else:
        print(f"\n  Monitor CSI — Ctrl+C per uscire")
        print(f"  {'t(s)':>6} {'seq':>5} {'subc':>4} {'ampl_mean':>9} "
              f"{'ampl_std':>8} {'RSSI':>5} {'score':>5}  Stato")
        print(f"  {'-'*60}")

    t0 = time.time()
    last_status = False
    seen = 0
    skipped = 0
    stop = {"v": False}

    def handle_sig(_s, _f): stop["v"] = True
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    try:
        for item in line_source(args):
            if stop["v"]:
                break
            # Supporta str (retrocompat) e tuple (line, source_id)
            if isinstance(item, tuple):
                line, source_id = item
            else:
                line, source_id = item, None
            if not line:
                continue
            # righe di debug del firmware (WiFi:, CSI:enabled, ESP32_CSI_READY)
            if not line.startswith("CSI:") or line.startswith("CSI:enabled") \
               or line.startswith("CSI:FAILED"):
                print(f"  # {line}", file=sys.stderr, flush=True)
                continue

            parsed = parse_csi_line(line)
            if not parsed:
                skipped += 1
                continue
            seen += 1
            if source_id is not None:
                parsed["source_id"] = source_id

            if use_ml:
                assert ml_clf is not None
                parsed["_t"] = round(time.time() - t0, 3)
                ml_clf.add_frame(parsed)
                probas = ml_clf.predict_proba()
                cls = ml_clf.predict()

                # Aggiorna heatmap matplotlib se attiva
                hm_im = track_heatmap["im"]
                hm_plt = track_heatmap["plt"]
                if heatmap_enabled and hm_im is not None and grid_rows > 0 \
                   and seen % args.print_every == 0 and probas:
                    grid_data = []
                    for r in range(grid_rows):
                        row_data = []
                        for c in range(grid_cols):
                            label = f"r{r}c{c}"
                            row_data.append(probas.get(label, 0.0))
                        grid_data.append(row_data)
                    hm_im.set_data(grid_data)
                    hm_im.axes.figure.canvas.draw_idle()
                    hm_plt.pause(0.001)

                # Se modello posizioni, mostra solo classe + prob max
                is_positions = ml_clf._class_labels is not None

                if seen % args.print_every == 0:
                    t = time.time() - t0
                    if is_positions:
                        max_prob = max(probas.values()) if probas else 0.0
                        mac_short = ""
                        frame_mac = parsed.get("mac", "")
                        if frame_mac and isinstance(frame_mac, str) and len(frame_mac) >= 8:
                            mac_short = f"MAC:{frame_mac[-8:]} "
                        print(f"  {t:>5.1f} "
                              f"{parsed.get('rssi', 0):>+5d} "
                              f"{max_prob:>7.3f}  "
                              f"{mac_short}{cls:>20}",
                              flush=True)
                    else:
                        print(f"  {t:>5.1f} "
                              f"{parsed.get('rssi', 0):>+5d} "
                              f"{probas.get('EMPTY', 0):>7.3f} "
                              f"{probas.get('STATIONARY', 0):>7.3f} "
                              f"{probas.get('MOVEMENT', 0):>7.3f} "
                              f"{cls:>12}",
                              flush=True)
            else:
                assert det is not None
                presence, info = det.update(parsed)
                if seen % args.print_every == 0:
                    t = time.time() - t0
                    status = "PRESENTE!" if presence else "vuoto"
                    if presence != last_status:
                        status = ">> " + status
                        last_status = presence
                    print(f"  {t:>6.1f} "
                          f"{parsed.get('seq', 0):>5} "
                          f"{parsed.get('num_subcarriers', 0):>4} "
                          f"{parsed.get('ampl_mean', 0):>9.2f} "
                          f"{parsed.get('ampl_std', 0):>8.2f} "
                          f"{parsed.get('rssi', 0):>+5d} "
                          f"{info['score']:>5.1f}  {status}",
                          flush=True)
    finally:
        pass  # ser chiuso da line_source o BLE

    print(f"\n  visti={seen}  ignorati={skipped}",
          file=sys.stderr)
    if not use_ml:
        assert det is not None
        print(f"  calibrato={det.calibrated}", file=sys.stderr)
    return 0


# ============================================================
# Modalita': capture su file
# ============================================================
def cmd_capture(args) -> int:
    out_dir = Path(args.out_dir) if args.out_dir else Path("csi_logs")
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    label = args.label or "capture"
    out_path = out_dir / f"csi_{label}_{ts}.txt"

    print(f"  Capture {args.seconds}s -> {out_path}", file=sys.stderr)
    t0 = time.time()
    n_csi = 0
    n_skip = 0

    with open(out_path, "w", buffering=1) as fh:
        fh.write(f"# csi_mac capture label={label} ts={ts}\n")
        try:
            for item in line_source(args):
                if isinstance(item, tuple):
                    line, _source_id = item
                else:
                    line = item
                if time.time() - t0 >= args.seconds:
                    break
                if not line:
                    continue
                if line.startswith("CSI:") and ":" in line[4:]:
                    parts = line.split(":", 2)
                    if len(parts) >= 3 and parts[1].isdigit():
                        fh.write(line + "\n")
                        n_csi += 1
                        continue
                print(f"  # {line}", file=sys.stderr, flush=True)
                n_skip += 1
        except KeyboardInterrupt:
            pass

    elapsed = time.time() - t0
    hz = n_csi / elapsed if elapsed > 0 else 0
    print(f"  Salvati {n_csi} frame ({hz:.1f} Hz) in {out_path}",
          file=sys.stderr)
    print(f"  Righe debug ignorate: {n_skip}", file=sys.stderr)
    return 0


# ============================================================
# Modalita': calibrate (baseline + movement + analyze)
# ============================================================
def _drain_buffer(it, timeout: float = 3.0):
    """Consuma e scarta linee dal buffer seriale per 'timeout' secondi."""
    t_end = time.time() + timeout
    for item in it:
        # item può essere str (per retrocompat) o (str, source_id) tuple
        line = item if isinstance(item, str) else item[0]
        if time.time() >= t_end:
            break


def _collect_frames(it, seconds: int, label: str) -> list[dict]:
    print(f"\n  Raccolta '{label}' — {seconds}s")
    print(f"  {'MUOVITI' if label == 'movement' else 'RIMANI FERMO'}.\n")
    frames: list[dict] = []

    # Scarta buffer seriale accumulato durante l'attesa (max 3s)
    _drain_buffer(it, timeout=3.0)

    t0 = time.time()
    last_report = 0.0
    for item in it:
        now = time.time()
        if now - t0 >= seconds:
            break
        # Supporta sia str (retrocompat) che tuple (line, source_id)
        if isinstance(item, tuple):
            line, source_id = item
        else:
            line, source_id = item, None
        if not line.startswith("CSI:"):
            continue
        parsed = parse_csi_line(line)
        if not parsed:
            continue
        if source_id is not None:
            parsed["source_id"] = source_id
        parsed["_t"] = round(now - t0, 3)
        parsed["_label"] = label
        frames.append(parsed)
        if now - last_report >= 5:
            last_report = now
            print(f"    {now - t0:>4.0f}s — {len(frames)} frame "
                  f"({len(frames)/(now - t0):.1f}/s)", flush=True)
    return frames


def _stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean": round(mean(vals), 3),
        "std": round(stdev(vals), 3) if len(vals) >= 2 else 0,
        "min": round(min(vals), 3),
        "max": round(max(vals), 3),
    }


def cmd_calibrate(args) -> int:
    train_ml = args.train_ml

    # Un solo data source per tutte le fasi
    it = iter(line_source(args))
    baseline = _collect_frames(it, args.seconds, "baseline")
    stationary = None
    if train_ml and args.stationary_seconds > 0:
        stationary = _collect_frames(it, args.stationary_seconds, "stationary")
    movement = _collect_frames(it, args.seconds, "movement")

    if len(baseline) < 10 or len(movement) < 10:
        print(f"\n  ATTENZIONE: troppi pochi frame "
              f"(baseline={len(baseline)} movement={len(movement)}). "
              f"L'ESP32 sta producendo CSI?", file=sys.stderr)
        return 1

    b_std = [f["ampl_std"] for f in baseline if "ampl_std" in f]
    m_std = [f["ampl_std"] for f in movement if "ampl_std" in f]
    b_rssi = [f["rssi"] for f in baseline if "rssi" in f]
    m_rssi = [f["rssi"] for f in movement if "rssi" in f]

    print("\n" + "="*60)
    print("  ANALISI CSI — baseline vs movement")
    print("="*60)
    print(f"\n  {'':<14}{'baseline':>12}{'movement':>12}")
    for k, b, m in [("ampl_std", b_std, m_std), ("rssi", b_rssi, m_rssi)]:
        bs, ms = _stats(b), _stats(m)
        print(f"\n  {k}:")
        for fld in ("n", "mean", "std", "min", "max"):
            print(f"    {fld:<10}{bs.get(fld, '-'):>12}{ms.get(fld, '-'):>12}")

    # Sweep di soglia su ampl_std
    print(f"\n  --- Strategia: soglia ampl_std ---")
    print(f"  {'Soglia':>8} {'FP%':>6} {'TP%':>6} {'Score':>6}  Verdetto")
    n_b, n_m = len(b_std), len(m_std)
    best = (None, -1.0)
    for th in [x / 10 for x in range(5, 51, 5)]:
        fp = sum(1 for v in b_std if v > th) / n_b
        tp = sum(1 for v in m_std if v > th) / n_m
        score = tp - fp
        if score > best[1]:
            best = (th, score)
        verdict = "OK" if (score > 0.5 and fp < 0.3) \
                  else ("FALSI POS" if score < 0.1 else "MARGINALE")
        print(f"  {th:>8.1f} {fp*100:>5.0f}% {tp*100:>5.0f}% {score:>6.2f}  {verdict}")

    print(f"\n  Migliore soglia ampl_std: {best[0]} (score={best[1]:.2f})")
    if best[1] < 0.1:
        print("  Detection RSSI/ampl insufficiente — l'ambiente o "
              "il setup CSI non distingue baseline da movement.")

    # Training ML
    if train_ml:
        print(f"\n{'='*60}")
        print(f"  TRAINING CSI ML CLASSIFIER")
        print(f"{'='*60}")
        if not _CSI_ML_AVAILABLE or CSIClassifier is None:
            print("  [ML] sklearn non installato. Salta training ML.")
            print("  Installa: pip install scikit-learn joblib")
        else:
            assert CSIClassifier is not None
            try:
                if args.num_aps > 1:
                    clf = MultiAPCSIClassifier(window_frames=30, num_aps=args.num_aps)
                else:
                    clf = CSIClassifier(window_frames=30)
                metrics = clf.train(baseline, stationary, movement)
                save_path = args.ml_model or CSI_MODEL_PATH or "csi_model.joblib"
                clf.save(save_path)
                print(f"  Training completato: {metrics['n_train']} campioni, "
                      f"{metrics['n_classes']} classi")

                # Salva frame come JSON
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                out_dir = args.out_dir or "csi_logs"
                Path(out_dir).mkdir(exist_ok=True)
                for label, frames in [("empty", baseline), ("stationary", stationary), ("movement", movement)]:
                    if frames:
                        path = Path(out_dir) / f"CSI_ML_{label}_{ts}.json"
                        with open(path, "w") as f:
                            json.dump({"label": label, "frames": frames}, f, indent=2)
                        print(f"  Salvato: {path}")
            except Exception as e:
                print(f"  ERRORE training ML: {e}")

    return 0


# ============================================================
# Heatmap
# ============================================================

def _is_grid_labels(labels: list[str]) -> bool:
    """Controlla se le etichette includono posizioni formato griglia r<N>c<M>.
    Almeno metà delle etichette (escluse baseline come 'vuoto') deve matchare."""
    if not labels:
        return False
    import re
    n_grid = sum(1 for lbl in labels if re.match(r"^r\d+c\d+$", lbl))
    n_other = len(labels) - n_grid
    # Griglia se almeno 1 cella griglia e non più non-griglia che celle griglia
    return n_grid >= 1 and n_grid >= n_other


def _parse_grid_dims(labels: list[str]) -> tuple[int, int]:
    """Estrae dimensioni griglia da etichette formato r<N>c<M>.
    Ignora etichette non-grid (es. 'vuoto').
    Restituisce (rows, cols) o (0, 0) se non rilevabile."""
    import re
    rows = set()
    cols = set()
    for lbl in labels:
        m = re.match(r"^r(\d+)c(\d+)$", lbl)
        if m:
            rows.add(int(m.group(1)))
            cols.add(int(m.group(2)))
    if not rows or not cols:
        return 0, 0
    return max(rows) + 1, max(cols) + 1


# ============================================================
# Comandi CLI
# ============================================================


def _countdown(seconds: int = 5):
    """Conto alla rovescia prima della registrazione."""
    for i in range(seconds, 0, -1):
        print(f"  {i}...", end=" ", flush=True)
        time.sleep(1)
    print("VIA!", flush=True)


def cmd_positions(args) -> int:
    """Colleziona CSI per posizioni (libere o su griglia) e addestra modello."""
    if not _CSI_ML_AVAILABLE or CSIClassifier is None:
        print("sklearn non installato. pip install scikit-learn joblib")
        return 1

    it = iter(line_source(args))
    labeled_frames: dict[str, list] = {}
    seconds = args.seconds

    # Modalità griglia vs posizioni libere
    grid_rows, grid_cols = 0, 0
    if args.grid:
        try:
            parts = args.grid.lower().split("x")
            grid_rows, grid_cols = int(parts[0]), int(parts[1])
            if grid_rows < 1 or grid_cols < 1:
                raise ValueError
        except (ValueError, IndexError):
            print(f"  ERRORE: formato griglia non valido: '{args.grid}' (usa ROWSxCOLS es. 3x3)")
            return 1
        n_positions = grid_rows * grid_cols
        print(f"\n  Colleziono dati per griglia {grid_rows}x{grid_cols} "
              f"({n_positions} celle) + vuoto ({seconds}s ciascuna)\n")
        labels_hint = [f"r{r}c{c}" for r in range(grid_rows) for c in range(grid_cols)]
    else:
        n_positions = args.num_positions
        print(f"\n  Colleziono dati per {n_positions} posizioni + vuoto ({seconds}s ciascuna)\n")

    # 1. Baseline vuoto
    print("=" * 50)
    print("  FASE 1: STANZA VUOTA (baseline)")
    print("  Tutti fuori dalla stanza. Premi INVIO quando pronto.")
    print("=" * 50)
    input()
    _countdown()
    labeled_frames["vuoto"] = _collect_frames(it, seconds, "vuoto")
    print(f"  -> {len(labeled_frames['vuoto'])} frame raccolti\n")

    # 2. Posizioni
    for i in range(n_positions):
        print("=" * 50)
        if args.grid:
            r, c = i // grid_cols, i % grid_cols
            default_label = f"r{r}c{c}"
            hint = f"  GRIGLIA: cella ({r + 1}, {c + 1})/{grid_rows}x{grid_cols} — {default_label}"
            label = input(f"  {hint} [INVIO=ok, o nome diverso]: ").strip()
            if not label:
                label = default_label
        else:
            label = input(f"  POSIZIONE {i + 1}/{n_positions} — Nome (es. divano, sedia): ").strip()
            if not label:
                label = f"posto{i + 1}"
        print(f"  Mettiti in '{label}'. Premi INVIO quando pronto.")
        print("=" * 50)
        input()
        _countdown()
        labeled_frames[label] = _collect_frames(it, seconds, label)
        print(f"  -> {len(labeled_frames[label])} frame raccolti\n")

    # Verifica minimo frame
    for name, frames in labeled_frames.items():
        if len(frames) < 10:
            print(f"  ERRORE: '{name}' ha solo {len(frames)} frame (servono >= 10)")
            return 1

    # 3. Training
    print("\n" + "=" * 50)
    print("  TRAINING MODELLO POSIZIONI")
    print("=" * 50)
    clf = CSIClassifier(window_frames=args.window)
    try:
        metrics = clf.train_custom(labeled_frames)
        clf.save_custom()
        print(f"\n  Modello posizioni salvato!")
        print(f"  Classi: {list(labeled_frames.keys())}")
        if args.grid:
            print(f"  Griglia: {grid_rows}x{grid_cols}")
    except Exception as e:
        print(f"  ERRORE training: {e}")
        return 1

    return 0


# ============================================================
# Main
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="CSI direct from ESP32 USB/BLE")
    ap.add_argument("--port", help="Porta serial (default: autodetect)")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--ble", action="store_true",
                    help="Usa BLE invece di seriale (ESP32 BLE firmware)")
    ap.add_argument("--multi-rx", type=str, default=None,
                    help="Multi-ricevitore: porte separate da virgola (es. /dev/ttyUSB0,/dev/ttyUSB1)")
    ap.add_argument("--ap-mode", action="store_true",
                    help="ESP32 in AP mode (3 PC si connettono direttamente)")

    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--monitor", action="store_true",
                      help="Real-time presence detection")
    mode.add_argument("--capture", action="store_true",
                      help="Salva CSI su file per N secondi")
    mode.add_argument("--calibrate", action="store_true",
                      help="Baseline + movement + analisi")
    mode.add_argument("--positions", action="store_true",
                      help="Colleziona dati per N posizioni e addestra modello")

    ap.add_argument("--num-positions", type=int, default=4,
                    help="Numero di posizioni da collezionare (default 4)")
    ap.add_argument("--grid", type=str, default=None,
                    help="Griglia ROWSxCOLS per heatmap posizioni (es. 3x3). Sostituisce --num-positions")
    ap.add_argument("--heatmap", action="store_true",
                    help="Mostra heatmap probabilità su griglia (--monitor + modello griglia)")
    ap.add_argument("--seconds", type=int, default=30,
                    help="Durata capture/calibrate (default 30)")
    ap.add_argument("--label", help="Etichetta per file capture")
    ap.add_argument("--out-dir", help="Directory output capture")
    ap.add_argument("--window", type=int, default=50,
                    help="Finestra CSIDetector (default 50)")
    ap.add_argument("--ampl-th", type=float, default=2.0)
    ap.add_argument("--var-th", type=float, default=1.5)
    ap.add_argument("--print-every", type=int, default=5,
                    help="Stampa ogni N frame in --monitor (default 5)")
    # ML flags
    ap.add_argument("--use-ml", action="store_true",
                    help="Usa CSIClassifier (ML) invece di CSIDetector")
    ap.add_argument("--train-ml", action="store_true",
                    help="Addestra CSIClassifier dopo la calibrazione")
    ap.add_argument("--ml-model", type=str, default=None,
                    help="Percorso modello .joblib (default: csi_model.joblib)")
    ap.add_argument("--stationary-seconds", type=int, default=0,
                    help="Secondi per fase STATIONARY (0=salta, default: 0)")
    ap.add_argument("--num-aps", type=int, default=1,
                    help="Numero AP in channel hopping (1=mono-AP, 3=multi-AP). Default 1.")
    args = ap.parse_args()

    if args.monitor:
        return cmd_monitor(args)
    if args.capture:
        return cmd_capture(args)
    if args.calibrate:
        return cmd_calibrate(args)
    if args.positions:
        return cmd_positions(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
