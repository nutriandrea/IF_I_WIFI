# Room 3D — visualizzazione live (RuView-style)

Render Three.js della stanza con:
- 3 marker ricevitori ESP32 (cubi verdi)
- 3-4 marker pinger (sfere blu)
- Heatmap del pavimento (probabilita' per cella della griglia)
- Marker persona (sfera rossa) sulla cella piu' probabile
- Trail di movimento (opzionale)
- HUD con classe, confidence, MAC sorgente, FPS, stato WebSocket
- OrbitControls (drag rotate, scroll zoom)
- **Modalita' demo automatica**: se il WebSocket non si connette entro 5s, parte una simulazione random — utile per testare il rendering senza l'hardware

## Architettura

```
3 ESP32 ──UDP:5005──→ csi_mac.py ──WS:8765──→ room_3d.html
(ADR-018 binary)      (ML inference)            (Three.js render)
```

Tre ESP32 mandano frame CSI binari su UDP. `csi_mac.py` li demuxa per `node_id`, estrae feature multi-RX, predice la cella della griglia con `MultiAPCSIClassifier`, e broadcastsa la predizione + probas via WebSocket. Il browser le applica al pavimento come heatmap colorata.

## Avvio rapido

### 1. Flash dei 3 ESP32

Per ognuno dei 3 ESP32, modifica `firmware/esp32_csi_firmware/esp32_csi_firmware.ino`:

```cpp
#define NODE_ID 0    // ESP32 #1 -> 0,  ESP32 #2 -> 1,  ESP32 #3 -> 2
#define UDP_AUTO_START true
#define UDP_TARGET_HOST "10.12.124.109"   // IP del PC che gira csi_mac.py
```

Carica e ripeti per ogni board. (Ogni board deve avere `NODE_ID` diverso.)

### 2. Training del modello posizioni

Una persona si sposta tra le celle della griglia. Con la stanza ~6m x 5m e griglia 4×5:

```bash
python3 -m csi.csi_mac --positions --grid 4x5 --udp-port 5005 --use-ml
```

Lo script ti guida: "Stai fuori (30s vuoto), siediti in r0c0 (30s), in r0c1 (30s), ...". Salva `csi/csi_positions_model.joblib` e `csi_positions_labels.json`.

### 3. Live demo

Terminale 1 — server CSI con WebSocket:
```bash
python3 -m csi.csi_mac --monitor --use-ml \
  --udp-port 5005 --ws-port 8765
```

Terminale 2 — HTTP server per il browser:
```bash
python3 -m http.server -d mapping 8000
```

Browser:
```
http://localhost:8000/room_3d.html?room=6x5x3&grid=4x5&rx=0.5,0.5;5.5,0.5;3,4.5&tx=1,1;5,1;3,4
```

## URL params

| Param | Descrizione | Default | Esempio |
|---|---|---|---|
| `ws` | URL del WebSocket | `ws://<host>:8765` | `ws=ws://10.12.124.109:8765` |
| `room` | Dimensioni stanza in metri (W×L×H) | `6x5x3` | `room=8x6x3.2` |
| `grid` | Righe × colonne della griglia | `4x5` | `grid=3x4` |
| `rx` | Posizioni ricevitori, `x,y` per RX, separati da `;` | 3 default | `rx=0.5,0.5;5.5,0.5;3,4.5` |
| `tx` | Posizioni pinger, `x,y` per TX | 3 default | `tx=1,1;5,1;3,4;0,3` |
| `trail` | Mostra il trail di movimento (1 = on) | off | `trail=1` |

**Coordinate**: `x` lungo la larghezza, `y` lungo la lunghezza (0,0) = angolo SW. Le posizioni RX/TX sono in metri sul pavimento (z fissato a 0.5m per i RX e 1m per i TX nel rendering — modifiche al codice se ti serve altezza diversa).

## Protocollo WebSocket

Il backend deve mandare messaggi con questa struttura:

```json
{
  "type": "position",
  "t": 12.345,
  "class": "r1c2",
  "probas": {"r0c0": 0.05, "r0c1": 0.08, "r1c1": 0.42, "r1c2": 0.65, "EMPTY": 0.02},
  "rssi": -45,
  "mac": "aabbccddeeff",
  "source_id": "rx0"
}
```

I campi `class` e `probas` con label `rRcC` sono obbligatori. Il viz **interpola la heatmap su tutte le celle** in base alle probas — non solo sulla cella vincente, quindi anche `r1c1` (la seconda piu' probabile) appare colorata.

Compatibilita': accetta anche il tipo legacy `seat_prediction` con campi `prediction`/`probabilities`/`confidence`.

## Modalita' demo (senza ESP32)

Se apri `room_3d.html` senza che il WebSocket sia attivo, dopo 5 secondi parte una simulazione che muove un punto random per la stanza. **Utile per testare layout/posizioni RX/TX prima di lanciare l'hardware.**

## Troubleshooting

- **Pagina bianca**: aprila SEMPRE via HTTP (`python3 -m http.server`), non direttamente da `file://`. Il browser blocca i moduli ES da file://.
- **Heatmap sempre uniforme**: il modello ML non e' caricato. Verifica che `csi_positions_model.joblib` esista in `csi/`.
- **Nessun messaggio WS**: controlla che `csi_mac.py` sia stato lanciato con `--ws-port 8765` e che `websockets` sia installato (`pip install websockets`).
- **Frame UDP 0**: gli ESP32 non sanno l'IP del PC. Verifica `UDP_TARGET_HOST` nel firmware e che PC + ESP32 siano sulla stessa rete.
- **Heatmap "scaglionata"**: la griglia tessella la stanza interamente. Se vuoi un'area limitata, aggiusta `room` e `grid` insieme.
