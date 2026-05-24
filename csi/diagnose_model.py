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
from .csi_processor import (
    parse_csi_binary,
    parse_csi_line,
    parse_csi_radar3d,
    CSI_BINARY_MAGIC,
    CSI_RADAR3D_MAGIC,
)


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
    by_mac: Counter = Counter()       # source_mac or pair_id -> count
    pairs: Counter = Counter()        # (rx_node, source) -> count
    n_radar3d = 0
    n_adr018 = 0
    total = 0
    text_buf = bytearray()

    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue

        if len(data) >= 4:
            magic = int.from_bytes(data[:4], "little")

            # Radar3D cross-ping (nuovo firmware, magic 0xC5110003)
            if magic == CSI_RADAR3D_MAGIC:
                parsed = parse_csi_radar3d(data)
                if parsed:
                    n_radar3d += 1
                    node_id = parsed.get("rx_node", -1)
                    pair = parsed.get("_pair_id") or parsed.get("mac") or "(none)"
                    by_node[node_id] += 1
                    by_mac[pair] += 1
                    pairs[(node_id, pair)] += 1
                    total += 1
                    continue

            # ADR-018 (vecchio firmware esp32_csi_firmware, magic 0xC5110001)
            if magic == CSI_BINARY_MAGIC:
                parsed = parse_csi_binary(data)
                if parsed:
                    n_adr018 += 1
                    node_id = parsed.get("_node_id", parsed.get("ap_id", -1))
                    mac = parsed.get("mac") or "(none)"
                    by_node[node_id] += 1
                    by_mac[mac] += 1
                    pairs[(node_id, mac)] += 1
                    total += 1
                    continue

        # Testo? (fallback legacy serial)
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
        "n_radar3d": n_radar3d,
        "n_adr018": n_adr018,
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
    if cap["n_radar3d"] or cap["n_adr018"]:
        print(f"    Radar3D cross-ping (0xC5110003): {cap['n_radar3d']}")
        print(f"    ADR-018 single-source (0xC5110001): {cap['n_adr018']}")

    is_radar3d = cap["n_radar3d"] > cap["n_adr018"]
    src_label = "coppia (rx-tx)" if is_radar3d else "MAC sorgente"

    # 3. Frame per RX (NODE_ID)
    print(f"\n  Frame per ricevitore (NODE_ID):")
    if not cap["by_node"]:
        print(f"    NESSUN frame ricevuto. Gli ESP32 non stanno mandando UDP!")
    else:
        for node, cnt in sorted(cap["by_node"].items()):
            rate = cnt / cap["seconds"]
            star = "" if rate > 10 else "  ← BASSO"
            print(f"    NODE_ID {node}: {cnt} frame ({rate:.1f} Hz){star}")

    # 4. Frame per sorgente
    print(f"\n  Frame per {src_label}:")
    if not cap["by_mac"]:
        print(f"    NESSUNA sorgente vista.")
    else:
        sorted_src = sorted(cap["by_mac"].items(), key=lambda x: -x[1])
        for src, cnt in sorted_src[:12]:
            rate = cnt / cap["seconds"]
            disp = src if len(src) <= 17 else f"...{src[-8:]}"
            print(f"    {disp}: {cnt} frame ({rate:.1f} Hz)")

    if is_radar3d:
        expected = 9  # 3 RX × 3 TX
        actual = len(cap["by_mac"]) - (1 if "(none)" in cap["by_mac"] else 0)
        if actual < expected:
            print(f"\n  ⚠ Attese {expected} coppie rx-tx, viste {actual}. "
                  f"Verifica che TUTTI e 3 gli ESP32 siano accesi.")
        else:
            print(f"\n  ✓ {actual} coppie rx-tx attive (atteso {expected}).")

    # 5. Diagnostica match
    if meta and meta.get("known_sources"):
        seen_sources = set(cap["by_mac"].keys()) - {"(none)"}
        known_sources = set(meta["known_sources"])

        seen_known = seen_sources & known_sources
        seen_unknown = seen_sources - known_sources
        missing_known = known_sources - seen_sources

        print(f"\n  COMPATIBILITA' MODELLO vs SETUP ATTUALE:")
        print(f"    Sorgenti note dal modello:    {len(known_sources)}")
        print(f"    Sorgenti viste ora:           {len(seen_sources)}")
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
            if is_radar3d:
                print(f"      Le coppie rx-tx attese non arrivano. Cause possibili:")
                print(f"      - Non tutti i 3 ESP32 sono accesi/connessi")
                print(f"      - NODE_MACS in network_config.h diversi da quelli reali")
                print(f"      - WIFI canale diverso dal CHANNEL del firmware (default 6)")
            else:
                print(f"      Probabilmente le MAC dei pinger sono cambiate (randomization).")
                print(f"      Considera passare al firmware esp32_radar3d (cross-ping, MAC fissi).")
            print(f"      Fix: re-train nello stato attuale dopo aver risolto.")
        elif missing_known and seen_known:
            pct = len(seen_known) / len(known_sources) * 100
            print(f"    ⚠ PARZIALE: {pct:.0f}% delle sorgenti note sono attive.")
            if is_radar3d:
                print(f"      Manca uno o piu' ESP32 (NODE_ID {sorted(missing_known)}).")
            print(f"      Modello degradato. Re-train consigliato.")
        elif not missing_known:
            print(f"    ✓ OK: tutte le sorgenti note sono attive.")
            print(f"      Se la predizione e' comunque cattiva, il problema NON e' la sorgente.")
            print(f"      Verifica: rate per RX bilanciato? Classi bilanciate nel training?")

    return 0


if __name__ == "__main__":
    sys.exit(main())
