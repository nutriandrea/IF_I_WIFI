#!/usr/bin/env python3
"""
Calibrate Presence Detection — Arduino UNO Q

Raccoglie campioni RSSI in due fasi (vuoto / movimento) e trova la
soglia ottimale per il rilevamento presenza nell'ambiente corrente.

Usage:
  # Fase 1: baseline (nessuno si muove, 30s)
  python3 calibrate_presence.py --mode baseline --seconds 30

  # Fase 2: movimento (cammina nella stanza, 30s)
  python3 calibrate_presence.py --mode movement --seconds 30

  # Analisi combinata (usa file salvati dalle fasi precedenti)
  python3 calibrate_presence.py --mode analyze

  # Monitoraggio real-time con soglia calibrata
  python3 calibrate_presence.py --mode monitor --threshold 1.5

  # Calibrazione rapida (baseline + movimento + analisi in sequenza)
  python3 calibrate_presence.py --mode quick
"""

import subprocess, time, json, sys, os, re, shutil, argparse
from datetime import datetime
from collections import deque
from statistics import mean, stdev

# ============================================================
# Config
# ============================================================
SAMPLING_INTERVAL = 0.5  # secondi tra campioni
REPORT_DIR = os.path.dirname(os.path.abspath(__file__))
IW = shutil.which("iw") or "/usr/sbin/iw"


# ============================================================
# RSSI sampling (stessa logica di feasibility_test.py)
# ============================================================
def _rssi(iface: str) -> float | None:
    if not iface:
        return None
    try:
        r = subprocess.check_output(f"{IW} dev {iface} link", shell=True,
                                     timeout=3, stderr=subprocess.DEVNULL).decode()
        m = re.search(r"signal:\s*(-?\d+\.?\d*)\s*dBm", r)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def detect_wifi_iface() -> str | None:
    """Trova la prima interfaccia WiFi attiva."""
    ifaces = []
    try:
        out = subprocess.check_output("ip link show", shell=True,
                                       timeout=5, stderr=subprocess.DEVNULL).decode()
        for m in re.finditer(r"^\d+:\s+(\S+):", out, re.MULTILINE):
            name = m.group(1).strip(":")
            if re.match(r"^(wlan|wlx|wlp)", name):
                ifaces.append(name)
    except Exception:
        pass
    if ifaces:
        return ifaces[0]
    # Fallback: /sys/class/net
    try:
        for entry in sorted(os.listdir("/sys/class/net")):
            if re.match(r"^(wlan|wlx)", entry):
                return entry
    except FileNotFoundError:
        pass
    return None


def collect_samples(iface: str, seconds: int = 30, label: str = "baseline") -> list[dict]:
    """Raccoglie campioni RSSI e li salva su file."""
    print(f"\n  Collezione '{label}' — {seconds}s su {iface}")
    print(f"  Muoviti nella stanza durante la fase '{label}'!" if label == "movement"
          else f"  RIMANI FERMO durante la fase '{label}'.")
    print(f"  Premi Ctrl+C per interrompere prima.\n")

    samples = []
    start = time.time()
    last_report = 0

    try:
        while time.time() - start < seconds:
            val = _rssi(iface)
            now = time.time()
            if val is not None and -120 <= val <= 0:
                samples.append({
                    "rssi_dbm": val,
                    "timestamp": now,
                    "label": label,
                    "t": now - start,
                })

            # Report progress ogni 5s
            elapsed = now - start
            if elapsed - last_report >= 5:
                n = len(samples)
                rate = n / elapsed if elapsed > 0 else 0
                if n >= 2:
                    vals = [s["rssi_dbm"] for s in samples]
                    print(f"    {elapsed:.0f}s — {n} campioni ({rate:.1f}/s), "
                          f"RSSI: {min(vals):.0f}..{max(vals):.0f} dBm, "
                          f"std={stdev(vals):.2f}")
                last_report = elapsed

            time.sleep(SAMPLING_INTERVAL)

    except KeyboardInterrupt:
        print(f"\n  Interrotto dopo {time.time()-start:.0f}s")

    # Salva su file
    filename = f"calib_{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    filepath = os.path.join(REPORT_DIR, filename)
    with open(filepath, "w") as f:
        json.dump({"label": label, "samples": samples,
                    "iface": iface, "duration_s": round(time.time()-start, 1)},
                   f, indent=2)
    print(f"\n  Salvati {len(samples)} campioni in {filename}")
    return samples


def load_samples(label: str) -> list[dict]:
    """Carica campioni da un file di calibrazione."""
    pattern = f"calib_{label}_*.json"
    import glob
    files = sorted(glob.glob(os.path.join(REPORT_DIR, pattern)))
    if not files:
        print(f"  Nessun file trovato per '{label}' (cercato: {pattern})")
        return []
    # Prende il piu recente
    with open(files[-1]) as f:
        data = json.load(f)
    print(f"  Caricati {len(data['samples'])} campioni '{label}' da {os.path.basename(files[-1])}")
    return data["samples"]


