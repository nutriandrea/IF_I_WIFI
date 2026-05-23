#!/usr/bin/env python3
"""
diagnose_model.py — verifica se il modello posizioni e' compatibile
con lo stato attuale del setup.

Stampa:
  1. MAC/source noti dal modello (registrati durante il training)
  2. Classi etichetta nel modello
  3. Frame UDP in arrivo nelle prossime 10s: per RX (node_id) e per MAC
  4. Diagnosi: i MAC visti combaciano con quelli del training?

Uso:
    python3 -m csi.diagnose_model
    python3 -m csi.diagnose_model --udp-port 5005 --seconds 15
"""
from __future__ import annotations
import argparse
import json
import os
import socket
import sys
import time
from collections import Counter

from . import csi_ml
from .csi_processor import parse_csi_binary, parse_csi_line


def load_model_metadata(labels_path: str) -> dict:
    if not os.path.exists(labels_path):
        return {}
    with open(labels_path) as f:
        d = json.load(f)
    if isinstance(d, list):
        return {"class_labels": d, "known_sources": [], "source_key": "mac"}
    return {
        "class_labels": d.get("class_labels", []),
        "known_sources": d.get("known_sources", d.get("known_macs", [])),
        "source_key": d.get("source_key", "mac"),
    }


def capture_udp(port: int, seconds: int) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.5)
    print(f"\n  Ascolto frame UDP per {seconds}s su 0.0.0.0:{port}...")

    by_node: Counter = Counter()      # rx_node_id -> count
    by_mac: Counter = Counter()       # source_mac -> count
    pairs: Counter = Counter()        # (node_id, mac) -> count
    total = 0
    text_buf = bytearray()

    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue

        # Binary ADR-018?
        if len(data) >= 4:
            magic = int.from_bytes(data[:4], "little")
            if magic == 0xC5110001:
                parsed = parse_csi_binary(data)
                if parsed:
                    node_id = parsed.get("_node_id", parsed.get("ap_id", -1))
                    mac = parsed.get("mac") or "(none)"
                    by_node[node_id] += 1
                    by_mac[mac] += 1
                    pairs[(node_id, mac)] += 1
                    total += 1
                    continue

        # Testo?
        text_buf.extend(data)
        while b"\n" in text_buf:
            line, _, text_buf = text_buf.partition(b"\n")
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            if not text.startswith("CSI:"):
                continue
            parsed = parse_csi_line(text)
            if parsed:
                node_id = parsed.get("ap_id", -1)
                mac = parsed.get("mac") or "(none)"
                by_node[node_id] += 1
                by_mac[mac] += 1
                pairs[(node_id, mac)] += 1
                total += 1

    sock.close()
    return {
        "total": total,
        "seconds": seconds,
        "by_node": dict(by_node),
        "by_mac": dict(by_mac),
        "pairs": pairs,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Diagnostica compatibilita' modello vs setup attuale")
    ap.add_argument("--labels", default=csi_ml.POSITIONS_LABELS_PATH,
                    help="Path al JSON delle etichette (default: csi/csi_positions_labels.json)")
    ap.add_argument("--udp-port", type=int, default=5005)
    ap.add_argument("--seconds", type=int, default=10)
    args = ap.parse_args()

    print("=" * 62)
    print("  DIAGNOSTICA MODELLO POSIZIONI")
    print("=" * 62)

    # 1. Metadata del modello
    meta = load_model_metadata(args.labels)
    if not meta:
        print(f"\n  ATTENZIONE: nessun file '{args.labels}' trovato.")
        print(f"  Il modello non e' stato ancora addestrato con --positions.")
    else:
        labels = meta.get("class_labels", [])
        sources = meta.get("known_sources", [])
        key = meta.get("source_key", "mac")
        print(f"\n  Classi nel modello ({len(labels)}):")
        for l in labels:
            print(f"    - {l}")
        print(f"\n  Sorgenti note ({key}) ({len(sources)}):")
        for s in sources:
            disp = s if len(s) <= 17 else f"...{s[-8:]}"
            print(f"    - {disp}")

    # 2. Cattura UDP attuale
    cap = capture_udp(args.udp_port, args.seconds)
    total = cap["total"]
    print(f"\n  Catturati {total} frame in {cap['seconds']}s "
          f"({total / cap['seconds']:.1f} Hz totale)")

    # 3. Frame per RX (NODE_ID)
    print(f"\n  Frame per ricevitore (NODE_ID):")
    if not cap["by_node"]:
        print(f"    NESSUN frame ricevuto. Gli ESP32 non stanno mandando UDP!")
    else:
        for node, cnt in sorted(cap["by_node"].items()):
            rate = cnt / cap["seconds"]
            star = "" if rate > 10 else "  ← BASSO"
            print(f"    NODE_ID {node}: {cnt} frame ({rate:.1f} Hz){star}")

    # 4. Frame per MAC pinger
    print(f"\n  Frame per MAC sorgente (pinger):")
    if not cap["by_mac"]:
        print(f"    NESSUN MAC pinger visto.")
    else:
        sorted_macs = sorted(cap["by_mac"].items(), key=lambda x: -x[1])
        for mac, cnt in sorted_macs[:10]:
            rate = cnt / cap["seconds"]
            mac_short = mac if len(mac) <= 17 else f"...{mac[-8:]}"
            print(f"    {mac_short}: {cnt} frame ({rate:.1f} Hz)")

    # 5. Diagnostica match
    if meta and meta.get("known_sources"):
        seen_macs = set(cap["by_mac"].keys()) - {"(none)"}
        known_macs = set(meta["known_sources"])

        seen_known = seen_macs & known_macs
        seen_unknown = seen_macs - known_macs
        missing_known = known_macs - seen_macs

        print(f"\n  COMPATIBILITA' MODELLO vs SETUP ATTUALE:")
        print(f"    Sorgenti note dal modello:    {len(known_macs)}")
        print(f"    Sorgenti viste ora:           {len(seen_macs)}")
        print(f"    Sorgenti riconosciute:        {len(seen_known)}")
        print(f"    Sorgenti nuove (sconosciute): {len(seen_unknown)}")
        print(f"    Sorgenti mancanti:            {len(missing_known)}")

        if seen_unknown:
            print(f"\n    Sorgenti nuove (IGNORATE dal modello):")
            for m in seen_unknown:
                disp = m if len(m) <= 17 else f"...{m[-8:]}"
                print(f"      - {disp}")
        if missing_known:
            print(f"\n    Sorgenti del training non viste (FEATURE A ZERO):")
            for m in missing_known:
                disp = m if len(m) <= 17 else f"...{m[-8:]}"
                print(f"      - {disp}")

        # Verdetto
        print(f"\n  VERDETTO:")
        if not seen_known and missing_known:
            print(f"    ✗ CATASTROFE: nessuna sorgente del training e' attiva ora.")
            print(f"      Probabilmente le MAC dei pinger sono cambiate (randomization).")
            print(f"      Fix: re-train con questi pinger nello stato attuale.")
        elif missing_known and seen_known:
            pct = len(seen_known) / len(known_macs) * 100
            print(f"    ⚠ PARZIALE: {pct:.0f}% delle sorgenti note sono attive.")
            print(f"      Modello degradato. Re-train consigliato.")
        elif not missing_known:
            print(f"    ✓ OK: tutte le sorgenti note sono attive.")
            print(f"      Se la predizione e' comunque cattiva, il problema NON e' MAC.")
            print(f"      Verifica: rate per RX bilanciato? Classi bilanciate nel training?")

    return 0


if __name__ == "__main__":
    sys.exit(main())
