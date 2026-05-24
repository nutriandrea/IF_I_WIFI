# Arduino Wi-Fi Sensing

> **Human presence, position, vitals (breathing + heart rate), and sleep analysis from everyday Wi-Fi.**
> ESP32 captures Channel State Information (CSI) → host processes it → browser shows heatmap, radar 3D, and vitals.

No cameras. No wearables. Just the Wi-Fi routers already in the room.

---

## What it can do

| Capability | How | Hardware |
|---|---|---|
| **Cross-ping multi-RX** | 3 ESP32 ping each other at 100 Hz → 9 stable CSI (tx, rx) channels, fixed MACs | 3 ESP32 + host |
| **Presence (EMPTY/STILL/MOTION)** | Variance-based state machine, no ML. Configurable hysteresis + dwell | 1-3 ESP32 + host |
| **Position (grid classifier)** | RandomForest classifier on CSI stats → which grid cell is occupied | 3 ESP32 + host |
| **Continuous (x,y) tracking** | RF multi-output regressor + Kalman 2D → blob coordinates in meters | 3 ESP32 + host |
| **Motion vs static** | Velocity hysteresis on Kalman state | derived from position |
| **Browser radar 3D** | Three.js sonar scene, continuous blob, sweep rings, motion indicator | ESP32 + host + browser |
| **Browser heatmap grid** | Real-time probability grid via WebSocket | ESP32 + host + browser |
| **Breathing rate (BPM)** | Phase CSI → IIR bandpass 0.1–0.5 Hz → zero-crossing BPM (RuView port) | 3 ESP32 + host |
| **Heart rate (BPM)** | Phase CSI → IIR bandpass 0.8–2.0 Hz → autocorrelation peak HR (RuView port) | 3 ESP32 + host |
| **Sleep stage analysis** | Breathing regularity heuristic → awake/light/deep + apnea detection | 3 ESP32 + host |
| **Doppler profile** | FFT-based Doppler shift from phase difference → spectral band power | 3 ESP32 + host |
| **RSSI features** | CUSUM, FFT band powers, temporal stats, skew/kurtosis from RSSI stream | 1-3 ESP32 + host |
| **Signal processing** | Spectrogram, BVP, Fresnel zones, biquad IIR, Hampel filter, Welford stats, CSI ratio | host (library) |
| **Home Assistant bridge** | MQTT discovery + state publishing (6 sensors + 3 binary sensors per node) | host + MQTT broker |
| **Remote sensing client** | WebSocket client for remote RuView-compatible sensing server | host (network) |
| **Presence/motion (basic)** | RSSI-based detector (works without ESP32) | UNO Q only |
| **Record & replay** | Save CSI to file, replay for offline development | ESP32 + host |

---

## Quick start — demo completa

### 1. Flash ESP32 — cross-ping firmware (architettura corrente)

Sketch: **`firmware/esp32_radar3d/esp32_radar3d.ino`** (guida dettagliata: [`firmware/esp32_radar3d/FLASHING.md`](firmware/esp32_radar3d/FLASHING.md)).

I 3 ESP32 si **pingano fra loro** a 100 Hz su canale 6: ogni nodo trasmette broadcast 802.11 e contemporaneamente cattura CSI sui frame ricevuti dagli altri due. Risultato: 9 coppie `(tx_node, rx_node)` con MAC **stabili** — niente piu' problemi di MAC randomization tipici dei pinger telefono.

Per ogni board:

1. Copia `firmware/esp32_radar3d/secrets.h.example` in `secrets.h` e mettici SSID/password del tuo WiFi 2.4 GHz
2. Modifica `firmware/esp32_radar3d/network_config.h`:
   - `#define NODE_ID 0` (oppure 1 o 2 per le altre due schede)
   - `NODE_MACS[3][6]`: MAC delle 3 schede (vedi sezione "Scoprire i MAC" in FLASHING.md)
   - `#define UDP_TARGET_IP "..."`: IP del PC che girera' la pipeline
3. Arduino IDE → Tools > Board: **ESP32 Dev Module**, Upload Speed: **115200**
4. Carica lo sketch
5. Cambia `NODE_ID`, ricarica sulla scheda successiva. Ripeti per tutte e 3.

**Legacy** (`firmware/esp32_csi_firmware/esp32_csi_firmware.ino`): firmware vecchio single-source con pinger esterni. Funziona ancora (UDP magic `0xC5110001`, pipeline retrocompatibile), ma soffre del problema MAC randomization sui telefoni.

