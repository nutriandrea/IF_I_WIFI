# If-I-Wi-Fy — Wi-Fi Sensing on Arduino UNO Q

> *Ambient intelligence through the radio waves that already surround you.*

Privacy-preserving presence and motion detection using nothing but the Wi-Fi that already fills the room. Built on the **Arduino UNO Q** — the dual-brain board that fuses a Linux AI processor (Qualcomm Dragonwing QRB2210) with a real-time microcontroller (STM32U585) in a single device.

No cameras. No wearables. No new infrastructure. The walls hum with information; we just listen.

For the full vision and applications (eldercare, smart workplaces, energy management, privacy-first security, retail analytics, industrial safety), see [docs/VISION.md](docs/VISION.md).

---

## Two sensing pipelines, one platform

This repository contains two independent pipelines that share the same Arduino UNO Q hardware and the same Python orchestration layer. They differ in the physical signal they exploit.

| | **RSSI** | **CSI** |
|---|---|---|
| Signal | Received Signal Strength Indicator (coarse, single dBm value per packet) | Channel State Information (per-subcarrier amplitude + phase) |
| Hardware needed | UNO Q only | UNO Q + ESP32 |
| Sample rate | 2–20 Hz (`/proc/net/wireless`) | 10–100 Hz |
| Resolution | One number per packet | 64–128 complex numbers per packet |
| What it can detect | Coarse presence, motion bursts | Fine-grained motion, breathing rate, gait, posture (with more work) |
| Setup complexity | Trivial | Higher (ESP32 firmware + UART bridge) |
| Status in this repo | Working end-to-end on UNO Q | Working end-to-end on UNO Q (when ESP32+bridge are wired) |

You can start with RSSI and progress to CSI when finer sensing is needed. Both run **on-device** with no cloud dependency.

---

## Hardware

### Arduino UNO Q

The dual-brain board that makes everything possible.

| Component | Role |
|---|---|
| Qualcomm Dragonwing QRB2210 (quad ARM A53 @ 2 GHz, Debian Linux) | Python pipeline: signal acquisition, feature extraction, ML inference, dashboard |
| STM32U585 (ARM Cortex-M33, Zephyr OS) | Deterministic real-time I/O: sensor reads, actuators, relays, LED feedback |
| Dual-band Wi-Fi 5 (2.4 / 5 GHz) + Bluetooth 5.1 | The sensing substrate itself (and the bridge to the network) |
| 2 GB LPDDR4 RAM, 16 GB+ eMMC | Enough to train, run, and log ML pipelines on-device |
| USB-C, ~€60 retail | Deployable at consumer scale |

**MCU↔Linux communication** does NOT go via a classic `/dev/tty*`. The Qualcomm side talks to the STM32 through the `arduino-router` service — a Go daemon that exposes a MessagePack RPC socket at `/var/run/arduino-router.sock`. The router is preinstalled and running by default. Internally it bridges `/dev/ttyHS1` (115200 baud, internal UART) to the Unix socket. Python clients call `client.call("method_name", args)` and the STM32 responds.

### ESP32 (CSI pipeline only)

A classic **ESP32** (WROOM-32 / DevKit V1) with built-in WiFi. Required only for the CSI pipeline. The ESP32 is the only widely available chip whose drivers expose per-subcarrier CSI from received 802.11 frames.

---

## Pipeline 1 — RSSI (UNO Q standalone)

