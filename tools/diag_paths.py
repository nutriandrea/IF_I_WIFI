#!/usr/bin/env python3
"""
diag_paths.py — Diagnostica della rete cross-ping 3-ESP32.

Ascolta UDP per N secondi e stampa una tabella 3×3 di tutti i percorsi
(tx_node, rx_node) → fps + varianza media dell'ampiezza CSI.

Serve a capire IMMEDIATAMENTE:
  - quali ESP32 stanno trasmettendo (colonne TX)
  - quali ESP32 stanno ricevendo (righe RX)
  - se uno dei MAC in network_config.h è sbagliato (paths < 9)
  - se la varianza di un RX è zero (CSI bloccato)

Uso:
    PYTHONPATH=. python3 tools/diag_paths.py
    PYTHONPATH=. python3 tools/diag_paths.py --seconds 15 --port 5005

Output tipico (situazione SANA):
    ┌─────┬─────────────┬─────────────┬─────────────┐
    │     │   TX0       │   TX1       │   TX2       │
    ├─────┼─────────────┼─────────────┼─────────────┤
    │ RX0 │ 98.2 fps    │ 99.1 fps    │ 97.8 fps    │
    │     │ var=12.4    │ var=8.9     │ var=10.1    │
    │ RX1 │ 99.5 fps    │ 100.0 fps   │ 98.9 fps    │
    │     │ var=9.2     │ var=11.8    │ var=8.5     │
    │ RX2 │ 98.7 fps    │ 98.3 fps    │ 99.2 fps    │
    │     │ var=7.6     │ var=8.1     │ var=12.9    │
    └─────┴─────────────┴─────────────┴─────────────┘
    9 paths attivi · ✅ SETUP OK

Output tipico (situazione ROTTA — TX1 non riconosciuto):
    │ RX0 │ 98.2 fps    │ ---         │ 97.8 fps    │
    │ RX1 │ 99.5 fps    │ ---         │ 98.9 fps    │
    │ RX2 │ 98.7 fps    │ ---         │ 99.2 fps    │
    6 paths attivi · ⚠ TX1 NON VISIBILE.
    Probabili cause:
      - L'ESP32 con NODE_ID=1 non sta trasmettendo (controlla Serial Monitor)
      - NODE_MACS[1] in network_config.h non è il vero MAC del chip NODE_ID=1
      - Due ESP32 sono stati flashati con lo stesso NODE_ID
"""
from __future__ import annotations

import argparse
import socket
import statistics
import sys
import time
from collections import defaultdict
from typing import Any

sys.path.insert(0, "."); sys.path.insert(0, "..")  # tollerante a launch da dirs diverse

from csi.csi_processor import (
    parse_csi_radar3d, parse_csi_crossping, parse_csi_binary, parse_csi_line,
)


# ============================================================
# Capture
# ============================================================
def capture(port: int, seconds: int) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Try to bind to a specific interface if needed, or just handle the error
    try:
        sock.bind(("", port))
    except OSError as e:
        if e.errno == 48:
            print(f"  Errore: Porta {port} già in uso. Chiudi altri processi che usano questa porta.", file=sys.stderr)
            sys.exit(1)
        raise e
    sock.settimeout(0.2)

    counts: dict[tuple[int, int], int] = defaultdict(int)
    amps_by_path: dict[tuple[int, int], list[float]] = defaultdict(list)
    macs_seen: dict[str, int] = defaultdict(int)
    other_macs: dict[str, int] = defaultdict(int)
    total_frames = 0
    invalid_frames = 0
    rx_node_seen: set[int] = set()
    tx_node_seen: set[int] = set()
    text_buf = bytearray()
    unknown_magics: dict[str, int] = defaultdict(int)

    t0 = time.time()
    print(f"  Ascolto UDP :{port} per {seconds}s...", file=sys.stderr)
    while time.time() - t0 < seconds:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break

        total_frames += 1
        if len(data) >= 4:
            magic = int.from_bytes(data[:4], "little")
            parsed: dict[str, Any] | None = None
            if magic == 0xC5110003:
                parsed = parse_csi_radar3d(data)
            elif magic == 0xC5110002:
                parsed = parse_csi_crossping(data)
            elif magic == 0xC5110001:
                parsed = parse_csi_binary(data)
            elif 0xC5110000 <= magic <= 0xC511FFFF:
                unknown_magics[f"0x{magic:08x}"] += 1
            if parsed is not None:
                tx = int(parsed.get("tx_node", -1))
                rx = int(parsed.get("rx_node", -1))
                if tx >= 0 and rx >= 0:
                    counts[(tx, rx)] += 1
                    amps_by_path[(tx, rx)].append(float(parsed.get("ampl_mean", 0.0)))
                    tx_node_seen.add(tx)
                    rx_node_seen.add(rx)
                mac = parsed.get("mac")
                if isinstance(mac, str):
                    macs_seen[mac] += 1
                continue

        # text fallback
        text_buf.extend(data)
        while b"\n" in text_buf:
            line, _, text_buf = text_buf.partition(b"\n")
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            if text.startswith("CSI:"):
                p = parse_csi_line(text)
                if p:
                    mac = p.get("mac")
                    if isinstance(mac, str):
                        other_macs[mac] += 1
                    # Frame testo legacy: niente tx/rx node id

    sock.close()

    elapsed = time.time() - t0
    return {
        "counts": dict(counts),
        "amps_by_path": dict(amps_by_path),
        "total": total_frames,
        "invalid": invalid_frames,
        "elapsed": elapsed,
        "rx_seen": rx_node_seen,
        "tx_seen": tx_node_seen,
        "macs_seen": dict(macs_seen),
        "other_macs": dict(other_macs),
        "unknown_magics": dict(unknown_magics),
    }