# ============================================================
# Strategie di rilevamento
# ============================================================
class AdaptivePresenceDetector:
    """Detector adattivo basato su delta della media mobile."""

    def __init__(self, window_size: int = 20, delta_threshold: float = 1.5):
        self.window = deque(maxlen=window_size)
        self.delta_threshold = delta_threshold
        self.baseline_mean = None
        self.baseline_std = None

    def update(self, rssi: float) -> bool:
        self.window.append(rssi)
        if len(self.window) < 10:
            return False

        # Media recente (ultimi 5 campioni)
        recent = list(self.window)[-5:]
        recent_mean = mean(recent)

        if self.baseline_mean is None:
            # Auto-calibrazione dai primi dati
            self.baseline_mean = mean(self.window)
            self.baseline_std = stdev(self.window) if len(self.window) >= 2 else 0
            return False

        delta = abs(recent_mean - self.baseline_mean)
        return delta > self.delta_threshold


class StdDetector:
    """Detector basato su soglia di deviazione standard."""

    def __init__(self, window_size: int = 20, std_threshold: float = 2.0):
        self.window = deque(maxlen=window_size)
        self.std_threshold = std_threshold

    def update(self, rssi: float) -> bool:
        self.window.append(rssi)
        if len(self.window) < 5:
            return False
        return stdev(self.window) > self.std_threshold


class DeltaDetector:
    """Detector basato su range (max-min) della finestra."""

    def __init__(self, window_size: int = 20, delta_threshold: float = 5.0):
        self.window = deque(maxlen=window_size)
        self.delta_threshold = delta_threshold

    def update(self, rssi: float) -> bool:
        self.window.append(rssi)
        if len(self.window) < 5:
            return False
        return max(self.window) - min(self.window) > self.delta_threshold


# ============================================================
# Analisi
# ============================================================
def analyze(baseline: list[dict], movement: list[dict]):
    """Analizza i campioni raccolti e raccomanda soglie."""
    print("\n" + "=" * 60)
    print("  ANALISI — Calibrazione Presence Detection")
    print("=" * 60)

    b_vals = [s["rssi_dbm"] for s in baseline]
    m_vals = [s["rssi_dbm"] for s in movement]

    if len(b_vals) < 10:
        print("  ERRORE: almeno 10 campioni necessari per baseline")
        return
    if len(m_vals) < 10:
        print("  ERRORE: almeno 10 campioni necessari per movement")
        return

    # Statistiche di base
    b_mean, b_std = mean(b_vals), stdev(b_vals)
    m_mean, m_std = mean(m_vals), stdev(m_vals)

    print(f"\n  --- Statistiche ---")
    print(f"  {'':>15} {'Baseline':>12} {'Movement':>12}")
    print(f"  {'Campioni':>15} {len(b_vals):>12} {len(m_vals):>12}")
    print(f"  {'Media':>15} {b_mean:>12.2f} {m_mean:>12.2f}")
    print(f"  {'Std Dev':>15} {b_std:>12.2f} {m_std:>12.2f}")
    print(f"  {'Min':>15} {min(b_vals):>12.2f} {min(m_vals):>12.2f}")
    print(f"  {'Max':>15} {max(b_vals):>12.2f} {max(m_vals):>12.2f}")

    # Prova diverse soglie per std threshold
    print(f"\n  --- Strategia 1: Soglia STD ---")
    print(f"  {'Soglia':>8} {'FP':>8} {'TP':>8} {'Verdetto':>12}")
    best_std_th = None
    best_std_score = 0
    for th in [round(x * 0.5, 1) for x in range(1, 21)]:
        fp = 1 if b_std > th else 0
        tp = 1 if m_std > th else 0
        score = tp - fp  # 0=inutile, 1=ottimo
        verdict = "OK" if score == 1 else ("FALSI POS" if fp else "NON RILEVA")
        if score > best_std_score:
            best_std_score = score
            best_std_th = th
        if th <= b_std * 2 or score == 1 or (fp and th < b_std + 2):
            print(f"  {th:>8.1f} {fp:>8} {tp:>8} {verdict:>12}")

    print(f"\n  Miglior soglia STD: {best_std_th}")
    print(f"  -> std_threshold = baseline_std * {best_std_th/b_std:.1f}" if best_std_th else "")

    # Prova diverse soglie per adaptive detector
    print(f"\n  --- Strategia 2: Adaptive Delta (media mobile) ---")
    print(f"  {'Soglia':>8} {'FP':>8} {'TP':>8} {'Verdetto':>12}")
    best_delta_th = None
    best_delta_score = 0
    for th in [round(x * 0.25, 2) for x in range(1, 41)]:
        # Simula adaptive detector su baseline
        det = AdaptivePresenceDetector(window_size=20, delta_threshold=th)
        b_detections = sum(1 for v in b_vals if det.update(v))
        fp_rate = b_detections / len(b_vals)

        det2 = AdaptivePresenceDetector(window_size=20, delta_threshold=th)
        m_detections = sum(1 for v in m_vals if det2.update(v))
        tp_rate = m_detections / len(m_vals)

        if tp_rate >= 0.5 and fp_rate <= 0.2:
            score = tp_rate - fp_rate
            if score > best_delta_score:
                best_delta_score = score
                best_delta_th = th
        if abs(th - best_delta_th) < 0.5 or (fp_rate <= 0.3 and tp_rate >= 0.3):
            verdict = "OK" if fp_rate <= 0.2 and tp_rate >= 0.5 else \
                      ("FALSI POS" if fp_rate > 0.3 else "POCO")
            print(f"  {th:>8.2f} {fp_rate:>8.0%} {tp_rate:>8.0%} {verdict:>12}")

    # Risultati
    print(f"\n  --- Raccomandazioni ---")
    print(f"  Ambiente: std baseline = {b_std:.2f} (segnale ~{b_mean:.0f} dBm)")
    print(f"  Metodo STD:      soglia = {best_std_th or 'N/A'} (non consigliato con segnale forte)")
    print(f"  Metodo Adapt:    soglia = {best_delta_th or 'N/A'}")
    print(f"")
    print(f"  Per l'uso nel decision engine:")
    if best_delta_th:
        print(f"    detector = AdaptivePresenceDetector(")
        print(f"        window_size=20, delta_threshold={best_delta_th})")
    print(f"")

    # Salva report
    report = {
        "timestamp": datetime.now().isoformat(),
        "iface": baseline[0].get("iface", "?"),
        "baseline": {"n": len(b_vals), "mean": b_mean, "std": b_std,
                     "min": min(b_vals), "max": max(b_vals)},
        "movement": {"n": len(m_vals), "mean": m_mean, "std": m_std,
                     "min": min(m_vals), "max": max(m_vals)},
        "recommended": {
            "std_threshold": best_std_th,
            "adaptive_delta_threshold": best_delta_th,
            "method": "adaptive_delta" if best_delta_th else "std",
        }
    }
    report_file = f"calib_report_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(os.path.join(REPORT_DIR, report_file), "w") as f:
        json.dump(report, f, indent=2)
    print(f"  Report salvato: {report_file}")


