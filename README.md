# Arduino Wi-Fi Sensing

> **Human presence, position, and breathing rate from everyday Wi-Fi.**
> ESP32 captures Channel State Information (CSI) → UNO Q processes it with ML → browser shows heatmap + vitals.

No cameras. No wearables. Just the Wi-Fi routers already in the room.

---

## What it can do

| Capability | How | Hardware |
|---|---|---|
| **Position detection** | ML classifier on CSI features → which seat/cell is occupied | ESP32 + UNO Q |
| **Browser heatmap** | Real-time probability grid via WebSocket | ESP32 + UNO Q + browser |
| **Breathing rate (BPM)** | Phase CSI → bandpass 0.1–0.5 Hz → zero-crossing BPM | ESP32 + UNO Q |
| **Presence/motion (basic)** | RSSI-based detector (works without ESP32) | UNO Q only |
| **Record & replay** | Save CSI to file, replay for offline development | ESP32 + UNO Q |

---

## Quick start — demo completa

### 1. Flash ESP32

Apri `firmware/esp32_csi_firmware/esp32_csi_firmware.ino` nell'IDE Arduino:

- **AP mode** (3 PC si connettono all'ESP32): scommenta `#define CSI_AP_MODE`
- **Serial mode** (ESP32 collegato via USB): commenta `#define CSI_AP_MODE`
- Copia `secrets.h.example` in `secrets.h` e configura il tuo WiFi

Compila e carica su ESP32.

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

## Architecture

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
python3 -m csi.csi_mac --monitor                          # Detection in tempo reale
python3 -m csi.csi_mac --monitor --use-ml                  # Con ML classifier
python3 -m csi.csi_mac --capture --seconds 60              # Registra CSI su file
python3 -m csi.csi_mac --calibrate                         # Calibrazione baseline
python3 -m csi.csi_mac --positions --grid 4x5 --use-ml     # Addestramento posizioni
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
| `csi_mac.py` | CLI principale: monitor, capture, calibrate, positions |
| `csi_ml.py` | ML classifier (Random Forest), PhaseBreathingEstimator, RSSIFeatureExtractor |
| `csi_processor.py` | Parser CSI (testo e binario ADR-018), CSIDetector |
| `csi_record.py` | Record su file e replay (utile per test offline) |
| `csi_plot.py` | Plot live ampiezza/fase CSI (waterfall, time, bar) |
| `phase_sanitizer.py` | Unwrap fase, rimozione outlier, smoothing |

### `mapping/` — Browser frontend

| File | Descrizione |
|---|---|
| `classroom_heatmap.html` | **Frontend demo**: heatmap griglia + BPM via WebSocket |

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
| `esp32_csi_firmware/esp32_csi_firmware.ino` | Firmware ESP32: CSI capture, AP mode, UDP streaming |

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

```bash
# Terminale 1: server CSI
python3 -m csi.csi_mac --monitor --use-ml --udp-port 5005 --ws-port 8765 --vitals

# Browser: apri mapping/classroom_heatmap.html?ws=ws://IP:8765
```

---

## License

Progetto universitario — Politecnico di Milano, Corsi di Ingegneria dei Sistemi di Internet of Things 2025/26.