Detects presence and motion from the WiFi signal strength of `wlan0` (the UNO Q's own radio). No extra hardware required.

### How it works

1. `/proc/net/wireless` is polled at 10–20 Hz for the RSSI of the last frame received from the AP the UNO Q is associated to. Zero `subprocess` overhead.
2. A short sliding window (~30 samples) extracts statistical features: mean, std, gradient, consecutive same-sign run length, and signal_avg from `iw station dump`.
3. A multi-metric scoring detector fuses these signals into a `presence_score`. Above a calibrated threshold → presence.

### Files

All in [`rssi/`](rssi/):
| File | Description |
|---|---|
| [`enhanced_presence.py`](rssi/enhanced_presence.py) | Main detector. Modes: `quick` / `baseline` / `movement` / `analyze` / `monitor` |
| [`calibrate_presence.py`](rssi/calibrate_presence.py) | Legacy calibration (std + adaptive delta strategies) |
| [`monitor_presence.py`](rssi/monitor_presence.py) | Monitor-mode capture (requires root, optional) |
| [`decision_engine.py`](rssi/decision_engine.py) | Orchestrator: RSSI → score → STM32 relay/LED action via bridge RPC |
| [`rssi_ml.py`](rssi/rssi_ml.py) | RSSI ML classifier (Random Forest) |
| [`bridge_client.py`](rssi/bridge_client.py) | CLI wrapper for arduino-router RPC |
| [`firmware/feasibility_bridge/`](firmware/feasibility_bridge/) | STM32 sketch — exposes `ping`, `get_sensors`, `set_relay`, `set_led` RPCs |

### Quick start

```bash
# On the UNO Q via SSH
cd ~/ArduinoApps/ArduinoWifiSensing
git pull
sudo apt-get install -y iw

# Quick calibration (30s baseline + 30s movement + analysis)
python3 -m rssi.enhanced_presence --mode quick

# Live monitoring with calibrated thresholds
python3 -m rssi.enhanced_presence --mode monitor

# Full orchestrator (RSSI + sensor RPCs + relay/LED)
python3 -m rssi.decision_engine
```

For the STM32 side (sensors + relay): upload [firmware/feasibility_bridge/feasibility_bridge.ino](firmware/feasibility_bridge/feasibility_bridge.ino) via Arduino IDE (Board: "Arduino UNO Q (STM32)").

### Known limit

The Qualcomm WiFi driver on UNO Q reports RSSI that alternates ±10 dBm sample-to-sample (likely dual antenna / dual MAC queue artifact). The `enhanced_presence` detector compensates with EMA filtering and signal_avg fusion, but residual noise overlaps with human-motion signal. RSSI alone gives **coarse** presence detection with non-trivial false-positive rates. For sub-second, room-mapping, or biometric-grade sensing, use the CSI pipeline.

---

## Pipeline 2 — CSI (UNO Q + ESP32)

Detects presence and motion from per-subcarrier amplitude and phase of WiFi packets received by the ESP32. Far more sensitive than RSSI because it captures **multipath fingerprinting** — the way a person's body reshapes the radio environment is visible per subcarrier, not just as a single dBm number.

### Credits

The ESP32 CSI capture firmware is adapted from the **[ESP32-CSI-Tool](https://stevenmhernandez.github.io/ESP32-CSI-Tool/)** by Steven M. Hernandez, which exposes the ESP-IDF `esp_wifi_set_csi_rx_cb()` API in a usable form. We adapted the output format (`CSI:<seq>:<rssi>:<noise>:<rate>:<bw>:<sub_count>:<r0,i0,...>`) so the UNO Q's STM32 bridge can stream it via the `arduino-router` to the Linux side. The Python parser also accepts the original `CSI_DATA,...` format for compatibility with Hernandez's analysis scripts.

### Architecture

```
   ESP32                    UNO Q STM32                  UNO Q Linux
┌─────────────┐    UART    ┌──────────────┐    RPC    ┌──────────────┐
│ CSI capture │──921600───▶│ csi_bridge   │──msgpack─▶│ csi_processor│
│ (Hernandez) │  D0/D1     │ buffer       │  router   │ detect/log   │
└─────────────┘            └──────────────┘           └──────────────┘
```

The ESP32 connects to a 2.4 GHz AP (your home WiFi), captures CSI from every received packet (beacons + responses), and streams CSV frames on its UART. The STM32 bridges those frames into the Linux side as RPC responses. Python parses, runs the detector, logs.

### Files

All in [`csi/`](csi/):
| File | Description |
|---|---|
| [`csi_processor.py`](csi/csi_processor.py) | Main CSI pipeline. Modes: `--ping` / `--monitor` / `--calibrate` / `--benchmark` / `--analyze`. Includes `parse_csi_line` and `CSIDetector` classes |
| [`csi_mac.py`](csi/csi_mac.py) | Standalone: read CSI directly from ESP32's USB or BLE on a Mac/PC. Bypasses UNO Q entirely. `--ble` per Bluetooth Low Energy |
| [`csi_ble.py`](csi/csi_ble.py) | BLE reader module: `BleReader` class + `ble_reader()` callback. Dipendenza: `bleak` |
| [`csi_ml.py`](csi/csi_ml.py) | CSI ML classifier + RSSIFeatureExtractor, RuleBasedClassifier, DopplerShiftExtractor, SleepQualityAnalyzer |
| [`csi_plot.py`](csi/csi_plot.py) | **Live real-time visualization**: `waterfall` (spectrogram), `time` (time-series), `bar` (bar chart) |
| [`csi_record.py`](csi/csi_record.py) | **Recording and replay** — record from serial (auto-rotate), replay to stdout/pipe/WebSocket |
| [`csi/seat_mapper.py`](csi/seat_mapper.py) | **Classroom demo**: fingerprint per sedia → RandomForest → live prediction + WebSocket |
| [`phase_sanitizer.py`](csi/phase_sanitizer.py) | Phase sanitization (unwrap → outlier removal → smoothing) |

Firmware in [`firmware/`](firmware/):
| File | Description |
|---|---|
| [`firmware/esp32_csi_firmware/`](firmware/esp32_csi_firmware/) | ESP32 sketch — captures CSI and streams CSV via UART (wired) |
| [`firmware/esp32_csi_bt/`](firmware/esp32_csi_bt/) | ESP32 sketch — captures CSI via Bluetooth SPP (wireless) |
| [`firmware/esp32_csi_bridge/`](firmware/esp32_csi_bridge/) | STM32 sketch — buffers ESP32 frames, exposes RPCs |

### Wireless: ESP32 via Bluetooth Low Energy (nessun cavo)

Il firmware [`esp32_csi_bt`](firmware/esp32_csi_bt/) usa **Bluetooth Low Energy (BLE)** — più moderno, più efficiente, e compatibile con UNO Q (che non ha Classic Bluetooth).

**Nessun cablaggio:** basta alimentazione USB (power bank). L'ESP32 cattura CSI via WiFi e la streamma via BLE usando il **Nordic UART Service (NUS)**.

```
   ESP32                          Mac / UNO Q Linux
┌──────────────┐     BLE NUS     ┌──────────────────┐
│ CSI capture  │────────────────▶│ python (bleak)    │
│ + BLE NUS TX │ 6E400003-...   │ iter_lines()      │
└──────────────┘                 └──────────────────┘
```

#### Firmware BLE

Il firmware si trova in [`firmware/esp32_csi_bt/esp32_csi_ble.ino`](firmware/esp32_csi_bt/esp32_csi_ble.ino):

| Parametro | Valore |
|---|---|
| Nome dispositivo | `ESP32_CSI` |
| UUID servizio | `6E400001-B5A3-F393-E0A9-E50E24DCCA9F` (Nordic UART Service) |
| TX (ESP32 → host) | `6E400003-B5A3-F393-E0A9-E50E24DCCA9F` (notify) |
| RX (host → ESP32) | `6E400002-B5A3-F393-E0A9-E50E24DCCA9F` (write) |
| CSI baud (WiFi → BLE) | interna, nessuna porta seriale necessaria |

#### Modulo Python `csi_ble.py`

[`csi/csi_ble.py`](csi/csi_ble.py) implementa `BleReader` (classe) e `ble_reader()` (funzione callback-based) usando **bleak** (cross-platform BLE). L'iteratore `iter_lines()` produce linee CSI complete in formato identico a quello seriale, rendendo il resto della pipeline trasparente.

#### `--ble` flag

Tutti i tool supportano `--ble` per passare da seriale a BLE senza cambiare codice:

```bash
# CSI monitor via BLE
python3 -m csi.csi_mac --ble --monitor

# Calibrate via BLE
python3 -m csi.csi_mac --ble --calibrate

# Recording via BLE
python3 -m csi.csi_record --record --ble

# Seat mapper training via BLE
python3 -m csi.seat_mapper --mode train --ble --num-seats 10

# Seat mapper live via BLE
python3 -m csi.seat_mapper --mode live --ble
```

**Dipendenze:** `pip install bleak` (necessario su Mac e su UNO Q Linux MPU).

### Wiring (ESP32 → UNO Q, via UART cablata)

```
ESP32 GND  →  UNO Q GND          (mandatory)
ESP32 5V   →  UNO Q 5V
ESP32 TX   →  UNO Q D0           (STM32 Serial1 RX)
ESP32 RX   →  UNO Q D1           (STM32 Serial1 TX)
Baud rate: 921600 (or 115200 if you flashed the diagnostic build)
```

### Quick start

**On a host computer (Mac/PC):**
```bash
cd firmware/esp32_csi_firmware
cp secrets.h.example secrets.h    # then edit SSID/PASS, AP must be 2.4 GHz!
```

Open `firmware/esp32_csi_firmware/esp32_csi_firmware.ino` in Arduino IDE. Board: **ESP32 Dev Module**. Upload Speed: **115200** (more reliable than 921600 with cheap USB-Serial chips). Upload to ESP32 via USB. Watch the Serial Monitor at the firmware's baud rate (currently 115200 in the diagnostic build) for `ESP32_CSI_READY` → `WiFi:connecting...` → `WiFi:OK,<ip>` → `CSI:enabled`.

**On the UNO Q (full pipeline through the STM32 bridge):**
```bash
# 1. Upload firmware/esp32_csi_bridge/esp32_csi_bridge.ino to the STM32 via Arduino IDE
#    (Board: "Arduino UNO Q (STM32)")
# 2. Wire ESP32 to D0/D1 as above
# 3. Power-cycle / reset both

cd ~/ArduinoWifiSensing
git pull
pip install msgpack
python3 -m csi.csi_processor --ping       # → ESP32: pong:OK
python3 -m csi.csi_processor --monitor    # live presence detection
```

**On a Mac (CSI without UNO Q, ESP32 only):**
```bash
pip install pyserial
python3 -m csi.csi_mac --monitor          # autodetects /dev/cu.usbserial-*
python3 -m csi.csi_mac --capture --seconds 60 --label test1
python3 -m csi.csi_mac --calibrate --seconds 30
```

**Analisi e replay:**
```bash
python3 -m csi.csi_plot --mode waterfall     # live waterfall (con ESP32)
python3 -m csi.csi_plot --mode bar           # bar chart per subcarrier
python3 -m csi.csi_record --record           # registra su file (seriale)
python3 -m csi.csi_record --record --ble     # registra su file (BLE)
python3 -m csi.csi_record --info csi_captures/csi_capture_*.txt
python3 -m csi.csi_record --replay capture.txt | python3 -m csi.csi_mac --stdin
```

### Why CSI matters

RSSI gives one number per packet. CSI gives 64–128 (one per subcarrier). When a person enters the room, RSSI may barely budge, but the per-subcarrier multipath structure changes dramatically — different subcarriers experience different constructive/destructive interference from the new body. The variance across subcarriers, the temporal evolution of phase, and cross-subcarrier covariance are all rich features that academic literature has exploited for breathing-rate estimation, gait recognition, fall detection, and pose estimation. This repo provides the substrate.

---

## Pipeline 3 — Machine Learning Classifiers

Both RSSI and CSI pipelines can be **upgraded from threshold-based detectors to Random Forest classifiers**, replacing hand-tuned thresholds with models that learn the specific noise and signal patterns of the deployment environment.

### Why Random Forest

| Requirement | Why Random Forest fits |
|---|---|
| Non-linear decision boundary | RSSI noise (±10 dBm on UNO Q) is not linearly separable — a single threshold doesn't cut it |
| Robust to outliers | Ensemble of 30–50 trees prevents the ±10 dBm spikes from triggering false positives |
| No feature scaling | RSSI is in dBm, CSI subcarrier amplitudes are in ADC units — RF handles mixed scales natively |
| Feature importance | Automatically identifies which subcarriers or RSSI features are most indicative of presence |
| Fast inference | ~0.1 ms per prediction (`O(depth × trees)`) — negligible overhead at 10–100 Hz |
| Tiny model | 30 trees × depth 5 × 10 features ≈ a few KB on disk |

### RSSIClassifier — Adaptive presence from RSSI

**File:** [`rssi_ml.py`](rssi_ml.py)

Replaces `GradientDetector` with a **Random Forest binary classifier** (EMPTY / PRESENT).

**Features** (extracted from a sliding window of ~20 RSSI samples):

```
rssi_mean, rssi_std, rssi_min, rssi_max, rssi_range
gradient_mean, gradient_std, gradient_max_abs
zero_crossing_rate
```

**Training:** During calibration (30s baseline + 30s movement at 20 Hz ≈ 1200 labeled samples), the classifier learns to distinguish ambient noise from human-induced signal variation.

**Usage:**

```bash
# Quick calibration + threshold analysis + ML training
python3 -m rssi.enhanced_presence --mode quick --train-ml

# Or separately: calibrate then train
python3 -m rssi.enhanced_presence --mode baseline --seconds 30
python3 -m rssi.enhanced_presence --mode movement --seconds 30
python3 -m rssi.enhanced_presence --mode train-ml

# Monitor with ML classifier (soglia probabilità 0.4)
python3 -m rssi.enhanced_presence --mode monitor --use-ml --ml-threshold 0.4
```

---
### Multi-AP channel hopping

**Configurazione:** [`firmware/esp32_csi_firmware/secrets.h.example`](firmware/esp32_csi_firmware/secrets.h.example)

L'ESP32 commuta ciclicamente tra **3 AP telefonici** su canali 2.4 GHz fissi (1, 6, 11) per ottenere 3 prospettive CSI indipendenti. Il flusso seriale include linee `AP:<id>` e `AP_SWITCH:<id>` per tracciare l'AP attuale. Il parser in `csi_processor.py` eredita `ap_id` per ogni frame CSI.

Numero AP configurabile via `#define NUM_APS` — se non definito, il firmware opera in modalita mono-AP.

**Vedi:** [docs/MULTI_AP_RUVIEW.md#1-multi-ap-channel-hopping](docs/MULTI_AP_RUVIEW.md#1-multi-ap-channel-hopping)

---

### CSIClassifier — Activity recognition from CSI

**File:** [`csi_ml.py`](csi_ml.py)

Replaces `CSIDetector` with a **Random Forest multi-class classifier** (EMPTY / STATIONARY / MOVEMENT).

**Features** (extracted from a window of ~30 CSI frames):

| Group | Features | Count |
|---|---|---|
| Per-subcarrier profile | `sub_mean_00..31` + `sub_std_00..31` | 64 |
| Spectral shape | `variance_across_subcarriers`, `sub_peak_mean/std` | 3 |
| Temporal variance | `temporal_variance`, `temporal_std_variance` | 2 |
| Amplitude dynamics | `ampl_mean_min/max/range`, `ampl_std_min/max/range` | 6 |
| Radio metadata | `rssi_mean/std`, `noise_floor_mean` | 3 |
| **Total** | | **~88** |

**Classes:**

| Class | What it means | Physical basis |
|---|---|---|
| `EMPTY` | No person in the room | Baseline multipath profile |
| `STATIONARY` | Person sitting/standing still | Micro-Doppler from breathing (±0.1 dB per subcarrier) |
| `MOVEMENT` | Person walking or gesturing | Large per-subcarrier amplitude fluctuations (±dB) |

**Usage:**

```bash
# Via UNO Q bridge
python3 -m csi.csi_processor --calibrate --train-ml --seconds 30
python3 -m csi.csi_processor --monitor --use-ml

# Via ESP32 USB (Mac/PC standalone)
python3 -m csi.csi_mac --calibrate --train-ml --seconds 30
python3 -m csi.csi_mac --monitor --use-ml

# Three-class training (add STATIONARY phase)
python3 -m csi.csi_processor --calibrate --train-ml --seconds 30 --stationary-seconds 30
```
### MultiAP CSIClassifier

**File:** [`csi/csi_ml.py`](csi/csi_ml.py)

Estensione che mantiene **3 buffer interni** (uno per AP) e concatena i vettori feature in un singolo vettore di dimensione `CSI_FEATURE_SIZE × NUM_APS` (= 243 per 3 AP). Addestramento e inferenza con RandomForest.

```bash
python3 -m csi.csi_mac --calibrate --train-ml --num-aps 3
python3 -m csi.csi_mac --monitor --use-ml --num-aps 3
```

**Vedi:** [docs/MULTI_AP_RUVIEW.md#5-multiap-csiclassifier](docs/MULTI_AP_RUVIEW.md#5-multiap-csiclassifier)

### Phase Sanitizer

**File:** [`csi/phase_sanitizer.py`](csi/phase_sanitizer.py)

Rimuove artefatti CFO/SFO/PLL dalla fase CSI grezza: unwrap → Z-score outlier removal + interpolazione → moving average smoothing. Include `phase_difference()` per calcolo differenziale (usato dal Doppler).

**Vedi:** [docs/MULTI_AP_RUVIEW.md#2-phase-sanitizer](docs/MULTI_AP_RUVIEW.md#2-phase-sanitizer)

### RSSI Feature Extraction + Rule-Based Classifier

**File:** [`csi_ml.py`](csi_ml.py) — classi `RSSIFeatureExtractor`, `RuleBasedClassifier`

Estrae feature tempo-frequenza dal RSSI (media, varianza, skewness, kurtosis, FFT con bande respiratoria 0.1-0.5 Hz e motoria 0.5-3.0 Hz, CUSUM change-point detection) e classifica in **EMPTY / STATIONARY / MOVEMENT** con confidence scoring.

**Vedi:** [docs/MULTI_AP_RUVIEW.md#3-rssi-feature-extraction](docs/MULTI_AP_RUVIEW.md#3-rssi-feature-extraction)

### Doppler Shift + Sleep Quality

**File:** [`csi_ml.py`](csi_ml.py) — classi `DopplerShiftExtractor`, `SleepQualityAnalyzer`

Stima velocita radiale dallo sfasamento CSI (`f_Doppler = Δφ/(2π·Δt)`) e analisi respiratoria con stima stage sonno (AWAKE/REM/LIGHT/DEEP) e rilevamento apnea.

**Vedi:** [docs/MULTI_AP_RUVIEW.md#6-doppler-shift-extractor](docs/MULTI_AP_RUVIEW.md#6-doppler-shift-extractor)

### Room Mapping + Live Position Tracking

**File:** [`mapping/room_mapper.py`](mapping/room_mapper.py), [`mapping/room_server.py`](mapping/room_server.py), [`mapping/room_map.html`](mapping/room_map.html), [`mapping/room_map_3d.html`](mapping/room_map_3d.html)

Mappa la stanza tramite fingerprinting RSSI (3 AP) e stima posizione live con weighted k-NN. Il server WebSocket collega ESP32 → PositionEstimator → browser.

**2D canvas:** [`mapping/room_map.html`](mapping/room_map.html) — puntino animato su griglia stanza.

**3D Three.js:** [`mapping/room_map_3d.html`](mapping/room_map_3d.html) — stanza 3D con heatmap probabilistica,
AP markers, tracciato movimento, cursori segnale, camera orbitale.

```bash
# 1. Calibrazione guidata
python3 -m mapping.room_mapper calibrate fingerprint.json

# 2. Configura posizione AP (per 3D)
python3 -m mapping.room_mapper setup-aps fingerprint.json

# 3. Server live (con ESP32 o in simulazione)
python3 -m mapping.room_server --simulate --fingerprint fingerprint.json

# 4. Browser → 2D: http://localhost:8080/room_map.html
#             3D: http://localhost:8080/room_map_3d.html
```

**Vedi:** [docs/MULTI_AP_RUVIEW.md#8-room-mapping-e-localizzazione](docs/MULTI_AP_RUVIEW.md#8-room-mapping-e-localizzazione)

### CSI Seat Mapper — Demo in aula

**File:** [`csi/seat_mapper.py`](csi/seat_mapper.py), [`mapping/classroom_heatmap.html`](mapping/classroom_heatmap.html)

Fingerprinting CSI multi-AP per riconoscere quale sedia è occupata. Addestra un RandomForest su dati raccolti sedia per sedia, poi predice in tempo reale via WebSocket con visualizzazione browser.

```bash
# 1. Training interattivo (prima della demo)
python3 -m csi.seat_mapper --mode train --num-seats 10 --seconds 30

#    Via BLE (invece di seriale):
python3 -m csi.seat_mapper --mode train --ble --num-seats 10

# 2. Live prediction (demo)
python3 -m csi.seat_mapper --mode live --port 8080

#    Via BLE:
python3 -m csi.seat_mapper --mode live --ble --port 8080

# Browser → http://localhost:8080/classroom_heatmap.html
```

La visualizzazione mostra la pianta dell'aula con sedie colorate: 🟢 libera, 🔴 occupata. Confidenza, RSSI e probabilità per classe in sidebar.

### Feature importance analysis

Both classifiers expose `feature_importance` — a ranked list of which features most influence the decision. This is valuable for understanding **which subcarriers** or **which RSSI statistics** carry the most signal, which can inform future hardware or algorithm optimizations.

```bash
# Show feature importance after training
python3 -m rssi.rssi_ml --load rssi_model.joblib
python3 -m csi.csi_ml --load csi_model.joblib
```

### Dependencies

```bash
# UNO Q (Debian)
sudo apt install python3-sklearn python3-joblib

# Mac / PC
pip install scikit-learn joblib
```

Both `rssi_ml.py` and `csi_ml.py` use **lazy imports** — they load gracefully without sklearn (returning a clear error message) if the dependencies are not installed, so existing users who don't use the ML features are unaffected.

### New: RuView-inspired features

| File | What it does |
|---|---|
| [`csi/phase_sanitizer.py`](csi/phase_sanitizer.py) | Phase sanitization pipeline (unwrap → outlier removal → smoothing) |
| [`csi/csi_ml.py`](csi/csi_ml.py) | Also contains: RSSIFeatureExtractor, RuleBasedClassifier, DopplerShiftExtractor, SleepQualityAnalyzer |
| [`csi/csi_ble.py`](csi/csi_ble.py) | BLE reader: `BleReader` + `ble_reader()`. Dipendenza: `bleak` |
| [`csi/csi_plot.py`](csi/csi_plot.py) | Live CSI visualization (waterfall / time / bar) |
| [`csi/csi_record.py`](csi/csi_record.py) | CSI recording and replay |
| [`csi/seat_mapper.py`](csi/seat_mapper.py) | Seat fingerprint → RandomForest → live prediction + WebSocket |
| [`mapping/classroom_heatmap.html`](mapping/classroom_heatmap.html) | Classroom seat visualization (WebSocket) |
| [`tests/test_seat_mapper.py`](tests/test_seat_mapper.py) | 12 tests for seat classifier |
| [`tests/test_ruview_features.py`](tests/test_ruview_features.py) | 29 tests covering all RuView-inspired features |
| [`tests/test_multi_ap.py`](tests/test_multi_ap.py) | 7 tests for multi-AP classifier |
| [`tests/test_csi_tools.py`](tests/test_csi_tools.py) | 8 tests for csi_plot.py and csi_record.py |
| [`mapping/room_mapper.py`](mapping/room_mapper.py) | FingerprintMap + PositionEstimator (weighted k-NN) |
| [`mapping/room_server.py`](mapping/room_server.py) | WebSocket bridge: ESP32 → PositionEstimator → browser |
| [`mapping/room_map.html`](mapping/room_map.html) | HTML5 canvas 2D with live position dot |
| [`mapping/room_map_3d.html`](mapping/room_map_3d.html) | Three.js 3D room with heatmap, AP markers, person tracking |
| [`tests/test_room_mapper.py`](tests/test_room_mapper.py) | 17 tests for fingerprint map and estimator |
| [`docs/MULTI_AP_RUVIEW.md`](docs/MULTI_AP_RUVIEW.md) | Full documentation of all new features |

**Vedi la documentazione completa:** [docs/MULTI_AP_RUVIEW.md](docs/MULTI_AP_RUVIEW.md)

---

## Repository layout

```
.
├── README.md                           # this file
├── .gitignore
│
├── docs/                               # documentation
│   ├── VISION.md                       # full If-I-Wi-Fy vision
│   ├── TESTING.md                      # how to run tests
│   ├── MULTI_AP_RUVIEW.md              # multi-AP, RuView, room mapping docs
│   ├── arduino_cloud_integration.md
│   ├── demo_24h_plan.md
│   └── shopping_list.md
│
├── rssi/                               # RSSI pipeline (UNO Q standalone)
│   ├── __init__.py
│   ├── enhanced_presence.py            # main detector
│   ├── calibrate_presence.py           # calibration tool
│   ├── monitor_presence.py             # monitor-mode capture
│   ├── decision_engine.py              # orchestrator + STM32 bridge
│   ├── rssi_ml.py                      # Random Forest classifier
│   └── bridge_client.py                # arduino-router RPC client
│
├── csi/                                # CSI pipeline (UNO Q + ESP32)
│   ├── __init__.py
│   ├── csi_processor.py                # main CSI pipeline + CSIDetector
│   ├── csi_mac.py                      # direct USB/BLE CSI (Mac/PC)
│   ├── csi_ble.py                      # BLE reader (bleak)
│   ├── csi_ml.py                       # ML classifier + feature extractors
│   ├── csi_plot.py                     # live visualization (matplotlib)
│   ├── csi_record.py                   # record & replay
│   ├── seat_mapper.py                  # seat fingerprint + classroom demo
│   └── phase_sanitizer.py              # phase unwrap + outlier removal
│
├── mapping/                            # Room mapping + 3D visualization
│   ├── __init__.py
│   ├── room_mapper.py                  # FingerprintMap + PositionEstimator
│   ├── room_server.py                  # WebSocket bridge → browser
│   ├── room_map.html                   # 2D canvas visualizer
│   ├── room_map_3d.html                # 3D Three.js + heatmap
│   └── classroom_heatmap.html          # seat mapper visualization
│
├── firmware/                           # Microcontroller sketches
│   ├── feasibility_bridge/             # STM32 sketch (RSSI pipeline)
│   ├── esp32_csi_firmware/             # ESP32 sketch: CSI via UART (wired)
│   ├── esp32_csi_bt/                   # ESP32 sketch: CSI via Bluetooth (wireless)
│   └── esp32_csi_bridge/               # STM32 sketch: CSI bridge
│
├── tests/                              # All Python tests (109 total)
│   ├── __init__.py
│   ├── conftest.py                     # pytest path setup
│   ├── test_csi_processor.py           # CSI parser + detector (21 tests)
│   ├── test_csi_tools.py               # plot + record (8 tests)
│   ├── test_detectors.py               # gradient detector
│   ├── test_ml_classifiers.py          # RSSI + CSI ML (6 tests)
│   ├── test_multi_ap.py                # multi-AP support (7 tests)
│   ├── test_room_mapper.py             # fingerprint + positioning (17 tests)
│   ├── test_seat_mapper.py             # seat classifier (12 tests)
│   └── test_ruview_features.py         # RuView features (29 tests)
│
├── rssi_model.joblib                   # trained RSSI model (generated)
├── csi_model.joblib                    # trained CSI model (generated)
└── csi_captures/                       # CSI recording output (created at runtime)
``` (gitignored)
```

---

## Setup

### Common dependencies (UNO Q Linux side)

```bash
sudo apt-get install -y iw python3-pip
pip install msgpack pyserial
```

`iw` is needed by the RSSI pipeline. `msgpack` is needed by anything that talks to `arduino-router` (the bridge). `pyserial` is needed by `csi_mac.py` (host-side standalone CSI).  
`bleak` is needed by `csi_ble.py` (BLE reader).

For the **ML classifiers** (optional — all other features work without it):
```bash
# UNO Q (Debian)
sudo apt install python3-sklearn python3-joblib

# Mac / PC
pip install scikit-learn joblib
```

### Mac / PC (CSI standalone, no UNO Q)

```bash
pip install pyserial bleak
# msgpack NOT required — csi_processor.py imports it lazily only when
# RouterClient.connect() is called
```

### STM32 sketches

Use Arduino IDE with the **Arduino UNO Q (STM32)** core selected. You must install the **Arduino_RouterBridge** library via Library Manager (the core ships a stub that errors out otherwise).

Only one of `feasibility_bridge` or `esp32_csi_bridge` can run on the STM32 at a time — they expose different RPC method sets. Choose based on which pipeline you're testing.

### ESP32 sketch

Arduino IDE with the **esp32** core (Espressif) and Board: **ESP32 Dev Module**. The CSI API uses `esp_wifi_set_csi()` from ESP-IDF, exposed by the Arduino core. No external library needed.

Set Upload Speed to **115200** (or **460800** max) — 921600 fails on many USB-Serial chips and cables.

---

## Troubleshooting

**Upload to ESP32 fails with "chip stopped responding"**: lower Tools > Upload Speed to 115200, use a short data-quality USB cable, plug directly into the host (no hub), optionally hold BOOT during the whole upload.

**Serial Monitor shows only garbage**: Arduino IDE 2.x has a known bug where the baud-rate dropdown doesn't always take effect. Close and reopen Serial Monitor, or use `screen /dev/cu.usbserial-XXX 115200` from a terminal.

**ESP32 boots then reset-loops**: USB hub current-limited. Plug ESP32 directly into the host. The firmware has brownout detector disabled and TX power reduced to mitigate; if it still loops, the supply path is too weak.

**`csi_processor.py --ping` returns `ESP32_NOT_CONNECTED`**: the STM32 bridge can't reach the ESP32 via UART. Verify wiring (TX/RX not swapped, GND shared), baud match (both at 921600 in production firmware), and that the ESP32 is actually running its firmware (Serial Monitor shows boot messages).

**RSSI presence detection gives constant false positives**: the Qualcomm driver alternates RSSI between two values. This is known. Use `signal_avg` (which `enhanced_presence` does) and a higher gradient threshold (`--grad-threshold 3.0`).

For more, see [docs/TESTING.md](docs/TESTING.md).

---

## Privacy by construction

Everything runs on the edge. No frames are sent to the cloud. No biometric features are extracted. The signals we exploit (RSSI, CSI amplitude/phase per subcarrier) cannot be reversed into identifying information about the persons present. This is privacy-preserving **as a property of the physics**, not as a promise from a manufacturer.

See [docs/VISION.md](docs/VISION.md) for the broader argument.

---

## License

To be decided. The Steven Hernandez ESP32-CSI-Tool that the ESP32 firmware adapts is under its own license — consult the upstream repository for terms.
