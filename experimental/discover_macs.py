#!/usr/bin/env python3
"""
discover_macs.py — Helper per leggere il MAC dell'ESP32 collegato via seriale
e aggiornare automaticamente firmware/esp32_radar3d/network_config.h.

Workflow:
    1. Collega UN ESP32 alla volta via USB.
    2. Lancia:
        python3 tools/discover_macs.py --port /dev/cu.usbserial-XXX
    3. Lo script:
        - apre il seriale a 115200
        - manda un reset hardware (DTR toggle)
        - cattura le righe finché trova "ESP32 MAC Address: AA:BB:CC:..."
        - ti chiede in quale posizione (NODE 0/1/2) salvarlo
        - aggiorna network_config.h conservando i MAC degli altri nodi
    4. Ripeti per ognuno dei 3 ESP32 (ciascuno NODE diverso).
    5. Flasha i 3 firmware con NODE_ID corrispondente.

Esempi:
    # Auto: prova /dev/cu.usbserial-* o /dev/ttyUSB*
    python3 tools/discover_macs.py

    # Specifica porta esplicita
    python3 tools/discover_macs.py --port /dev/cu.usbserial-1410

    # Dry-run (mostra cosa scriverebbe ma non modifica il file)
    python3 tools/discover_macs.py --dry-run

Dipendenze: pyserial (`pip install pyserial`).
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import time

_HAVE_SERIAL = False
try:
    import serial  # type: ignore
    _HAVE_SERIAL = True
except ImportError:
    pass


CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "firmware", "esp32_radar3d", "network_config.h",
)

MAC_REGEX = re.compile(
    r"(?:ESP32\s+)?MAC(?:\s+Address)?:\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})",
    re.IGNORECASE,
)


# ============================================================
# Serial port discovery
# ============================================================
def autodetect_port() -> str | None:
    """Cerca porte ESP32 tipiche su Mac/Linux."""
    candidates = (
        glob.glob("/dev/cu.usbserial-*") +
        glob.glob("/dev/cu.SLAB_USBtoUART*") +
        glob.glob("/dev/cu.wchusbserial*") +
        glob.glob("/dev/ttyUSB*") +
        glob.glob("/dev/ttyACM*")
    )
    if not candidates:
        return None
    if len(candidates) > 1:
        print(f"  ATTENZIONE: trovate {len(candidates)} porte: {candidates}",
              file=sys.stderr)
        print(f"  Usando la prima: {candidates[0]}", file=sys.stderr)
    return candidates[0]


# ============================================================
# Reset + MAC capture
# ============================================================
def capture_mac(port: str, baud: int = 115200, timeout: float = 12.0) -> str | None:
    """Apre il seriale, fa un reset hardware, e cerca il MAC nei log di boot."""
    if not _HAVE_SERIAL:
        print("ERROR: pyserial non installato. pip install pyserial",
              file=sys.stderr)
        return None

    print(f"  Apertura {port} @ {baud}...", file=sys.stderr)
    try:
        ser = serial.Serial(port, baud, timeout=0.3)
    except Exception as e:
        print(f"  ERROR: impossibile aprire {port}: {e}", file=sys.stderr)
        return None

    try:
        # Reset hardware via DTR toggle (funziona su ESP32 dev boards)
        ser.dtr = False; ser.rts = True; time.sleep(0.1)
        ser.dtr = True; ser.rts = False; time.sleep(0.05)
        ser.dtr = False; time.sleep(0.1)
        ser.reset_input_buffer()

        print(f"  Reset inviato. Lettura log boot per max {timeout}s...",
              file=sys.stderr)
        t0 = time.time()
        buf = ""
        while time.time() - t0 < timeout:
            try:
                chunk = ser.read(256).decode("utf-8", errors="replace")
            except Exception:
                continue
            if chunk:
                buf += chunk
                # log realtime
                sys.stderr.write(chunk)
                sys.stderr.flush()
                m = MAC_REGEX.search(buf)
                if m:
                    return m.group(1).upper()
        return None
    finally:
        ser.close()


# ============================================================
# network_config.h patching
# ============================================================
def read_current_macs(config_path: str) -> list[str | None]:
    """Estrae i 3 MAC dal file (None per slot non popolato/parsable)."""
    if not os.path.exists(config_path):
        return [None, None, None]
    with open(config_path) as f:
        text = f.read()
    # Cerca pattern: { 0xXX, 0xXX, 0xXX, 0xXX, 0xXX, 0xXX }
    rx = re.compile(r"\{\s*0x([0-9A-Fa-f]{2})\s*,\s*"
                    r"0x([0-9A-Fa-f]{2})\s*,\s*"
                    r"0x([0-9A-Fa-f]{2})\s*,\s*"
                    r"0x([0-9A-Fa-f]{2})\s*,\s*"
                    r"0x([0-9A-Fa-f]{2})\s*,\s*"
                    r"0x([0-9A-Fa-f]{2})\s*\}")
    matches = rx.findall(text)
    out: list[str | None] = []
    for m in matches[:3]:
        out.append(":".join(m).upper())
    while len(out) < 3:
        out.append(None)
    return out


def write_updated_macs(config_path: str, macs: list[str | None],
                       dry_run: bool = False) -> None:
    """Rigenera la sezione NODE_MACS con i nuovi MAC."""
    if not os.path.exists(config_path):
        print(f"  ERROR: {config_path} non esiste. Crealo dal template.",
              file=sys.stderr)
        return

    with open(config_path) as f:
        text = f.read()

    # Costruisci nuovo blocco
    lines = ["static const uint8_t NODE_MACS[3][6] = {"]
    for i, mac in enumerate(macs):
        if mac is None:
            lines.append(f"  {{ 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 }},  // NODE {i} (DA SCOPRIRE)")
        else:
            bytes_ = mac.split(":")
            byte_list = ", ".join(f"0x{b.upper()}" for b in bytes_)
            lines.append(f"  {{ {byte_list} }},  // NODE {i}")
    lines.append("};")
    new_block = "\n".join(lines)

    # Sostituisci il blocco esistente
    pattern = re.compile(
        r"static\s+const\s+uint8_t\s+NODE_MACS\s*\[\s*3\s*\]\s*\[\s*6\s*\]\s*=\s*\{[^}]*?\};",
        re.DOTALL,
    )
    if pattern.search(text):
        new_text = pattern.sub(new_block, text, count=1)
    else:
        print(f"  ERROR: blocco NODE_MACS non trovato in {config_path}",
              file=sys.stderr)
        return

    if dry_run:
        print("  --- DRY RUN: nuovo blocco NODE_MACS ---", file=sys.stderr)
        print(new_block)
        return

    with open(config_path, "w") as f:
        f.write(new_text)
    print(f"  ✓ {config_path} aggiornato.", file=sys.stderr)


# ============================================================
# Main
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Scopre MAC ESP32 collegato via seriale e aggiorna network_config.h",
    )
    ap.add_argument("--port", help="Porta seriale (es. /dev/cu.usbserial-1410). Auto se omesso.")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--timeout", type=float, default=12.0,
                    help="Secondi di lettura post-reset")
    ap.add_argument("--node", type=int, choices=[0, 1, 2],
                    help="In quale NODE_MACS[N] salvare. Se omesso, chiede interattivo.")
    ap.add_argument("--config", default=CONFIG_PATH,
                    help=f"Path al network_config.h (default: {CONFIG_PATH})")
    ap.add_argument("--dry-run", action="store_true",
                    help="Non scrive il file, mostra solo il diff")
    args = ap.parse_args()

    if not _HAVE_SERIAL:
        print("ERROR: pyserial non installato. pip install pyserial",
              file=sys.stderr)
        return 1

    port = args.port or autodetect_port()
    if port is None:
        print("ERROR: nessuna porta seriale trovata. Specifica --port.",
              file=sys.stderr)
        return 1

    print("\n  Lo script farà un reset dell'ESP32 collegato e cercherà il MAC.",
          file=sys.stderr)
    print("  Assicurati che il firmware esp32_radar3d sia già flashato.\n",
          file=sys.stderr)

    mac = capture_mac(port, args.baud, args.timeout)
    if mac is None:
        print(f"\n  ERROR: MAC non trovato in {args.timeout}s. Verifica:",
              file=sys.stderr)
        print(f"    - firmware esp32_radar3d è flashato sull'ESP32?", file=sys.stderr)
        print(f"    - baud 115200 corretto?", file=sys.stderr)
        print(f"    - lo Serial Monitor in Arduino IDE è chiuso?", file=sys.stderr)
        return 1

    print(f"\n  ✓ MAC trovato: {mac}", file=sys.stderr)

    # Determina il node target
    if args.node is not None:
        node = args.node
    else:
        current = read_current_macs(args.config)
        print(f"\n  Stato attuale di {args.config}:", file=sys.stderr)
        for i, m in enumerate(current):
            mark = "←" if m == mac else ""
            print(f"    NODE {i}: {m or '(vuoto)'} {mark}", file=sys.stderr)
        while True:
            try:
                ans = input(f"\n  In quale NODE salvare {mac}? [0/1/2/q]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr); return 1
            if ans.lower() == "q":
                return 0
            if ans in ("0", "1", "2"):
                node = int(ans); break
            print("  Risposta non valida.", file=sys.stderr)

    current = read_current_macs(args.config)
    current[node] = mac
    write_updated_macs(args.config, current, dry_run=args.dry_run)

    print(f"\n  Riassunto dopo aggiornamento:", file=sys.stderr)
    for i, m in enumerate(current):
        print(f"    NODE {i}: {m or '(vuoto)'}", file=sys.stderr)
    print("\n  Ricorda: setta NODE_ID corrispondente nel firmware prima del flash.\n",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
