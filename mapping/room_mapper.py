#!/usr/bin/env python3
"""
room_mapper.py — WiFi fingerprint mapping + position estimation (k-NN).

Tre modalita':
  python3 room_mapper.py calibrate <fingerprint.json>   # calibrazione guidata
  python3 room_mapper.py locate   <fingerprint.json>     # localizzazione da terminale
  python3 room_mapper.py info     <fingerprint.json>     # mostra punti calibrazione

Formato fingerprint JSON:
  {
    "room": {"width": 6.0, "height": 5.0, "name": "Sala"},
    "aps": 3,
    "points": [
      {"x": 1.0, "y": 1.0, "label": "angolo_sx",
       "rssi": [-45.0, -48.0, -52.0], "timestamp": 1234567890.0},
      ...
    ]
  }

Algoritmo localizzazione: weighted k-NN (k=3, peso = 1/distanza).
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from typing import Optional

# ============================================================
# FingerprintMap
# ============================================================

DEFAULT_K = 3
EPSILON = 1e-9


class FingerprintPoint:
    """Un punto calibrato: posizione (x,y) + vettore RSSI dai 3 AP."""

    def __init__(
        self,
        x: float,
        y: float,
        rssi: list[float],
        label: str = "",
        timestamp: float | None = None,
    ):
        self.x = x
        self.y = y
        self.rssi = rssi  # [rssi_ap0, rssi_ap1, rssi_ap2]
        self.label = label
        self.timestamp = timestamp or time.time()

    def to_dict(self) -> dict:
        return {
            "x": self.x,
            "y": self.y,
            "rssi": self.rssi,
            "label": self.label,
            "timestamp": self.timestamp,
        }

    @staticmethod
    def from_dict(d: dict) -> FingerprintPoint:
        return FingerprintPoint(
            x=d["x"], y=d["y"], rssi=d["rssi"],
            label=d.get("label", ""), timestamp=d.get("timestamp"),
        )


class FingerprintMap:
    """Mappa fingerprint: contiene i punti calibrati e i metadati stanza.

    Parametri aggiuntivi (opzionali, per visualizzazione 3D):
        room_height_m : float
            Altezza stanza in metri (per Three.js).
        ap_positions : list[dict]
            Posizioni 3D degli AP: [{"x":.., "y":..(up), "z":..(depth)}, ...]
            Ordine corrisponde all'indice AP nei vettori RSSI.
    """

    def __init__(self, width: float = 6.0, height: float = 5.0,
                 name: str = "Stanza", num_aps: int = 3,
                 room_height_m: float = 3.0):
        self.room = {"width": width, "height": height, "name": name}
        self.room_height_m = room_height_m
        self.num_aps = num_aps
        self.points: list[FingerprintPoint] = []
        self.ap_positions: list[dict] = []  # [{"x":.., "y":..(up), "z":..(depth)}, ...]

    def add_point(self, point: FingerprintPoint):
        if len(point.rssi) != self.num_aps:
            raise ValueError(
                f"RSSI deve avere {self.num_aps} valori (AP), "
                f"ne ha {len(point.rssi)}"
            )
        self.points.append(point)

    def add_point_xy(self, x: float, y: float, rssi: list[float],
                     label: str = "") -> FingerprintPoint:
        pt = FingerprintPoint(x, y, rssi, label)
        self.add_point(pt)
        return pt

    def save(self, path: str):
        """Salva mappa su file JSON."""
        data = {
            "room": self.room,
            "room_height_m": self.room_height_m,
            "aps": self.num_aps,
            "ap_positions": self.ap_positions,
            "points": [p.to_dict() for p in self.points],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"  Mappa salvata: {path} ({len(self.points)} punti)")

    @staticmethod
    def load(path: str) -> FingerprintMap:
        """Carica mappa da file JSON."""
        if not os.path.exists(path):
            raise FileNotFoundError(f"File non trovato: {path}")
        with open(path) as f:
            data = json.load(f)
        fm = FingerprintMap(
            width=data["room"]["width"],
            height=data["room"]["height"],
            name=data["room"].get("name", "Stanza"),
            num_aps=data.get("aps", 3),
            room_height_m=data.get("room_height_m", 3.0),
        )
        fm.ap_positions = data.get("ap_positions", [])
        for pd in data["points"]:
            fm.points.append(FingerprintPoint.from_dict(pd))
        return fm

    @property
    def n_points(self) -> int:
        return len(self.points)

    def get_ap_positions(self) -> list[dict]:
        """Restituisce posizioni AP con default se non configurate."""
        if self.ap_positions:
            return self.ap_positions
        # Default: AP disposti lungo una parete a meta' altezza
        hw = self.room["width"] / 2
        hd = self.room["height"] / 2
        defaults = [
            {"x": hw * 0.3, "y": 2.5, "z": hd - 0.5},
            {"x": hw,       "y": 2.5, "z": hd - 0.5},
            {"x": hw * 1.7, "y": 2.5, "z": hd - 0.5},
        ]
        return defaults[:self.num_aps]

    def info(self) -> str:
        lines = [
            f"  Stanza: {self.room['name']}",
            f"  Dimensioni: {self.room['width']:.1f} x {self.room['height']:.1f} x {self.room_height_m:.1f} m",
            f"  AP: {self.num_aps}",
            f"  Punti calibrazione: {self.n_points}",
        ]
        if self.ap_positions:
            for i, ap in enumerate(self.ap_positions):
                lines.append(
                    f"    AP{i}: ({ap['x']:.1f}, {ap['y']:.1f}, {ap['z']:.1f})")
        for i, p in enumerate(self.points):
            rssi_str = ", ".join(f"{v:+.0f}" for v in p.rssi)
            lines.append(f"    {i+1}. ({p.x:.1f}, {p.y:.1f}) [{p.label}] "
                         f"RSSI=({rssi_str})")
        return "\n".join(lines)


# ============================================================
# PositionEstimator — weighted k-NN
# ============================================================

class PositionEstimator:
    """
    Stima posizione da vettore RSSI via weighted k-NN.

    Parameters
    ----------
    k : int
        Numero nearest neighbor (default 3).
    """

    def __init__(self, k: int = DEFAULT_K):
        self.k = k
        self._fmap: Optional[FingerprintMap] = None

    def load_map(self, fmap: FingerprintMap):
        self._fmap = fmap

    def load(self, path: str):
        self._fmap = FingerprintMap.load(path)

    @property
    def ready(self) -> bool:
        return self._fmap is not None and self._fmap.n_points >= 2

    # --------------------------------------------------------
    # Stima
    # --------------------------------------------------------

    def estimate(self, rssi_vector: list[float]) -> dict:
        """
        Stima posizione da vettore RSSI.

        Parameters
        ----------
        rssi_vector : list
            [rssi_ap0, rssi_ap1, ...] — stesso ordine della calibrazione.

        Returns
        -------
        dict con x, y, confidence, n_neighbors, nearest_label
        """
        if not self.ready:
            return {"x": 0, "y": 0, "confidence": 0.0, "error": "non calibrato"}

        fmap = self._fmap
        assert fmap is not None

        if len(rssi_vector) != fmap.num_aps:
            return {
                "x": 0, "y": 0, "confidence": 0.0,
                "error": f"attesi {fmap.num_aps} RSSI, ricevuti {len(rssi_vector)}",
            }

        # Calcola distanza Euclidea in spazio RSSI per ogni punto
        neighbors = []
        for pt in fmap.points:
            # Normalizza RSSI per dare peso simile a tutti gli AP
            dist = math.sqrt(
                sum((rssi_vector[i] - pt.rssi[i]) ** 2 for i in range(fmap.num_aps))
            )
            neighbors.append((dist, pt))

        # Ordina per distanza
        neighbors.sort(key=lambda x: x[0])
        nearest = neighbors[:self.k]

        # Weighted centroid (peso = 1/distanza)
        total_weight = 0.0
        wx, wy = 0.0, 0.0

        for dist, pt in nearest:
            w = 1.0 / (dist + EPSILON)
            # Bonus per match esatti
            if dist < 1.0:
                w *= 2.0
            wx += pt.x * w
            wy += pt.y * w
            total_weight += w

        if total_weight == 0:
            return {"x": 0, "y": 0, "confidence": 0.0, "error": "peso zero"}

        x_est = wx / total_weight
        y_est = wy / total_weight

        # Confidence: inversamente proporzionale alla distanza del nearest
        best_dist = nearest[0][0] if nearest else 99
        # RSSI tipico range 10-40 dBm di differenza
        # A 0 dBm differenza → confidence 1.0, a 30 dBm → 0.0
        confidence = max(0.0, min(1.0, 1.0 - best_dist / 30.0))

        return {
            "x": round(x_est, 2),
            "y": round(y_est, 2),
            "confidence": round(confidence, 3),
            "n_neighbors": len(nearest),
            "best_distance": round(best_dist, 2),
            "nearest_label": nearest[0][1].label if nearest else "",
        }

    # --------------------------------------------------------
    # Simulazione (test offline)
    # --------------------------------------------------------

    def simulate(self, rssi_vector: list[float]) -> dict:
        """Come estimate() ma stampa a schermo."""
        result = self.estimate(rssi_vector)
        if "error" in result:
            print(f"  ERRORE: {result['error']}")
        else:
            print(f"  Posizione: ({result['x']:.1f}, {result['y']:.1f}) "
                  f"conf={result['confidence']:.2f} "
                  f"k={result['n_neighbors']} "
                  f"best_dist={result['best_distance']:.1f}")
            if result["nearest_label"]:
                print(f"  Piu' vicino: {result['nearest_label']}")
        return result


# ============================================================
# CLI — Calibrazione
# ============================================================

def _parse_rssi(line: str, num_aps: int = 3) -> Optional[list[float]]:
    """Legge riga tipo 'RSSI:-45,-42,-48' da stdin e restituisce lista float."""
    line = line.strip()
    if not line.startswith("RSSI:"):
        return None
    parts = line[5:].split(",")
    if len(parts) != num_aps:
        print(f"  ATTENZIONE: attesi {num_aps} RSSI, trovati {len(parts)}")
        return None
    try:
        return [float(p) for p in parts]
    except ValueError:
        return None


def cmd_calibrate(args: list[str]):
    """
    Calibrazione guidata: cammina in un punto, inserisci coordinate e RSSI.
    """
    path = args[0] if args else "fingerprint.json"

    print("\n=== Calibrazione WiFi Fingerprint ===")
    print("Dimensioni stanza (default 6x5 m): ", end="", flush=True)
    inp = input().strip()
    if inp:
        parts = inp.split("x")
        w = float(parts[0].strip())
        h = float(parts[1].strip()) if len(parts) > 1 else w
    else:
        w, h = 6.0, 5.0

    print(f"Numero AP (default 3): ", end="", flush=True)
    inp = input().strip()
    num_aps = int(inp) if inp else 3

    fmap = FingerprintMap(width=w, height=h, num_aps=num_aps)

    print("\nIstruzioni:")
    print("  1. Posizionati in un punto della stanza")
    print(f"  2. Scrivi: <x> <y> <label>   (es. '1.5 2.5 scrivania')")
    print(f"  3. Incolla RSSI dagli AP come: RSSI:-45,-48,-52")
    print("  4. 'info' per vedere i punti raccolti")
    print("  5. 'save' per salvare e uscire\n")

    while True:
        print(f"\n  [{fmap.n_points + 1}] Punto (x y label): ", end="", flush=True)
        inp = input().strip()
        if not inp:
            continue

        cmd = inp.lower().split()
        if cmd[0] == "save":
            break
        if cmd[0] == "info":
            print(fmap.info())
            continue
        if cmd[0] == "exit" or cmd[0] == "quit":
            print("  Uscita senza salvare.")
            return

        try:
            x = float(cmd[0])
            y = float(cmd[1])
            label = " ".join(cmd[2:]) if len(cmd) > 2 else f"punto_{fmap.n_points + 1}"
        except (ValueError, IndexError):
            print("  Formato: x y [label]  (es. '1.5 2.5 scrivania')")
            continue

        print(f"  Incolla RSSI (RSSI:val1,val2,...): ", end="", flush=True)
        rssi_line = input().strip()
        rssi = _parse_rssi(rssi_line, num_aps)
        if rssi is None:
            print(f"  Formato RSSI: 'RSSI:-45,-48,-52' ({num_aps} valori)")
            continue

        fmap.add_point_xy(x, y, rssi, label)
        print(f"  [+] {label} ({x:.1f}, {y:.1f}) RSSI=({','.join(f'{v:+.0f}' for v in rssi)}")

    fmap.save(path)
    print(f"\n  Fatto! Usa:  python3 room_mapper.py locate {path}")


# ============================================================
# CLI — Configurazione posizioni AP (per visualizzazione 3D)
# ============================================================

def cmd_setup_aps(args: list[str]):
    """Configura le posizioni 3D degli AP nella stanza."""
    if not args:
        print("  Uso: python3 room_mapper.py setup-aps <fingerprint.json>")
        sys.exit(1)

    path = args[0]
    fmap = FingerprintMap.load(path)

    print(f"\n=== Configurazione posizioni AP ===")
    print(f"  Stanza: {fmap.room['name']} "
          f"({fmap.room['width']}x{fmap.room['height']}x{fmap.room_height_m}m)")
    print(f"  AP da configurare: {fmap.num_aps}")
    print(f"\n  Coordinate: x=orizzontale, y=verticale(su), z=profondita'")
    print(f"  (tutte in metri, esempio: '1.5 2.5 0.5')")
    print(f"  'info' per vedere AP configurati, 'save' per salvare\n")

    positions = list(fmap.ap_positions) if fmap.ap_positions else []

    while len(positions) < fmap.num_aps:
        default = _default_ap_pos(fmap, len(positions))
        print(f"  AP{len(positions)} (x y z) "
              f"[default: {default['x']:.1f} {default['y']:.1f} {default['z']:.1f}]: ",
              end="", flush=True)
        inp = input().strip()
        if inp.lower() in ("save", "exit", "quit"):
            break
        if inp.lower() == "info":
            _print_ap_info(positions)
            continue

        if inp:
            try:
                parts = [float(v) for v in inp.split()]
                if len(parts) == 3:
                    positions.append({"x": parts[0], "y": parts[1], "z": parts[2]})
                    continue
            except ValueError:
                pass
            print("  Formato: x y z  (es. '1.5 2.5 0.5')")
            continue

        positions.append(default)

    if positions:
        fmap.ap_positions = positions
        fmap.save(path)
        print(f"  Posizioni AP salvate ({len(positions)} AP)")
    else:
        print("  Nessuna posizione configurata.")


def _default_ap_pos(fmap, idx: int) -> dict:
    """Posizione AP di default lungo la parete frontale."""
    w = fmap.room["width"]
    d = fmap.room["height"]
    spacing = w / (fmap.num_aps + 1)
    return {"x": spacing * (idx + 1), "y": 2.5, "z": 0.0}


def _print_ap_info(positions: list[dict]):
    if not positions:
        print("  Nessun AP configurato.")
    for i, ap in enumerate(positions):
        print(f"    AP{i}: ({ap['x']:.1f}, {ap['y']:.1f}, {ap['z']:.1f})")


# ============================================================
# CLI — Localizzazione da terminale
# ============================================================

def cmd_locate(args: list[str]):
    """Localizzazione interattiva da terminale."""
    if not args:
        print("  Uso: python3 room_mapper.py locate <fingerprint.json>")
        sys.exit(1)

    path = args[0]
    estimator = PositionEstimator()
    try:
        estimator.load(path)
    except FileNotFoundError:
        print(f"  ERRORE: {path} non trovato. Fai prima 'calibrate'.")
        sys.exit(1)

    fmap = estimator._fmap
    assert fmap is not None
    print(f"\n=== Localizzazione: {fmap.room['name']} ===")
    print(f"  {fmap.n_points} punti calibrazione, {fmap.num_aps} AP\n")
    print("  Incolla RSSI (RSSI:val1,val2,...) o 'quit':")

    while True:
        print("  > ", end="", flush=True)
        inp = input().strip()
        if not inp or inp.lower() in ("quit", "exit", "q"):
            break

        rssi = _parse_rssi(inp, fmap.num_aps)
        if rssi is None:
            print(f"  Formato: 'RSSI:-45,-48,-52' ({fmap.num_aps} valori)")
            continue

        estimator.simulate(rssi)


# ============================================================
# CLI — Info
# ============================================================

def cmd_info(args: list[str]):
    """Mostra info fingerprint."""
    if not args:
        print("  Uso: python3 room_mapper.py info <fingerprint.json>")
        sys.exit(1)
    fmap = FingerprintMap.load(args[0])
    print(fmap.info())


# ============================================================
# Main
# ============================================================

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "calibrate":
        cmd_calibrate(args)
    elif cmd == "locate":
        cmd_locate(args)
    elif cmd == "info":
        cmd_info(args)
    elif cmd == "setup-aps":
        cmd_setup_aps(args)
    else:
        print(f"Comando sconosciuto: {cmd}")
        print("Usa: calibrate | locate | info | setup-aps")
        sys.exit(1)


if __name__ == "__main__":
    main()