### 2. Addestra un modello posizioni

Con l'ESP32 connesso e una persona che si sposta tra le posizioni:

```bash
# Griglia 3×3 (9 posizioni + vuoto)
python3 -m csi.csi_mac --positions --grid 3x3 --use-ml
```

Il modello viene salvato in `csi_positions_model.joblib`.

### 3. Demo live

```bash
# ESP32 via UDP (AP mode)
python3 -m csi.csi_mac \
  --monitor --use-ml \
  --udp-port 5005 \
  --ws-port 8765 \
  --vitals
```

Apri `mapping/classroom_heatmap.html?ws=ws://localhost:8765` nel browser.

Vedrai:
- La griglia delle posizioni con probabilità in tempo reale
- BPM (respirazione) nella sidebar quando il modello è pronto

---

## Continuous blob tracking (radar 3D)

Modalità alternativa che non discretizza in celle: **regressore continuo** che predice `(x, y)` in metri + `Kalman 2D` per smoothing + classificatore fermo/movimento basato sulla velocità stimata. Output renderizzato in stile **sonar/radar verde** con Three.js.

Quando usare questa modalità rispetto al grid classifier:

| Aspetto | Grid classifier | Blob regressor |
|---|---|---|
| Output | Cella vincente (`r1c2`) + probabilità | Coordinate continue `(x=1.42, y=3.75)` |
| Risoluzione | Celle (1-2m tipico) | Sub-metrica (interpolata) |
| Calibrazione | 1 punto per cella (8+ celle) | 5-9 punti totali |
| Smoothing | Nessuno → "stuck" e flash random | Kalman 2D → fluido |
| Velocità | No | Si, vettore (vx, vy) |
| Fermo/movimento | No | Si, con hysteresis |
| Visualizzazione | `room_3d.html` (heatmap griglia) | `radar_3d.html` (sonar) |

### Training del blob (3 minuti, 1 persona alla volta)

```bash
# Punti default per stanza 2.5×6m (modifica con --points "x1,y1;x2,y2;...")
python3 -m csi.blob_cli --train \
    --udp-port 5005 \
    --seconds 30 \
    --points "0.5,0.5;2.0,0.5;1.25,3.0;0.5,5.5;2.0,5.5"
```

Lo script ti chiede una posizione alla volta, premi INVIO, raccoglie 30s, passa alla successiva. Salva `csi/csi_blob_model.joblib` + `csi/csi_blob_meta.json`.

### Monitor live + visualizzazione

```bash
# Terminale A: blob inference + WebSocket
python3 -m csi.blob_cli --monitor --udp-port 5005 --ws-port 8765

# Terminale B: HTTP per il browser
python3 -m http.server -d mapping 8000
```

Browser (HTTP non HTTPS):
```
http://localhost:8000/radar_3d.html?room=2.5x6x2.7&rx=0.2,0.5;2.3,3.0;0.2,5.5&tx=2.3,0.5;0.2,3.0;2.3,5.5
```

URL params: `room=<W>x<L>x<H>` (metri), `rx=` posizioni 3 ESP32 (`x,y;x,y;...`), `tx=` posizioni 3-4 pinger, `ws=` URL WebSocket.

### Cosa vedi nella UI radar

- Stanza wireframe verde su sfondo nero
- 3 marker RX (cubi verdi) e 3-4 TX (ottaedri ciano) nelle posizioni configurate
- **Blob** rosso (in movimento) o verde (fermo) che si muove fluido nello spazio
- **Halo** dimensionato in base alla confidence
- **Trail** delle ultime 80 posizioni
- **Vettore velocità** (freccia arancione)
- **Sweep rings** sonar che si espandono dal blob
- **Indicatore FERMO/MOVIMENTO** grande in alto al centro (rosso pulsante in movimento)
- **HUD** con coordinate, velocità, confidence, FPS

### Demo mode (senza hardware)

Apri `radar_3d.html` senza WebSocket attivo: dopo 5s parte una simulazione (blob che gira in cerchio nella stanza alternando movimento/fermo). Utile per verificare le posizioni dei marker RX/TX prima di accendere l'hardware.

### Protocollo WebSocket (type: "blob")

```json
{
  "type": "blob",
  "t": 12.345,
  "x_raw": 2.31, "y_raw": 3.42,
  "x": 2.30, "y": 3.40,
  "vx": 0.05, "vy": -0.02,
  "speed": 0.054,
  "motion": false,
  "confidence": 0.87
}
```

