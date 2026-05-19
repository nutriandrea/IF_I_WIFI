# Multi-AP CSI Sensing & Room Mapping

Documentazione delle feature avanzate sviluppate per il pipeline CSI
con architettura a 3 AP telefonici + ESP32 + Arduino UNO Q.

---

## Indice

1. [Multi-AP Channel Hopping](#1-multi-ap-channel-hopping)
2. [Phase Sanitizer](#2-phase-sanitizer)
3. [RSSI Feature Extraction](#3-rssi-feature-extraction)
4. [Rule-Based Classifier](#4-rule-based-classifier)
5. [MultiAP CSIClassifier](#5-multiap-csiclassifier)
6. [Doppler Shift Extractor](#6-doppler-shift-extractor)
7. [Sleep Quality Analyzer](#7-sleep-quality-analyzer)
8. [Room Mapping e Localizzazione](#8-room-mapping-e-localizzazione)
9. [Room Server e Web UI](#9-room-server-e-web-ui)
10. [Confronto con RuView](#10-confronto-ruview)

---

## 1. Multi-AP Channel Hopping

L'ESP32 commuta ciclicamente tra 3 AP telefonici su canali 2.4 GHz
fissi (1, 6, 11) per ottenere 3 prospettive CSI indipendenti.

### Firmware

File: [`esp32_csi_firmware/esp32_csi_firmware.ino`](../esp32_csi_firmware/esp32_csi_firmware.ino)

```
NUM_APS=3 definiti in secrets.h
┌─────────────────────────────────────────┐
│ AP_SSID[0] — Smartphone 1 (canale f~1) │
│ AP_SSID[1] — Smartphone 2 (canale f~6)  │
│ AP_SSID[2] — Smartphone 3 (canale f~11) │
└─────────────────────────────────────────┘
     │ ogni ~2s switch
     ▼
┌──────────────────────┐
│ esp_wifi_set_channel │──→ CSI callback → UART "CSI:..."
└──────────────────────┘
     │ a ogni switch
     ▼
"AP_SWITCH:<id>"  ← linea marker nel flusso seriale
```

Ogni frame CSI e associato all'AP corrente via `ap_id` nel parser,
permettendo al livello Python di separare i flussi.

### Configurazione

`secrets.h.example`:

```cpp
#define NUM_APS 3
static const char* AP_SSID[NUM_APS] = {"AP_1", "AP_2", "AP_3"};
static const char* AP_PASSWORD[NUM_APS] = {"pw1", "pw2", "pw3"};
static const int   AP_CHANNEL[NUM_APS] = {1, 6, 11};
```

### Parser

File: [`csi_processor.py`](../csi_processor.py)

- Linea `AP:<id>` → imposta `current_ap` nel contesto
- Linea `AP_SWITCH:<id>` → reset contesto AP
- Ogni frame CSI successivo eredita `ap_id` dal contesto

---

## 2. Phase Sanitizer

Rimuove artefatti di misura dalla fase CSI grezza (distorta da
CFO, SFO, PLL drift, rumore termico) seguendo l'approccio RuView.

File: [`phase_sanitizer.py`](../phase_sanitizer.py)

### Pipeline

```
φ_raw ──→ unwrap ──→ outlier removal ──→ smooth ──→ φ_clean
                 (Z-score)        (moving avg)
```

### Fasi

| Step | Metodo | Parametri |
|------|--------|-----------|
| Unwrap | `numpy.unwrap` / `scipy` | Default `period=π` |
| Outlier removal | Z-score con MAD | `z_threshold=2.5` |
| Interpolation | Lineare sugli outlier rimossi | — |
| Smoothing | Moving average convolutivo | `window=5`, padding edge |

### Differenza di fase (Doppler)

```python
from phase_sanitizer import PhaseSanitizer
san = PhaseSanitizer(z_threshold=2.5, smooth_window=5)
dphi = san.phase_difference(phi1, phi2)  # Δφ per coppie consecutive
```

---

## 3. RSSI Feature Extraction

Estrae feature tempo-frequenza dal flusso RSSI di una finestra
scorrevole, ispirato al paper RuView.

File: [`csi_ml.py`](../csi_ml.py) — classe `RSSIFeatureExtractor`

### Feature Time-Domain (per finestra)

| Feature | Descrizione |
|---------|-------------|
| `mean` | Media RSSI |
| `variance` | Varianza |
| `std` | Deviazione standard |
| `skewness` | Asimmetria distribuzione |
| `kurtosis` | Curtosi (picco distribuzione) |
| `range` | Max - min |
| `iqr` | Interquartile range |

### Feature Frequency-Domain (FFT)

| Feature | Descrizione |
|---------|-------------|
| `dominant_freq` | Frequenza con massima energia |
| `breathing_energy` | Energia nella banda 0.1-0.5 Hz |
| `motion_energy` | Energia nella banda 0.5-3.0 Hz |

Si applica finestra Hann prima della rFFT. Se `scipy` non e
disponibile, le feature frequenzali restituiscono 0.0
(graceful degradation).

### CUSUM Change-Point Detection

Algoritmo CUSUM (Cumulative Sum) per rilevare cambi improvvisi
nel segnale RSSI:

```
S⁺(t) = max(0, S⁺(t-1) + x(t) - μ - δ)
S⁻(t) = min(0, S⁻(t-1) + x(t) - μ + δ)
threshold = λ · σ
```

Output: posizioni dei change-point e varianza cumulativa normalizzata.

### Dataclass

```python
@dataclass
class RSSIFeatures:
    mean: float
    variance: float
    std: float
    skewness: float
    kurtosis: float
    range_: float
    iqr: float
    dominant_freq: float
    breathing_energy: float
    motion_energy: float
    cusum_score: float
```

Metodi: `to_dict()` per log, `to_vector()` per ML.

---

## 4. Rule-Based Classifier

Classificatore ternario EMPTY / STATIONARY / MOVEMENT basato
su regole configurabili, ispirato all'approccio rule-based di RuView.

File: [`csi_ml.py`](../csi_ml.py) — classe `RuleBasedClassifier`

### Logica di decisione

```
variance < presence_threshold → EMPTY
variance ≥ presence_threshold AND motion_energy < motion_threshold → STATIONARY
variance ≥ presence_threshold AND motion_energy ≥ motion_threshold → MOVEMENT
```

### Parametri

| Parametro | Default | Ruolo |
|-----------|---------|-------|
| `presence_variance_threshold` | 0.5 | Varianza RSSI minima per presenze |
| `motion_energy_threshold` | 1.0 | Energia banda motoria minima per movimento |

### Confidence Scoring

```
confidence = base(60%) + spectral_bonus(20%) + agreement_bonus(20%)

- spectral_bonus: se breathing_energy > motion_energy → STATIONARY
- agreement_bonus: se time-domain e freq-domain concordano
```

---

## 5. MultiAP CSIClassifier

Estensione di `CSIClassifier` che mantiene buffer separati per
ogni AP e concatena le feature in un singolo vettore.

File: [`csi_ml.py`](../csi_ml.py) — classe `MultiAPCSIClassifier`

### Architettura

```
┌──────────┐     ┌──────────────┐
│ Buffer 0 │────→│ FeatureVec 0 │──┐
├──────────┤     ├──────────────┤  │
│ Buffer 1 │────→│ FeatureVec 1 │──┤──→ concat → RF predict
├──────────┤     ├──────────────┤  │
│ Buffer 2 │────→│ FeatureVec 2 │──┘
└──────────┘     └──────────────┘
```

- `window_frames=30` per buffer
- Feature vector totale: `CSI_FEATURE_SIZE × NUM_APS` (= 243 per 3 AP)
- Addestramento e inferenza con RandomForest (scikit-learn)
- Persistenza via joblib

### Uso

```bash
python3 csi_mac.py --calibrate --train-ml --num-aps 3
python3 csi_mac.py --monitor --use-ml --num-aps 3
```

---

## 6. Doppler Shift Extractor

Stima lo spostamento Doppler dalla fase CSI per rilevare
movimento direzionale.

File: [`csi_ml.py`](../csi_ml.py) — classe `DopplerShiftExtractor`

### Principio

Lo sfasamento tra due frame CSI consecutivi nel tempo e
proporzionale alla velocita radiale del bersaglio:

```
f_Doppler = Δφ / (2π · Δt)
```

### Pipeline

```
φ(t) ──→ PhaseSanitizer.sanitize ──→ φ_clean
φ_clean[i] - φ_clean[i-1] ──→ Δφ
Δφ / (2π · Δt) ──→ f_Doppler
```

### Feature prodotte

| Feature | Descrizione |
|---------|-------------|
| `doppler_mean` | Media frequenza Doppler |
| `doppler_std` | Deviazione standard |
| `doppler_max` | Picco positivo |
| `doppler_min` | Picco negativo |
| `doppler_abs_max` | Picco assoluto |
| `doppler_band_power` | Energia FFT in banda 0.5-10 Hz |

### Uso

```python
from csi_ml import DopplerShiftExtractor

extractor = DopplerShiftExtractor(window=20)
for frame in csi_stream:
    extractor.add_frame(frame)
    if extractor.ready:
        features = extractor.compute()
        print(f"Velocita': {features['doppler_mean']:.2f} Hz")
```

---

## 7. Sleep Quality Analyzer

Analizza la qualita del sonno dal segnale respiratorio estratto
dal flusso RSSI/CSI.

File: [`csi_ml.py`](../csi_ml.py) — classe `SleepQualityAnalyzer`

### Parametri biometrici calcolati

| Metrica | Metodo |
|---------|--------|
| **Breathing Rate (BPM)** | Frequenza dominante nella banda 0.1-0.5 Hz × 60 |
| **Breathing Regularity** | 1 - (std_inter_bpm / mean_inter_bpm) |
| **Sleep Stage** | Mappatura BPM + regolarita → AWAKE/REM/LIGHT/DEEP |
| **Apnea Events** | Calo energia respiratoria >80% vs massimo storico |

### Soglie Sleep Stage

| Stage | BPM range | Regularity |
|-------|-----------|------------|
| AWAKE | > 18 | < 0.5 |
| REM | 14-20 | 0.5-0.8 |
| LIGHT | 10-16 | 0.6-0.9 |
| DEEP | 8-14 | > 0.8 |

(Le soglie sono range sovrapposti intenzionalmente — vince il
matching con regularity piu alta.)

### Uso

```python
from csi_ml import SleepQualityAnalyzer

analyzer = SleepQualityAnalyzer(history_minutes=5)
for rssi_sample in stream:
    bpm, stage, apnea = analyzer.analyze(rssi_sample)
    print(f"BPM: {bpm:.1f}  Stage: {stage}  Apnea: {apnea}")
```

---

## 8. Room Mapping e Localizzazione

Mappa la stanza tramite fingerprinting RSSI e stima la posizione
live con k-NN pesato.

File: [`room_mapper.py`](../room_mapper.py)

### FingerprintMap

Struttura dati che contiene:

- **Metadati stanza**: larghezza, altezza, nome
- **Numero AP**: default 3
- **Punti calibrazione**: lista di `FingerprintPoint` con posizione
  (x, y), vettore RSSI, label opzionale, timestamp

Formato JSON:

```json
{
  "room": {"width": 6.0, "height": 5.0, "name": "Sala"},
  "aps": 3,
  "points": [
    {"x": 1.0, "y": 1.0, "rssi": [-45, -48, -52],
     "label": "angolo_sx", "timestamp": 1234567890.0}
  ]
}
```

### PositionEstimator — Weighted k-NN

Algoritmo:

1. **Distanza**: Euclidean in RSSI-space tra vettore query e ogni
   punto calibrato
2. **Selezione**: k nearest neighbors (default 3)
3. **Peso**: `weight = 1 / (distance + 1e-9)`, bonus ×2 se distance < 1 dBm
4. **Posizione**: media pesata delle coordinate dei k vicini
5. **Confidenza**: `1 - best_distance / 30` (0-1), dove 30 dBm e la
   massima differenza RSSI plausibile in una stanza

### Posizioni AP per visualizzazione 3D

Aggiungi le posizioni 3D degli smartphone (AP) nella stanza per la
visualizzazione Three.js:

```bash
# Configura interattivamente le posizioni degli AP
python3 room_mapper.py setup-aps fingerprint.json
```

Formato nel JSON:
```json
{
  "room": {"width": 8.0, "height": 5.0, "name": "Sala"},
  "room_height_m": 3.0,
  "aps": 3,
  "ap_positions": [
    {"x": 1.0, "y": 2.5, "z": 0.5},
    {"x": 7.0, "y": 2.5, "z": 0.5},
    {"x": 4.0, "y": 2.5, "z": 4.5}
  ],
  "points": [...]
}
```
Coordinate: `x`=orizzontale, `y`=verticale(su), `z`=profondita (metri).
Se non configurate, vengono calcolate posizioni di default lungo la
parete frontale.

### CLI

```bash
# 1. Calibrazione guidata (inserisci RSSI manualmente)
python3 room_mapper.py calibrate fingerprint.json

# 2. Configura posizioni AP (per 3D)
python3 room_mapper.py setup-aps fingerprint.json

# 3. Localizzazione interattiva
python3 room_mapper.py locate fingerprint.json

# 4. Info mappa
python3 room_mapper.py info fingerprint.json
```

### Formato input locate

```
RSSI:-45,-48,-52
```

Dove l'ordine dei valori RSSI corrisponde all'ordine degli AP.

---

## 9. Room Server e Web UI

Bridge live che collega il flusso ESP32 → PositionEstimator →
browser via WebSocket.

### room_server.py

File: [`room_server.py`](../room_server.py)

```
┌─────────┐   USB    ┌────────────────┐   WebSocket   ┌──────────┐
│ ESP32   │──serial──│ room_server.py │───────────────│ Browser  │
│ (CSI)   │          │ PositionTracker│  :8765         │ canvas   │
└─────────┘          │ PositionEst.   │               │ live dot │
                     │ HTTP files     │   HTTP :8080   │          │
                     └────────────────┘               └──────────┘
```

#### PositionTracker

Buffer circolare per AP (default 5 frame ciascuno). Ogni
`estimate_interval` calcola la media RSSI per AP e interroga
`PositionEstimator`.

#### Modalita

| Flag | Descrizione |
|------|-------------|
| `--fingerprint path` | Fingerprint JSON (obbligatorio) |
| `--simulate` | Genera RSSI sintetici (nessun HW) |
| `--ws-port N` | Porta WebSocket (default 8765) |
| `--http-port N` | Porta HTTP file statici (default 8080) |
| `--rate N` | Stime al secondo (default 2 Hz) |
| `--window N` | Frame per AP per media RSSI (default 5) |

#### Fallback

Se `websockets` non e installato, il server scrive
`position.json` su disco (pollabile dal browser).

### room_map.html

File: [`room_map.html`](../room_map.html)

Interfaccia browser con:

- **Canvas HTML5** con stanza scala automatica
- **Griglia 1m** per riferimento spaziale
- **Punti calibrazione** (viola) con etichette
- **Puntino posizione live** (blu) con glow basato su confidenza
- **Tracciato movimento** (linea semitrasparente, attivabile)
- **Info bar**: stanza, coordinate, confidenza, RSSI
- **WebSocket** primario, **polling JSON** come fallback
- **FPS counter**

### Avvio

```bash
# Con ESP32 collegato
python3 room_server.py --fingerprint fingerprint.json

# In simulazione (test)
python3 room_server.py --simulate --fingerprint fingerprint.json

# Browser → http://localhost:8080/room_map.html
```

---

## 10. Visualizzazione 3D (Three.js)

File: [`room_map_3d.html`](../room_map_3d.html)

Versione Three.js della room map con:
- **Stanza 3D** con pavimento, pareti semitrasparenti e griglia 0.5m
- **AP markers** (3 sfere viola graduate con etichette CSS2D)
- **ESP32 receiver** (scatolina verde)
- **Heatmap probabilistica** sul pavimento: mappa di calore a 40×40
  calcolata via kernel gaussiano centrato sulla posizione stimata,
  larghezza inversamente proporzionale alla confidenza
- **Tracciato movimento** (linea semitrasparente)
- **Cursori segnale** tra AP e persona (curve quadratiche)
- **OrbitControls** — trascina per ruotare, scroll per zoom
- **HUD** con posizione, confidenza, label, FPS, stato connessione

### Heatmap

Ad ogni stima posizione, la heatmap viene ricalcolata nel browser:

```
per ogni cella (i,j) della griglia 40×40:
    distanza euclidea dal centro stima
    peso = exp(-d² / (2 · σ²))
    colore = mappa blu→ciano→verde→giallo→rosso

dove σ = σ_min + (1 - confidenza) · (σ_max - σ_min)
```

- Bassa confidenza → gaussiana larga (incertezza alta)
- Alta confidenza → gaussiana stretta (posizione precisa)

### Configurazione posizioni AP

Le posizioni 3D degli AP si configurano con:

```bash
python3 room_mapper.py setup-aps fingerprint.json
```

Oppure modificando direttamente il JSON:
```json
{
  "room_height_m": 3.0,
  "ap_positions": [
    {"x": 1.0, "y": 2.5, "z": 0.5},
    {"x": 7.0, "y": 2.5, "z": 0.5},
    {"x": 4.0, "y": 2.5, "z": 4.5}
  ]
}
```

Coordinate: `x`=orizzontale, `y`=verticale(su), `z`=profondita (metri).
Se non configurate, posizioni di default lungo la parete frontale.

### Avvio

```bash
python3 room_server.py --simulate --fingerprint fingerprint.json
# Browser → http://localhost:8080/room_map_3d.html
```

---

## 11. Confronto con RuView

Tutte le feature implementate sono ispirate al repository
[RuView](https://github.com/ruvnet/RuView) e adattate
all'architettura ESP32 + UNO Q + 3 AP telefonici.

| Feature RuView | Nostra implementazione | Differenze |
|----------------|----------------------|------------|
| Phase sanitization | `phase_sanitizer.py` | Stessa pipeline unwrap+outlier+smooth |
| RSSI feature extraction | `RSSIFeatureExtractor` in `csi_ml.py` | Aggiunto CUSUM, FFT con bande respiratorie |
| Rule-based classifier | `RuleBasedClassifier` in `csi_ml.py` | Ternario (EMPTY/STATIONARY/MOVEMENT) con confidence score |
| Doppler shift | `DopplerShiftExtractor` in `csi_ml.py` | Basato su fase CSI invece che RSSI |
| Sleep quality | `SleepQualityAnalyzer` in `csi_ml.py` | Aggiunta stima stage sonno (AWAKE/REM/LIGHT/DEEP) |
| Multi-AP | Channel hopping firmware + `MultiAPCSIClassifier` | 3 AP telefonici invece di router dedicati |
| Room mapping | `FingerprintMap` + `PositionEstimator` | k-NN pesato invece di approcci ML complessi |
| Live visualization (2D) | `room_server.py` + `room_map.html` | WebSocket + Canvas 2D |
| 3D room visualization | `room_map_3d.html` (Three.js) | Heatmap probabilistica + 3D scene, adattato per 1 ESP32 |

---

## Test

```bash
# Esegue tutti i test (53 totali)
python3 -m pytest test_multi_ap.py test_ruview_features.py test_room_mapper.py -v

# Test specifici
python3 -m pytest test_ruview_features.py -v  # 29 test feature extraction
python3 -m pytest test_multi_ap.py -v          # 7 test multi-AP
python3 -m pytest test_room_mapper.py -v       # 17 test mapping
```

## Dipendenze

```bash
# Minime (tutte le feature base)
pip install numpy

# Feature extraction completa (FFT, CUSUM)
pip install scipy

# ML classifier (opzionale)
pip install scikit-learn joblib

# WebSocket UI
pip install websockets

# Seriale ESP32
pip install pyserial
```