# ============================================================
# Pretty table
# ============================================================
def render_table(report: dict, n_nodes: int = 3) -> str:
    counts = report["counts"]
    amps = report["amps_by_path"]
    elapsed = report["elapsed"]

    # Costruisce statistiche per cella
    cells: dict[tuple[int, int], tuple[float, float]] = {}
    for tx in range(n_nodes):
        for rx in range(n_nodes):
            c = counts.get((tx, rx), 0)
            fps = c / max(elapsed, 1e-6)
            a = amps.get((tx, rx), [])
            if len(a) >= 2:
                var = statistics.pvariance(a)
            else:
                var = 0.0
            cells[(tx, rx)] = (fps, var)

    cell_w = 16
    lines: list[str] = []

    def hr(top=False, mid=False, bot=False) -> str:
        l, m, r = "┌", "┬", "┐"
        if mid: l, m, r = "├", "┼", "┤"
        if bot: l, m, r = "└", "┴", "┘"
        return l + ("─" * (5 + 2)) + m + ((("─" * cell_w) + m) * n_nodes)[:-1] + r

    lines.append(hr(top=True))
    header = "│ " + " " * 4 + "│" + "".join(f" {f'TX{i}':<{cell_w-1}}│" for i in range(n_nodes))
    lines.append(header)
    lines.append(hr(mid=True))
    for rx in range(n_nodes):
        row1 = f"│ RX{rx} │"
        row2 = "│     │"
        for tx in range(n_nodes):
            fps, var = cells[(tx, rx)]
            if fps < 0.1:
                row1 += f" {'---':<{cell_w-1}}│"
                row2 += f" {' ':<{cell_w-1}}│"
            else:
                row1 += f" {f'{fps:5.1f} fps':<{cell_w-1}}│"
                row2 += f" {f'var={var:7.2f}':<{cell_w-1}}│"
        lines.append(row1); lines.append(row2)
    lines.append(hr(bot=True))
    return "\n".join(lines)