### Diagnostica

```bash
python3 -m csi.diagnose_model
```

Lo script ascolta 10s di UDP e stampa:
- **Tipo di firmware rilevato**: radar3d cross-ping (`0xC5110003`) vs ADR-018 legacy (`0xC5110001`)
- **Frame per ricevitore** (`NODE_ID 0/1/2`) — i 3 RX devono avere rate simili
- **Frame per sorgente**: con radar3d sono le 9 coppie `rx{n}-tx{m}` attese; con legacy sono MAC dei pinger
- **Verdetto**: compatibilità del modello salvato con il setup attuale (sorgenti riconosciute / mancanti / sconosciute)

Con il firmware cross-ping `esp32_radar3d`, le sorgenti sono **stabili by design** (NODE_ID hardware, non MAC casualizzati): il `diagnose_model` ti dice se uno dei 3 nodi non sta trasmettendo o se il `NODE_MACS` in `network_config.h` non combacia coi MAC reali.

---

## Architecture

### Cross-ping (firmware esp32_radar3d, default)

```
   ┌──────────┐      ┌──────────┐      ┌──────────┐
   │ ESP32 #0 │ ←──→ │ ESP32 #1 │ ←──→ │ ESP32 #2 │   3 nodi che si pingano
   └────┬─────┘      └────┬─────┘      └────┬─────┘   broadcast 802.11 @ 100Hz
        │ UDP             │ UDP             │ UDP    canale 6 fisso, 9 canali (tx,rx)
        └────────────────┬┴─────────────────┘
                         ▼
                  ┌─────────────────┐     WebSocket    ┌──────────┐
                  │  PC centrale     │ ───────────────→│ Browser  │
                  │  csi_mac.py /    │    JSON         │ heatmap  │
                  │  blob_cli.py     │    position/    │ + radar  │
                  │  + ML model      │    blob         │   3D     │
                  └─────────────────┘                  └──────────┘
```

- Magic UDP: `0xC5110003`
- Sorgenti: 9 coppie stabili `rx0-tx0 .. rx2-tx2`
- Risolve definitivamente il problema MAC randomization

### Legacy single-source (firmware esp32_csi_firmware)

```
┌──────────────┐     UDP/Serial     ┌──────────────┐     WebSocket     ┌──────────┐
│  ESP32       │ ──────────────────→│  UNO Q / PC   │ ────────────────→│ Browser  │
│  (CSI source) │    CSI frames      │  (processing) │    position +    │ (heatmap │
│  AP or STA   │                    │  csi_mac.py   │    vitals JSON   │ + vitals)│
└──────────────┘                    └──────────────┘                  └──────────┘
                                            │
                                     ┌──────┴──────┐
                                     │  ML model    │
                                     │  (joblib)    │
                                     └──────────────┘
```

- Magic UDP: `0xC5110001`
- Sorgenti: MAC dei pinger esterni (telefoni/laptop). Soggetto a MAC randomization.

### Data flow

1. **ESP32** cattura frame Wi-Fi e estrae CSI (I/Q per 64–128 subcarrier)
2. **Firmware** serializza in formato testo (`CSI:<seq>:<rssi>:<ampl>...`) o binario ADR-018
3. **csi_mac.py** riceve via UDP o seriale, estrae feature, predice posizione + BPM
4. **WebSocket server** (opzionale, `--ws-port`) trasmette JSON a browser
5. **classroom_heatmap.html** mostra griglia colorata + BPM

---

## CLI reference

### Comandi principali

```bash
# --- Grid classifier (cells discrete) ---
python3 -m csi.csi_mac --monitor                            # Detection in tempo reale
python3 -m csi.csi_mac --monitor --use-ml                    # Con ML classifier
python3 -m csi.csi_mac --capture --seconds 60                # Registra CSI su file
python3 -m csi.csi_mac --calibrate                           # Calibrazione baseline
python3 -m csi.csi_mac --positions --grid 4x5 --use-ml       # Addestramento posizioni grid

# --- Blob regressor (coordinate continue + motion) ---
python3 -m csi.blob_cli --train --points "0.5,0.5;2,0.5;1.25,3;0.5,5.5;2,5.5"
python3 -m csi.blob_cli --monitor --udp-port 5005 --ws-port 8765

# --- Diagnostica ---
python3 -m csi.diagnose_model                                # Verifica modello vs setup
```

