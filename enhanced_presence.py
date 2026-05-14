#!/usr/bin/env python3
"""
Enhanced Presence Detection — Arduino UNO Q

Multi-metric fusion con gradient detector + active probing.
Campiona fino a 50 Hz e combina:
  - RSSI gradient (rate of change)
  - signal_avg, tx_rate da iw station dump
  - Ping RTT al gateway (opzionale)
  - Consecutive same-sign gradient (pattern detection)

Usage:
  # Calibrazione rapida (30s baseline + 30s movimento + analisi)
  python3 enhanced_presence.py --mode quick

  # Solo baseline
  python3 enhanced_presence.py --mode baseline --seconds 30

  # Solo movimento
  python3 enhanced_presence.py --mode movement --seconds 30

  # Analisi dati gia' raccolti
  python3 enhanced_presence.py --mode analyze

  # Monitoraggio real-time
  python3 enhanced_presence.py --mode monitor
"""

import subprocess, time, json, sys, os, re, argparse
from datetime import datetime
from collections import deque
from statistics import mean, stdev

# ============================================================
# Config
# ============================================================
IW = "/usr/sbin/iw"
SAMPLING_INTERVAL = 0.05  # 50ms = 20 Hz (con 10ms per iw, resta margine)
BASELINE_SECONDS = 30
MOVEMENT_SECONDS = 30
REPORT_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================
# WiFi metrics collector
# ============================================================
_last_station_dump = {}
_last_station_ts = 0

def get_wifi_metrics(iface: str) -> dict:
    """Raccoglie tutte le metriche WiFi disponibili."""
    global _last_station_dump, _last_station_ts

    m = {}
    now = time.time()

    # 1. iw link — veloce (10ms), sempre disponibile
    try:
        out = subprocess.check_output(
            f"{IW} dev {iface} link", shell=True, timeout=2
        ).decode()
        m_ = re.search(r"signal:\s*(-?\d+)", out)
        if m_: m["rssi"] = int(m_.group(1))
        m_ = re.search(r"tx bitrate:\s*([\d.]+)", out)
        if m_: m["tx_rate"] = float(m_.group(1))
        m_ = re.search(r"rx bitrate:\s*([\d.]+)", out)
        if m_: m["rx_rate"] = float(m_.group(1))
        m_ = re.search(r"freq:\s*(\d+)", out)
        if m_: m["freq"] = int(m_.group(1))
    except Exception:
        pass

    # 2. station dump — metriche extra (aggiornato ogni 1s, costa ~10ms)
    if now - _last_station_ts >= 1.0:
        try:
            out = subprocess.check_output(
                f"{IW} dev {iface} station dump", shell=True, timeout=2
            ).decode()
            m2 = {}
            m_ = re.search(r"signal avg:\s*(-?\d+)", out)
            if m_: m2["signal_avg"] = int(m_.group(1))
            m_ = re.search(r"signal:\s*(-?\d+)", out)
            if m_ and "signal_avg" not in m2:
                m2["signal_avg"] = int(m_.group(1))
            m_ = re.search(r"inactive time:\s*(\d+)", out)
            if m_: m2["inactive_time"] = int(m_.group(1))
            m_ = re.search(r"tx retries:\s*(\d+)", out)
            if m_: m2["tx_retries"] = int(m_.group(1))
            m_ = re.search(r"beacon loss:\s*(\d+)", out)
            if m_: m2["beacon_loss"] = int(m_.group(1))
            m_ = re.search(r"expected throughput:\s*([\d.]+)", out)
            if m_: m2["expected_tp"] = float(m_.group(1))
            _last_station_dump = m2
            _last_station_ts = now
        except Exception:
            pass

    m.update(_last_station_dump)
    return m


# ============================================================
# Active probing (ping al gateway)
# ============================================================
_gateway = None
_last_ping = {}
_last_ping_ts = 0

