#!/usr/bin/env python3
"""
CSI direct from ESP32 USB — bypass UNO Q.

Legge il flusso CSI dall'ESP32 connesso in USB al Mac (o a un qualsiasi
host con pyserial), senza passare dalla UNO Q / arduino-router.

L'ESP32 deve avere caricato esp32_csi_firmware/esp32_csi_firmware.ino
che stampa "CSI:<seq>:<rssi>:<noise>:<rate>:<bw>:<sub>:<r0,i0,...>" a 921600 baud.

Modalita':
  --monitor             Real-time presence detection con CSIDetector
  --capture N           Salva N secondi su file (csi_capture_*.txt)
  --calibrate           Baseline 30s + movement 30s + analisi soglie
  --num-aps N           Modalita multi-AP (channel hopping tra N hotspot).
                        Con --use-ml usa MultiAPCSIClassifier.

Esempi:
    python3 csi_mac.py --monitor
    python3 csi_mac.py --capture 60
    python3 csi_mac.py --calibrate --seconds 30
    python3 csi_mac.py --port /dev/cu.usbserial-1140 --monitor
    python3 csi_mac.py --monitor --use-ml --num-aps 3   # multi-AP + ML

Dipendenze: pip install pyserial
"""

from __future__ import annotations
import argparse
import glob
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import mean, stdev
from typing import Iterator, Optional

try:
    import serial
except ImportError:
    sys.exit("Manca pyserial. Installa con:  pip install pyserial")

# Riusa parser e detector esistenti
from csi_processor import parse_csi_line, CSIDetector

# CSI ML Classifier: import lazy
try:
    from csi_ml import CSIClassifier, MultiAPCSIClassifier, CSI_CLASSES, CSI_LABELS, CSI_MODEL_PATH
    _CSI_ML_AVAILABLE = True
except ImportError:
    CSIClassifier = None
    MultiAPCSIClassifier = None
    CSI_CLASSES = {}
    CSI_LABELS = []
    CSI_MODEL_PATH = None
    _CSI_ML_AVAILABLE = False

DEFAULT_BAUD = 921600


# ============================================================
# Serial helpers
# ============================================================
def autodetect_port() -> Optional[str]:
    cands: list[str] = []
    cands += sorted(glob.glob("/dev/cu.usbserial*"))
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
    ser = open_port(args.port, args.baud)
    ml_clf = None
    det = None
    use_ml = args.use_ml

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
                ml_clf = CSIClassifier(window_frames=30)
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

    if use_ml:
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
        for line in iter_lines(ser):
            if stop["v"]:
                break
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

            if use_ml:
                assert ml_clf is not None
                parsed["_t"] = round(time.time() - t0, 3)
                ml_clf.add_frame(parsed)
                probas = ml_clf.predict_proba()
                cls = ml_clf.predict()

                if seen % args.print_every == 0:
                    t = time.time() - t0
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
        ser.close()

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
    ser = open_port(args.port, args.baud)

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
            for line in iter_lines(ser):
                if time.time() - t0 >= args.seconds:
                    break
                if not line:
                    continue
                if line.startswith("CSI:") and ":" in line[4:]:
                    # E' una riga CSI vera (CSI:<seq>:...), non "CSI:enabled"
                    parts = line.split(":", 2)
                    if len(parts) >= 3 and parts[1].isdigit():
                        fh.write(line + "\n")
                        n_csi += 1
                        continue
                # altre righe (debug) → stderr
                print(f"  # {line}", file=sys.stderr, flush=True)
                n_skip += 1
        except KeyboardInterrupt:
            pass
    ser.close()

    elapsed = time.time() - t0
    hz = n_csi / elapsed if elapsed > 0 else 0
    print(f"  Salvati {n_csi} frame ({hz:.1f} Hz) in {out_path}",
          file=sys.stderr)
    print(f"  Righe debug ignorate: {n_skip}", file=sys.stderr)
    return 0


# ============================================================
# Modalita': calibrate (baseline + movement + analyze)
# ============================================================
def _collect_frames(ser: serial.Serial, seconds: int, label: str) -> list[dict]:
    print(f"\n  Raccolta '{label}' — {seconds}s")
    print(f"  {'MUOVITI' if label == 'movement' else 'RIMANI FERMO'}.\n")
    frames: list[dict] = []
    t0 = time.time()
    last_report = 0.0
    for line in iter_lines(ser):
        now = time.time()
        if now - t0 >= seconds:
            break
        if not line.startswith("CSI:"):
            continue
        parsed = parse_csi_line(line)
        if not parsed:
            continue
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
    ser = open_port(args.port, args.baud)
    train_ml = args.train_ml

    try:
        baseline = _collect_frames(ser, args.seconds, "baseline")
        stationary = None
        if train_ml and args.stationary_seconds > 0:
            stationary = _collect_frames(ser, args.stationary_seconds, "stationary")
        movement = _collect_frames(ser, args.seconds, "movement")
    finally:
        ser.close()

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
# Main
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="CSI direct from ESP32 USB")
    ap.add_argument("--port", help="Porta serial (default: autodetect)")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)

    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--monitor", action="store_true",
                      help="Real-time presence detection")
    mode.add_argument("--capture", action="store_true",
                      help="Salva CSI su file per N secondi")
    mode.add_argument("--calibrate", action="store_true",
                      help="Baseline + movement + analisi")

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
    return 1


if __name__ == "__main__":
    sys.exit(main())