### Flag demo

| Flag | Effetto |
|---|---|
| `--ws-port 8765` | Broadcast posizioni + BPM via WebSocket |
| `--vitals` | Estrae respirazione dalla fase CSI |
| `--udp-port 5005` | Riceve frame UDP dall'ESP32 (evita crash USB su S3) |
| `--ap-mode` | ESP32 in AP mode (3 PC si connettono direttamente) |
| `--heatmap` | Mostra heatmap matplotlib in tempo reale |
| `--grid 3x3` | Griglia di posizioni (righe × colonne) |

### Esempi

```bash
# Solo seriale
python3 -m csi.csi_mac --monitor

# UDP + ML + WebSocket + vitals
python3 -m csi.csi_mac --monitor --use-ml --udp-port 5005 --ws-port 8765 --vitals

# Multi-AP (3 PC come sorgenti)
python3 -m csi.csi_mac --monitor --use-ml --num-aps 3
```

---

## Vitals — breathing rate da Wi-Fi

Il `PhaseBreathingEstimator` (in `csi_ml.py`) stima il BPM dalla fase CSI:

1. **Unwrap** fase per ogni subcarrier (rimuove salti 2π)
2. **Butterworth bandpass** 0.1–0.5 Hz (passa solo la banda respiratoria)
3. **Zero-crossing** sul segnale filtrato → BPM
4. **Selezione** del subcarrier con miglior rapporto segnale-rumore
5. **EMA smoothing** (α = 0.3) per stabilità

Attivazione: `--vitals` (richiede `--use-ml`).

Output via WebSocket:
```json
{"type": "vitals", "t": 12.5, "bpm": 16.2, "confidence": 0.87, "n_subcarriers": 52}
```

---

## File reference

### `csi/` — Core processing

| File | Descrizione |
|---|---|
| `csi_mac.py` | CLI principale grid-mode: monitor, capture, calibrate, positions |
| `csi_ml.py` | ML classifier grid-based (Random Forest), PhaseBreathingEstimator, RSSIFeatureExtractor |
| `csi_processor.py` | Parser CSI (testo e binario ADR-018), CSIDetector |
| `csi_record.py` | Record su file e replay (utile per test offline) |
| `csi_plot.py` | Plot live ampiezza/fase CSI (waterfall, time, bar) |
| `phase_sanitizer.py` | Unwrap fase, rimozione outlier, smoothing |
| `blob_regressor.py` | **NEW** RandomForestRegressor + Kalman 2D per coordinate continue (x,y) + motion classifier |
| `blob_cli.py` | **NEW** CLI per blob mode: `--train` (calibrazione punti) e `--monitor` (live + WebSocket) |
| `diagnose_model.py` | **NEW** Diagnostica modello: confronta MAC training vs runtime, frame per receiver, verdetto |

### `mapping/` — Browser frontend

| File | Descrizione |
|---|---|
| `classroom_heatmap.html` | Frontend 2D: heatmap griglia + BPM via WebSocket (grid classifier) |
| `room_3d.html` | **NEW** Frontend 3D Three.js: stanza + heatmap probabilistica griglia |
| `radar_3d.html` | **NEW** Frontend 3D stile sonar verde: blob continuo + sweep rings + indicatore FERMO/MOVIMENTO (blob regressor) |
| `README.md` | Documentazione URL params e protocollo WebSocket per i frontend 3D |

### `rssi/` — RSSI presence (senza ESP32)

Pipeline alternativa che funziona su UNO Q standalone (solo RSSI da `/proc/net/wireless`), senza bisogno di ESP32.

| File | Descrizione |
|---|---|
| `enhanced_presence.py` | Detector principale: quick / baseline / movement / analyze / monitor |
| `decision_engine.py` | Orchestratore RSSI → score → azioni |
| `calibrate_presence.py` | Calibrazione legacy |
| `monitor_presence.py` | Monitor mode capture |
| `rssi_ml.py` | ML classifier su RSSI |
| `bridge_client.py` | CLI wrapper arduino-router RPC |

### `firmware/` — ESP32 firmware