def detect_gateway() -> str | None:
    """Trova il gateway predefinito."""
    try:
        out = subprocess.check_output(
            "ip route | grep default", shell=True, timeout=3
        ).decode()
        m = re.search(r"default via (\S+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def get_ping_metrics(gw: str = None) -> dict:
    """Ping rapido (3 pacchetti) per stimare latenza."""
    global _last_ping, _last_ping_ts
    now = time.time()
    if now - _last_ping_ts < 1.0:
        return _last_ping  # cache 1s

    g = gw or _gateway or detect_gateway()
    if not g:
        return {}

    try:
        out = subprocess.check_output(
            f"ping -c 3 -W 1 {g}", shell=True, timeout=3
        ).decode()
        m2 = {}
        m = re.search(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/[\d.]+/([\d.]+)", out)
        if m:
            m2["ping_avg"] = float(m.group(1))
            m2["ping_mdev"] = float(m.group(2))
        else:
            times = re.findall(r"time=([\d.]+)", out)
            if times:
                t = [float(x) for x in times]
                m2["ping_avg"] = mean(t)
                m2["ping_mdev"] = stdev(t) if len(t) > 1 else 0
        _last_ping = m2
        _last_ping_ts = now
        return m2
    except Exception:
        return {}


# ============================================================
# Gradient-based Presence Detector
# ============================================================
class GradientDetector:
    """
    Rileva presenza basandosi su:
      - RSSI gradient (derivata prima)
      - Consecutive same-sign gradient (pattern di movimento)
      - Opzionale: varianza ping + signal_avg
    """

    def __init__(self, window_size: int = 20, grad_threshold: float = 1.0,
                 consecutive_threshold: int = 3):
        self.rssi_hist = deque(maxlen=window_size)
        self.grad_hist = deque(maxlen=window_size)
        self.signal_avg_hist = deque(maxlen=window_size)
        self.ping_hist = deque(maxlen=window_size)
        self.grad_threshold = grad_threshold
        self.consecutive_threshold = consecutive_threshold
        self.baseline_grad_mean = None
        self.baseline_grad_std = None
        self.calibrated = False
        self._t0 = 0

    def update(self, metrics: dict) -> tuple[bool, dict]:
        """
        Processa un nuovo campione.
        Ritorna: (presenza: bool, debug_info: dict)
        """
        now = time.time()
        if self._t0 == 0:
            self._t0 = now
        info = {"t": round(now - self._t0, 3)}

        # RSSI gradient
        rssi = metrics.get("rssi")
        if rssi is not None:
            self.rssi_hist.append(rssi)
            if len(self.rssi_hist) >= 2:
                grad = rssi - list(self.rssi_hist)[-2]  # derivata prima
                self.grad_hist.append(grad)
                info["grad"] = grad

        # signal_avg
        sa = metrics.get("signal_avg")
        if sa is not None:
            self.signal_avg_hist.append(sa)
            info["signal_avg"] = sa

        # ping jitter
        pm = metrics.get("ping_mdev")
        if pm is not None:
            self.ping_hist.append(pm)
            info["ping_mdev"] = pm

        # --- Calibrazione automatica ---
        if not self.calibrated and len(self.grad_hist) >= 15:
            grads = list(self.grad_hist)
            self.baseline_grad_mean = mean(grads)
            self.baseline_grad_std = stdev(grads) if len(grads) >= 2 else 0.5
            self.calibrated = True
            info["calibrated"] = True

        # --- Presence decision ---
        presence = False
        score = 0.0
        reasons = []

        if self.calibrated:
            # 1. Gradient magnitude score (normalizzato)
            recent_grads = list(self.grad_hist)[-5:] if len(self.grad_hist) >= 5 else list(self.grad_hist)
            if recent_grads:
                max_abs_grad = max(abs(g) for g in recent_grads)
                gs = (max_abs_grad - self.baseline_grad_mean) / max(self.baseline_grad_std, 0.1)
                score += max(0, gs)
                if gs > self.grad_threshold:
                    reasons.append(f"grad={max_abs_grad:.1f}")

            # 2. Consecutive same-sign gradient
            if len(self.grad_hist) >= self.consecutive_threshold:
                recent = list(self.grad_hist)[-self.consecutive_threshold:]
                # Conta consecutivi con stesso segno (non zero)
                non_zero = [g for g in recent if abs(g) > 0.1]
                if len(non_zero) >= 2:
                    cons = 1
                    for i in range(1, len(non_zero)):
                        if non_zero[i] * non_zero[i-1] > 0:
                            cons += 1
                        else:
                            cons = 1
                    cs = cons / self.consecutive_threshold
                    if cs >= 0.8:
                        score += cs * 2
                        reasons.append(f"cons={cons}")

            # 3. Ping jitter bonus
            if len(self.ping_hist) >= 3:
                recent_ping = list(self.ping_hist)[-3:]
                jitter = stdev(recent_ping) if len(recent_ping) > 1 else 0
                if jitter > 5:  # >5ms jitter = anomalo
                    score += 1
                    reasons.append(f"jitter={jitter:.1f}ms")

            presence = score > 1.5

        info["score"] = round(score, 2)
        info["presence"] = presence
        info["reasons"] = reasons
        info["calibrated"] = self.calibrated
        return presence, info


def get_detector_metrics(iface: str, gw: str = None) -> dict:
    """Raccoglie tutte le metriche e le fonde."""
    m = get_wifi_metrics(iface)
    ping = get_ping_metrics(gw)
    m.update(ping)
    return m


# ============================================================
# Sampling
# ============================================================
def collect_samples(iface: str, seconds: int, label: str, gw: str = None) -> list[dict]:
    """Raccoglie campioni per N secondi."""
    print(f"\n  Collezione '{label}' — {seconds}s su {iface}")
    print(f"  {'MUOVITI' if label == 'movement' else 'RIMANI FERMO'} durante la fase '{label}'.\n")

    samples = []
    start = time.time()
    last_report = 0
    det = GradientDetector()

    try:
        while time.time() - start < seconds:
            m = get_detector_metrics(iface, gw)
            m["timestamp"] = time.time()
            m["t"] = round(time.time() - start, 3)
            m["label"] = label
            presence, debug = det.update(m)
            m["_debug"] = debug
            samples.append(m)

            # Report ogni 5s
            elapsed = time.time() - start
            if elapsed - last_report >= 5:
                rssi_vals = [s.get("rssi") for s in samples if s.get("rssi") is not None]
                n = len(samples)
                rate = n / elapsed if elapsed > 0 else 0
                line = f"    {elapsed:.0f}s — {n} campioni ({rate:.1f}/s)"
                if rssi_vals:
                    line += f", RSSI: {min(rssi_vals):.0f}..{max(rssi_vals):.0f} dBm"
                    if len(rssi_vals) >= 2:
                        line += f", std={stdev(rssi_vals):.2f}"
                print(line)
                last_report = elapsed

            time.sleep(SAMPLING_INTERVAL)

    except KeyboardInterrupt:
        print(f"\n  Interrotto dopo {time.time()-start:.0f}s")

    filename = f"enh_calib_{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    filepath = os.path.join(REPORT_DIR, filename)
    # Salva senza i _debug per pulizia
    save_samples = [{k: v for k, v in s.items() if k != "_debug"} for s in samples]
    with open(filepath, "w") as f:
        json.dump({"label": label, "samples": save_samples,
                    "iface": iface, "duration_s": round(time.time()-start, 1)},
                   f, indent=2)
    print(f"\n  Salvati {len(samples)} campioni in {filename}")
    return samples


# ============================================================
# Analysis
# ============================================================
def analyze(baseline: list[dict], movement: list[dict]):
    """Analisi gradient-based per trovare soglia ottimale."""
    print("\n" + "=" * 60)
    print("  ANALISI — Enhanced Presence Detection")
    print("=" * 60)

    b_rssi = [s.get("rssi") for s in baseline if s.get("rssi") is not None]
    m_rssi = [s.get("rssi") for s in movement if s.get("rssi") is not None]

    if len(b_rssi) < 10:
        print("  ERRORE: almeno 10 campioni RSSI necessari per baseline")
        return
    if len(m_rssi) < 10:
        print("  ERRORE: almeno 10 campioni RSSI necessari per movement")
        return

    # Statistiche base
    print(f"\n  --- Statistiche RSSI ---")
    print(f"  {'':>15} {'Baseline':>12} {'Movement':>12}")
    print(f"  {'Campioni':>15} {len(b_rssi):>12} {len(m_rssi):>12}")
    print(f"  {'Media':>15} {mean(b_rssi):>12.2f} {mean(m_rssi):>12.2f}")
    print(f"  {'Std Dev':>15} {stdev(b_rssi):>12.2f} {stdev(m_rssi):>12.2f}")
    print(f"  {'Min':>15} {min(b_rssi):>12.0f} {min(m_rssi):>12.0f}")
    print(f"  {'Max':>15} {max(b_rssi):>12.0f} {max(m_rssi):>12.0f}")

    # Calcola gradienti
    b_grads = [b_rssi[i+1] - b_rssi[i] for i in range(len(b_rssi)-1)]
    m_grads = [m_rssi[i+1] - m_rssi[i] for i in range(len(m_rssi)-1)]

    b_abs_grads = [abs(g) for g in b_grads]
    m_abs_grads = [abs(g) for g in m_grads]

    print(f"\n  --- Statistiche Gradiente (|dRSSI/dt|) ---")
    print(f"  {'':>15} {'Baseline':>12} {'Movement':>12}")
    print(f"  {'Campioni':>15} {len(b_abs_grads):>12} {len(m_abs_grads):>12}")
    print(f"  {'Media':>15} {mean(b_abs_grads):>12.3f} {mean(m_abs_grads):>12.3f}")
    print(f"  {'Std Dev':>15} {stdev(b_abs_grads):>12.3f} {stdev(m_abs_grads):>12.3f}")
    print(f"  {'Max':>15} {max(b_abs_grads):>12.0f} {max(m_abs_grads):>12.0f}")

    # Trova soglia ottimale per gradient magnitude
    print(f"\n  --- Strategia: Soglia Gradiente ---")
    print(f"  {'Soglia':>8} {'FP':>8} {'TP':>8} {'Score':>8} {'Verdetto':>12}")
    best_th = None
    best_score = -1

    for th in [round(x * 0.1, 1) for x in range(1, 31)]:
        fp = sum(1 for g in b_abs_grads if g > th) / len(b_abs_grads)
        tp = sum(1 for g in m_abs_grads if g > th) / len(m_abs_grads)
        score = tp - fp
        if score > best_score:
            best_score = score
            best_th = th
        if th <= 3.0 or score >= 0.1:
            verdict = "OK" if score > 0.3 else \
                      ("FALSI POS" if fp > 0.3 else "POCO")
            print(f"  {th:>8.1f} {fp:>8.0%} {tp:>8.0%} {score:>8.2f} {verdict:>12}")

    # Trova soglia ottimale per consecutive same-sign
    print(f"\n  --- Strategia: Consecutive Same-Sign (N consecutivi = soglia) ---")
    print(f"  {'Soglia N':>8} {'FP':>8} {'TP':>8} {'Score':>8} {'Verdetto':>12}")
    best_n_th = None
    best_n_score = -1

    for n in range(2, 15):
        fp = sum(1 for i in range(len(b_grads)-n+1)
                 if all(abs(g) > 0.1 and g > 0 for g in b_grads[i:i+n]) or
                    all(abs(g) > 0.1 and g < 0 for g in b_grads[i:i+n])) / max(len(b_grads)-n+1, 1)
        tp = sum(1 for i in range(len(m_grads)-n+1)
                 if all(abs(g) > 0.1 and g > 0 for g in m_grads[i:i+n]) or
                    all(abs(g) > 0.1 and g < 0 for g in m_grads[i:i+n])) / max(len(m_grads)-n+1, 1)
        score = tp - fp
        if score > best_n_score:
            best_n_score = score
            best_n_th = n
        verdict = "OK" if score > 0.1 else ("FALSI POS" if fp > 0.1 else "NON RILEVA")
        print(f"  {n:>8d} {fp:>8.0%} {tp:>8.0%} {score:>8.2f} {verdict:>12}")

    # Raccomandazioni
    print(f"\n  --- Raccomandazioni ---")
    print(f"  Ambiente: {len(b_rssi)} campioni, segnale ~{mean(b_rssi):.0f} dBm")
    print(f"  Gradiente: soglia = {best_th or 'N/A'}, score = {best_score:.2f}" if best_th else "  Gradiente: nessuna soglia utile")
    print(f"  Consecutive: N = {best_n_th or 'N/A'}, score = {best_n_score:.2f}" if best_n_th else "  Consecutive: nessuna soglia utile")

    method = "gradient" if best_score > 0.1 else "consecutive" if best_n_score > 0.1 else "std_fallback"
    print(f"\n  Per l'uso nel decision engine:")
    if method == "gradient":
        print(f"    detector = GradientDetector(")
        print(f"        grad_threshold={best_th})")
    elif method == "consecutive":
        print(f"    detector = GradientDetector(")
        print(f"        consecutive_threshold={best_n_th})")

    # Salva report
    report = {
        "timestamp": datetime.now().isoformat(),
        "method": method,
        "recommended": {
            "grad_threshold": best_th,
            "consecutive_threshold": best_n_th,
        },
        "baseline": {"n": len(b_rssi), "mean": mean(b_rssi), "std": stdev(b_rssi)},
        "movement": {"n": len(m_rssi), "mean": mean(m_rssi), "std": stdev(m_rssi)},
        "gradient": {
            "baseline_mean": mean(b_abs_grads),
            "movement_mean": mean(m_abs_grads),
        }
    }
    report_file = f"enh_calib_report_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(os.path.join(REPORT_DIR, report_file), "w") as f:
        json.dump(report, f, indent=2)
    print(f"  Report salvato: {report_file}")


# ============================================================
# Monitor real-time
# ============================================================
def monitor(iface: str, grad_th: float = 1.0, cons_th: int = 3):
    """Monitoraggio real-time con GradientDetector."""
    gw = detect_gateway()
    det = GradientDetector(grad_threshold=grad_th,
                           consecutive_threshold=cons_th)

    print(f"\n  Monitoraggio real-time")
    print(f"  Soglia gradiente: {grad_th}, consecutive: {cons_th}")
    print(f"  Gateway: {gw or 'N/D'}")
    print(f"  {'Tempo':>6} {'RSSI':>6} {'SigAvg':>7} {'Grad':>5} {'Ping':>6} {'Score':>6} {'Stato':>10}")
    print(f"  {'-'*52}")

    start = time.time()
    t_prev = start
    try:
        while True:
            now = time.time()
            metrics = get_detector_metrics(iface, gw)
            presence, debug = det.update(metrics)
            t = now - start

            # Formatta output
            rssi_s = str(metrics.get("rssi", "-")) if metrics.get("rssi") is not None else "-"
            sa_s = str(metrics.get("signal_avg", "-"))
            grad_s = f"{debug.get('grad', 0):+.0f}" if "grad" in debug else "-"
            ping_s = f"{metrics.get('ping_avg', 0):.0f}" if metrics.get("ping_avg") is not None else "-"
            score_s = f"{debug['score']:.1f}"
            status_s = "PRESENTE!" if presence else "vuoto"
            print(f"  {t:>6.1f} {rssi_s:>6} {sa_s:>7} {grad_s:>5} {ping_s:>6} {score_s:>6} {status_s:>10}")

            time.sleep(SAMPLING_INTERVAL)

    except KeyboardInterrupt:
        print(f"\n  Monitor terminato dopo {t:.0f}s")


# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Enhanced Presence Detection — UNO Q")
    parser.add_argument("--mode", choices=["baseline", "movement", "analyze",
                                            "monitor", "quick"],
                        default="quick")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--grad-threshold", type=float, default=1.0,
                        help="Soglia gradiente per monitor (default: 1.0)")
    parser.add_argument("--cons-threshold", type=int, default=3,
                        help="Consecutive same-sign per monitor (default: 3)")
    args = parser.parse_args()

    iface = None
    try:
        out = subprocess.check_output("ip link show", shell=True, timeout=5).decode()
        for m in re.finditer(r"^\d+:\s+(\S+):", out, re.MULTILINE):
            name = m.group(1).strip(":")
            if re.match(r"^(wlan|wlx|wlp)", name):
                iface = name
                break
    except Exception:
        pass
    if not iface:
        try:
            for entry in sorted(os.listdir("/sys/class/net")):
                if re.match(r"^(wlan|wlx)", entry):
                    iface = entry
                    break
        except FileNotFoundError:
            pass
    if not iface:
        print("ERRORE: nessuna interfaccia WiFi trovata")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  Enhanced Presence Detection — {iface}")
    print(f"{'='*60}")

    if args.mode == "quick":
        print(f"\n{'='*60}")
        print(f"  CALIBRAZIONE RAPIDA")
        print(f"{'='*60}")
        print("Fase 1/3: Collezione BASELINE (stai fermo)")
        bl = collect_samples(iface, args.seconds, "baseline")
        print("\nFase 2/3: Collezione MOVEMENT (cammina nella stanza)")
        mv = collect_samples(iface, args.seconds, "movement")
        print("\nFase 3/3: Analisi")
        analyze(bl, mv)

    elif args.mode == "baseline":
        collect_samples(iface, args.seconds, "baseline")
    elif args.mode == "movement":
        collect_samples(iface, args.seconds, "movement")
    elif args.mode == "analyze":
        import glob
        bl_files = sorted(glob.glob(os.path.join(REPORT_DIR, "enh_calib_baseline_*.json")))
        mv_files = sorted(glob.glob(os.path.join(REPORT_DIR, "enh_calib_movement_*.json")))
        if not bl_files or not mv_files:
            print("ERRORE: file di calibrazione non trovati. Esegui --mode baseline e --mode movement prima.")
            return
        with open(bl_files[-1]) as f: bl = json.load(f)["samples"]
        with open(mv_files[-1]) as f: mv = json.load(f)["samples"]
        analyze(bl, mv)
    elif args.mode == "monitor":
        monitor(iface, args.grad_threshold, args.cons_threshold)


if __name__ == "__main__":
    main()