# ============================================================
# Diagnosis
# ============================================================
def diagnose(report: dict, n_nodes: int = 3) -> tuple[int, list[str]]:
    """Ritorna (exit_code, messaggi_diagnostici)."""
    msgs: list[str] = []
    counts = report["counts"]
    elapsed = report["elapsed"]

    if not counts:
        msgs.append("✗ NESSUN FRAME radar3d/crossping ricevuto.")
        if report["other_macs"]:
            msgs.append(f"  (Visti {sum(report['other_macs'].values())} frame in formato testo legacy)")
        if report["unknown_magics"]:
            for m, c in report["unknown_magics"].items():
                msgs.append(f"  (Visti {c} frame con magic sconosciuto {m})")
        msgs.append("  → Gli ESP32 non stanno inviando UDP al PC, oppure il formato è sbagliato.")
        msgs.append("    Verifica: a) UDP_TARGET_IP nel firmware = IP del PC,")
        msgs.append("              b) tutti gli ESP32 stampano 'CSI attivo' nel Serial Monitor.")
        return 2, msgs

    # Quante celle attive
    active = sum(1 for tx in range(n_nodes) for rx in range(n_nodes)
                 if counts.get((tx, rx), 0) >= elapsed * 5)  # almeno 5 fps
    expected = n_nodes * n_nodes
    msgs.append(f"📊 {active}/{expected} percorsi attivi (>= 5 fps).")

    # TX o RX mancanti?
    tx_active = [tx for tx in range(n_nodes)
                 if any(counts.get((tx, rx), 0) >= elapsed * 5 for rx in range(n_nodes))]
    rx_active = [rx for rx in range(n_nodes)
                 if any(counts.get((tx, rx), 0) >= elapsed * 5 for tx in range(n_nodes))]

    missing_tx = [t for t in range(n_nodes) if t not in tx_active]
    missing_rx = [r for r in range(n_nodes) if r not in rx_active]

    if missing_tx:
        for tx in missing_tx:
            msgs.append(f"⚠  TX{tx} NON VISIBILE (nessun nodo riceve i suoi ping).")
            msgs.append(f"    Cause più probabili:")
            msgs.append(f"      a) L'ESP32 con NODE_ID={tx} non sta trasmettendo")
            msgs.append(f"         → controlla il Serial Monitor: vedi '[{tx}] TX:xxxx CSI:yyyy' ogni 5s?")
            msgs.append(f"      b) NODE_MACS[{tx}] in network_config.h non corrisponde al MAC reale")
            msgs.append(f"         del chip flashato con NODE_ID={tx}")
            msgs.append(f"         → apri Serial Monitor di quell'ESP32 e confronta")
            msgs.append(f"      c) Due ESP32 hanno lo stesso NODE_ID (entrambi pensano di essere lo {tx_active[0]})")

    if missing_rx:
        for rx in missing_rx:
            msgs.append(f"⚠  RX{rx} NON RICEVE (nessun ping arriva da quel nodo).")
            msgs.append(f"    Cause più probabili:")
            msgs.append(f"      a) L'ESP32 con NODE_ID={rx} non sta inviando UDP")
            msgs.append(f"         → forse non è connesso al WiFi (verifica Serial Monitor)")
            msgs.append(f"      b) Il suo CSI callback non si attiva (config CSI fallita al boot)")

    # Varianze tutte zero?
    var_per_rx = {}
    for rx in range(n_nodes):
        ampls = []
        for tx in range(n_nodes):
            ampls.extend(report["amps_by_path"].get((tx, rx), []))
        if len(ampls) >= 2:
            var_per_rx[rx] = statistics.pvariance(ampls)

    if var_per_rx and max(var_per_rx.values()) < 0.01:
        msgs.append("⚠  VARIANZA QUASI ZERO su tutti i RX — il CSI sembra costante.")
        msgs.append("    Tipicamente significa che i frame sono identici (frame finti?)")
        msgs.append("    o che il driver CSI dell'ESP32 non sta producendo dati reali.")

    if active == expected:
        msgs.append("✅ SETUP HARDWARE OK.")
        # Verifica simmetria varianze
        if var_per_rx:
            vmax = max(var_per_rx.values())
            vmin = min(var_per_rx.values())
            if vmax > 0 and (vmax - vmin) / vmax < 0.1:
                msgs.append("⚠  Le varianze dei 3 RX sono molto simili (<10% scarto).")
                msgs.append("    → Movimento poco discriminante: il blob tenderà al centro stanza.")
                msgs.append("    Suggerimenti:")
                msgs.append("      - Verifica che la persona si muova abbastanza ampiamente")
                msgs.append("      - Allontana di più i 3 nodi tra loro (diversità spaziale)")
                msgs.append("      - Aumenta la finestra (--window 200 nel server)")
        return 0, msgs

    return 1, msgs


# ============================================================
# Main
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnostica 9-path della rete cross-ping ESP32")
    ap.add_argument("--port", type=int, default=5005, help="UDP port (default: 5005)")
    ap.add_argument("--seconds", type=int, default=10, help="Durata cattura (default: 10s)")
    ap.add_argument("--nodes", type=int, default=3, help="Numero nodi attesi (default: 3)")
    args = ap.parse_args()

    print(f"\n  Diagnostica rete cross-ping — {args.seconds}s di cattura")
    print(f"  (durante la cattura non muoverti, oppure muoviti uniformemente)\n")

    report = capture(args.port, args.seconds)

    # Tabella
    table = render_table(report, args.nodes)
    print(table)
    print()
    print(f"  Frame totali ricevuti: {report['total']}")
    if report["macs_seen"]:
        print(f"  MAC sorgente visti nei frame radar3d: {len(report['macs_seen'])}")
        for mac, c in sorted(report["macs_seen"].items(), key=lambda kv: -kv[1])[:10]:
            print(f"    {mac}: {c} frame")

    code, msgs = diagnose(report, args.nodes)
    print()
    for m in msgs:
        print(f"  {m}")
    print()
    return code


if __name__ == "__main__":
    sys.exit(main())