# ============================================================
# Monitor real-time
# ============================================================
def monitor(iface: str, threshold: float):
    """Monitoraggio real-time con AdaptivePresenceDetector."""
    det = AdaptivePresenceDetector(window_size=20, delta_threshold=threshold)
    print(f"\n  Monitoraggio real-time (soglia delta={threshold})")
    print(f"  {'Tempo':>6} {'RSSI':>8} {'Stato':>10}")
    print(f"  {'-'*26}")

    start = time.time()
    try:
        while True:
            val = _rssi(iface)
            t = time.time() - start
            if val is not None:
                presence = det.update(val)
                status = "PRESENTE!" if presence else "vuoto"
                print(f"  {t:>6.1f} {val:>8.1f} {status:>10}")
            time.sleep(SAMPLING_INTERVAL)
    except KeyboardInterrupt:
        print(f"\n  Monitor terminato dopo {t:.0f}s")


# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Calibra la soglia di presence detection su UNO Q")
    parser.add_argument("--mode", choices=["baseline", "movement", "analyze",
                                            "monitor", "quick"],
                        default="quick",
                        help="Fase di calibrazione")
    parser.add_argument("--seconds", type=int, default=30,
                        help="Durata collezione in secondi (default: 30)")
    parser.add_argument("--threshold", type=float, default=None,
                        help="Soglia per monitor (default: auto-calcolata)")
    args = parser.parse_args()

    iface = detect_wifi_iface()
    if not iface:
        print("ERRORE: nessuna interfaccia WiFi trovata")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  Calibrazione Presence Detection — {iface}")
    print(f"  {IW} (RSSI sampling)")
    print(f"{'='*60}")

    if args.mode == "baseline":
        collect_samples(iface, args.seconds, "baseline")

    elif args.mode == "movement":
        collect_samples(iface, args.seconds, "movement")

    elif args.mode == "analyze":
        baseline = load_samples("baseline")
        movement = load_samples("movement")
        if not baseline or not movement:
            print("  Esegui prima: python3 calibrate_presence.py --mode baseline")
            print("         e poi: python3 calibrate_presence.py --mode movement")
            sys.exit(1)
        analyze(baseline, movement)

    elif args.mode == "monitor":
        if args.threshold:
            monitor(iface, args.threshold)
        else:
            # Cerca report salvato
            import glob
            reports = sorted(glob.glob(os.path.join(REPORT_DIR, "calib_report_*.json")))
            if reports:
                with open(reports[-1]) as f:
                    rep = json.load(f)
                th = rep.get("recommended", {}).get("adaptive_delta_threshold", 1.5)
                print(f"  Soglia dal report: {th}")
                monitor(iface, th)
            else:
                print("  Nessun report trovato. Uso soglia default: 1.5")
                monitor(iface, 1.5)

    elif args.mode == "quick":
        print("\n=== CALIBRAZIONE RAPIDA ===")
        print("Fase 1/3: Collezione BASELINE (stai fermo)")
        bl = collect_samples(iface, args.seconds, "baseline")
        print("\nFase 2/3: Collezione MOVEMENT (cammina nella stanza)")
        mv = collect_samples(iface, args.seconds, "movement")
        print("\nFase 3/3: Analisi")
        analyze(bl, mv)


if __name__ == "__main__":
    main()