| File | Descrizione |
|---|---|
| `esp32_radar3d/esp32_radar3d.ino` | **DEFAULT** Firmware cross-ping multi-RX: 3 ESP32 si pingano fra loro, 9 canali stabili, UDP magic `0xC5110003` |
| `esp32_radar3d/network_config.h` | Config per-board: `NODE_ID`, `NODE_MACS[3][6]`, `UDP_TARGET_IP` |
| `esp32_radar3d/FLASHING.md` | Guida passo-passo per flash + scoperta MAC + posizionamento |
| `esp32_csi_firmware/esp32_csi_firmware.ino` | Firmware legacy single-source (UDP magic `0xC5110001`). Funziona con pinger esterni. |

### `tests/` — Suite di test

```bash
python3 -m pytest tests/
```

---

## Setup completo (da zero)

### Prerequisiti

```bash
pip install pyserial numpy scikit-learn joblib matplotlib websockets
```

### ESP32

1. Apri `firmware/esp32_csi_firmware/esp32_csi_firmware.ino` in Arduino IDE
2. Configura `secrets.h` (copia da `secrets.h.example`)
3. Per AP mode: scommenta `#define CSI_AP_MODE`
4. Compila e carica su ESP32 (115200 baud)

### Training modello posizioni

```bash
# 1. Stai fuori dalla stanza → premi INVIO (30s vuoto)
# 2. Siediti sulla prima posizione → premi INVIO (30s)
# 3. Ripeti per ogni cella della griglia
# 4. Il modello viene salvato automaticamente
python3 -m csi.csi_mac --positions --grid 4x5 --use-ml
```

### Demo

**Modalità A — Grid classifier (heatmap a celle)**:

```bash
# Terminale 1: server CSI con classifier
python3 -m csi.csi_mac --monitor --use-ml --udp-port 5005 --ws-port 8765 --vitals

# Terminale 2: HTTP server
python3 -m http.server -d mapping 8000

# Browser:
#   http://localhost:8000/classroom_heatmap.html?ws=ws://localhost:8765   (2D)
#   http://localhost:8000/room_3d.html?room=2.5x6x2.7&grid=4x2            (3D)
```

**Modalità B — Blob regressor (coordinate continue + radar 3D)**:

```bash
# 1. Una volta sola: training con 5 punti
python3 -m csi.blob_cli --train --udp-port 5005 \
    --points "0.5,0.5;2.0,0.5;1.25,3.0;0.5,5.5;2.0,5.5"

# 2. Live monitor
python3 -m csi.blob_cli --monitor --udp-port 5005 --ws-port 8765

# 3. HTTP + browser
python3 -m http.server -d mapping 8000
#   http://localhost:8000/radar_3d.html?room=2.5x6x2.7&rx=0.2,0.5;2.3,3.0;0.2,5.5&tx=2.3,0.5;0.2,3.0;2.3,5.5
```

### Hardware setup raccomandato per blob mode

| Parametro | Valore tipico |
|---|---|
| ESP32 receivers | 3 (con `NODE_ID = 0, 1, 2` nei rispettivi firmware) |
| Pinger sources | 3-4 (telefoni con MAC randomization OFF, o laptop) |
| Layout RX | Non-collineari, formano triangolo che copre la stanza |
| Layout TX | Opposti ai RX per massimizzare diversità multipath |
| Altezza RX | ~1.5 m (mensola, treppiede) |
| Altezza TX | ~1 m (tavolo) |
| Stessa rete WiFi 2.4 GHz, canale fisso (1/6/11) |  |

Tutti i pinger devono fare `ping -i 0.02 <ip_di_un_ESP32>` per generare traffico CSI (~50 Hz × 3 RX). Lascia attivi tutti i ping durante training E inference.

---

## Documentation

| File | Contents |
|------|----------|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | System architecture, module map, data flow, dependency graph, resource budget |
| [`docs/API.md`](docs/API.md) | Full Python API reference for all modules |
| [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) | Per-feature spec sheets with hardware requirements and expected results (Italian) |
| [`docs/HOW_IT_WORKS.md`](docs/HOW_IT_WORKS.md) | System explanation from scratch (Italian) |
| [`docs/HARDWARE_SETUP.md`](docs/HARDWARE_SETUP.md) | Hardware requirements and setup guide |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | Debugging guide |
| [`docs/TESTING.md`](docs/TESTING.md) | Test suite reference |
| [`firmware/esp32_radar3d/FLASHING.md`](firmware/esp32_radar3d/FLASHING.md) | ESP32 firmware flashing guide |

---

## License

Progetto universitario — Politecnico di Milano, Corsi di Ingegneria dei Sistemi di Internet of Things 2025/26.
