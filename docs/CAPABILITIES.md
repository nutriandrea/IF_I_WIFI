# Capacità — IF I WIFI

Ogni scheda descrive: funzionamento, HW minimo, comandi, cosa aspettarsi.

---

## 1. Presenza (EMPTY / STILL / MOTION)

**File**: `csi/presence/detector.py` (PresenceDetector) + `csi/quadrants/ws_server.py`

### Come funziona

Per ogni percorso CSI (tx_node → rx_node) calcola la **deviazione standard** di `ampl_mean` su una finestra scorrevole di ~1 s (100 frame a 100 Hz). L'aggregato è il **max** tra tutti i percorsi (più sensibile della media). Una **EMA** riduce lo jitter inter-frame.

Durante la calibrazione (~3 s di stanza vuota) memorizza `baseline_max`. Poi usa una **state machine con hysteresis e dwell time**:

```
aggregato < empty_mult × baseline_max           → EMPTY
empty_mult × baseline_max ≤ aggregato < move_mult × baseline_max  → STILL
aggregato ≥ move_mult × baseline_max            → MOTION
```

Nessun ML, nessun training. Funziona con 1, 2 o 3 ESP32 (degradazione graduale).

### HW minimo

| HW | Risultato |
|----|-----------|
| 1 ESP32 + host | 1 percorso, funziona ma sensibilità ridotta |
| 2 ESP32 + host | 4 percorsi, più robusto |
| 3 ESP32 + host | 9 percorsi, massima sensibilità |

### Comandi

```bash
# Demo con dati simulati (zero HW)
./tools/run_local.sh

# Con ESP32 reali: il ws_server include già PresenceDetector
python3 -m csi.quadrants.ws_server \
  --udp-port 5005 --ws-port 8765 \
  --room 6x5 --rx "0.5,0.5;5.5,0.5;3.0,4.5"
```

### Output WebSocket

```json
{"type":"presence", "state":"EMPTY", "confidence":0.95, "intensity":0.02}
{"type":"presence", "state":"STILL", "confidence":0.70, "intensity":0.15}
{"type":"presence", "state":"MOTION","confidence":0.88, "intensity":0.60}
```

### Cosa aspettarsi

- **EMPTY**: stanza vuota, falsi positivi < 5%
- **STILL**: persona seduta/ferma che respira
- **MOTION**: cammino, gesti ampi
- Dwell time configurabile: ~500 ms per evitare flickering tra EMPTY↔STILL

---

## 2. Tracking 2D (x,y) — No ML

**File**: `csi/quadrants/blob_live.py` (BlobEstimator)

### Come funziona

Per ogni RX, calcola la **varianza** di `ampl_mean` su tutti i percorsi verso quel RX. La varianza è proxy di "quanto il canale è perturbato". La posizione stimata è il **centroide pesato** delle posizioni RX, con peso = varianza residuale (rispetto al baseline di stanza vuota).

Più un RX è vicino alla persona, più il suo canale è perturbato → più peso ha nel centroide.

L'incertezza (`x_std`, `y_std`) è lo **spread pesato** delle distanze dalle posizioni RX. Con 3 RX in triangolo si ha buona copertura.

### HW minimo

| HW | Percorsi | Accuracy |
|----|----------|----------|
| 2 ESP32 | 4 | ~1-1.5 m in stanza 6×5 m |
| 3 ESP32 | 9 | ~0.5-1.5 m |

### Comandi

```bash
# ws_server con BlobEstimator (default, auto mode)
python3 -m csi.quadrants.ws_server \
  --udp-port 5005 --ws-port 8765 \
  --room 6x5 --grid 4x4 \
  --rx "0.5,0.5;5.5,0.5;3.0,4.5"
```

### Output WebSocket

```json
{"type":"position", "x":2.3, "y":3.1, "x_std":0.6, "y_std":0.5,
 "intensity":0.35, "confidence":0.72, "n_active_rx":3}
{"type":"cells", "rows":4, "cols":4, "probas":{"r2c1":0.45,"r2c2":0.30,...}}
```

### Cosa aspettarsi

- Posizione stimata: 0.5-1.5 m dalla posizione reale
- Ambiguità al centro geometrico dei RX
- Se la varianza globale è sotto soglia, non emette stima (stanza vuota)
- Funziona subito, nessun training

---

## 3. Tracking 2D ML (x,y) — RandomForest + Kalman

**File**: `csi/quadrants/regressor.py` (PositionRegressor)

### Come funziona

**Fase 1 — Training**: raccogli frame CSI in posizioni note (griglia r0c0, r0c1, ...). Il RandomForestRegressor impara a mappare feature CSI → coordinate (x,y) continue in [0,1]².

**Fase 2 — LOO-CV Gate**: Leave-One-Cell-Out cross-validation. Se l'errore mediano su una cella esclusa è > soglia, il modello è **rifiutato** e il sistema ricade automaticamente su BlobEstimator (no ML). Questo previene l'overfitting spaziale.

**Fase 3 — Inferenza**: KalmanFilter2D costant-velocity per smoothing temporale. Output: (x, y) smooth + incertezza.

### HW minimo

| HW | Dati necessari |
|----|----------------|
| 3 ESP32 | Training set: ~60 frame per cella griglia |

### Comandi

```bash
# 1. Training
python3 -m csi.blob_cli --train --udp-port 5005
# (segui le istruzioni: posizionati in ogni cella, premi Invio)

# 2. Monitor (auto-detect: usa ML se modello validato, altrimenti BlobEstimator)
python3 -m csi.quadrants.ws_server \
  --udp-port 5005 --ws-port 8765 \
  --room 6x5 --grid 4x4 \
  --rx "0.5,0.5;5.5,0.5;3.0,4.5"

# Forza modalità
python3 -m csi.quadrants.ws_server --quadrants-mode regressor ...
python3 -m csi.quadrants.ws_server --quadrants-mode blob_live ...
```

### Output WebSocket

```json
{"type":"position_ml", "x":0.35, "y":0.72, "x_std":0.04, "y_std":0.05,
 "smoothed":true, "confidence":0.91}
```

### Cosa aspettarsi

- Con training buono: accuracy migliore del BlobEstimator (~0.3-0.8 m)
- Con dati scarsi: il LOO-CV gate rifiuta il modello e usa BlobEstimator
- Richiede ~5 minuti di raccolta dati per stanza
- Overfitting rilevato automaticamente

---

## 4. Altezza 3 classi — HeightHeuristic

**File**: `csi/blob3d/tracker.py` (HeightHeuristic)

### Come funziona

Confronta l'energia nelle **subcarrier basse** (indice 0-31) vs **alte** (32-63) dello spettro CSI.

Le subcarrier a frequenza più alta sono leggermente più sensibili a scattering da torso/testa (parti alte del corpo). Il rapporto `varianza_sub_alte / varianza_sub_basse` correla con la posizione verticale della massa corporea. Con dwell hysteresis per evitare jitter.

Restituisce 3 classi (NON cm):

| Classe | Tipicamente | Come |
|--------|-------------|------|
| LOW | a terra / sdraiato | Sub basse dominano |
| MID | seduto | Bilanciato |
| HIGH | in piedi | Sub alte dominano |

### HW minimo

| HW | Precisione |
|----|------------|
| 3 ESP32 stessa quota | 3 classi discrete |
| 3+ ESP32 a quote diverse | Potrebbe dare più risoluzione (non testato) |

### Comandi

Incluso automaticamente in ws_server con `--enable-3d`:

```bash
python3 -m csi.quadrants.ws_server --enable-3d --room-height 3.0 ...
```

### Output

```json
{"type":"blob3d", "x":2.3, "y":3.1, "z":1.7, "z_class":"HIGH",
 "x_std":0.6, "y_std":0.5, "z_std":0.3, "confidence":0.72}
```

### Cosa aspettarsi

- **NON** dà l'altezza in centimetri
- Solo "sdraiato / seduto / in piedi"
- Transizioni lente (dwell ~1 s per evitare flickering)
- Affidabile solo con 3 ESP32 attivi

---

## 5. Respiro BPM

**File**: `csi/csi_ml.py` (PhaseBreathingEstimator) + signal processing in `csi/phase_sanitizer.py`

### Come funziona

1. Sanitizza la fase CSI (unwrap → outlier → smoothing)
2. Bandpass **0.1-0.5 Hz** (6-30 respiri/minuto)
3. Zero-crossing counting sulla componente filtrata
4. Media mobile su finestra di ~10 s

Il movimento toracico modula la fase del segnale CSI. Il respiro è il segnale biologico più facile da estrarre perché ha ampiezza e periodo stabili.

### HW minimo

| HW | Qualità |
|----|---------|
| 1 ESP32 | ±2-3 BPM, più rumoroso |
| 3 ESP32 | ±1-2 BPM |

### Comandi

```bash
# Training: durante calibrazione respiro (persona ferma, respira normalmente)
# Poi il ws_server pubblica BPM automaticamente

# Verifica offline su recording
python3 -m csi.csi_record --replay capture.txt --rate 100
```

### Output WebSocket

```json
{"type":"vitals", "breathing_bpm":16.2, "breathing_confidence":0.85,
 "heart_rate_bpm":72, "heart_rate_confidence":0.40}
```

### Cosa aspettarsi

- **±1-2 BPM** con persona ferma
- Fallisce se la persona parla, tossisce, o si muove
- ~10 s di stabilizzazione iniziale
- **Non è un dispositivo medico**

---

## 6. Battito cardiaco — Indicativo

**File**: `csi/csi_ml.py` (PhaseBreathingEstimator) + bandpass filtering

### Come funziona

Bandpass **0.8-2.0 Hz** (48-120 bpm) sulla fase CSI. Il segnale è molto più debole del respiro (l'escursione toracica del cuore è ~mm vs cm del respiro).

Richiede:
- Persona **perfettamente ferma**
- 3 ESP32 per multipath diversity (miglior SNR)
- Filtraggio aggressivo (wavelet o adaptive notch)

### HW minimo

| HW | Qualità |
|----|---------|
| 3 ESP32 | ±5-10 bpm, indicative |
| 1-2 ESP32 | Generalmente non affidabile |

### Cosa aspettarsi

- **±5-10 bpm** accuratezza
- Funziona solo a riposo, persona ferma
- NON diagnostico per aritmie o problemi cardiaci
- Se il respiro è forte (>20 bpm), le armoniche possono confondersi col battito

---

## 7. Caduta — Fall Detection

**Componenti**: `csi/presence/detector.py` + logica accelerazione di fase

### Come funziona

Una caduta produce un **transiente rapido** nella varianza di fase: in ~200-400 ms la varianza sale bruscamente (movimento) e poi si stabilizza su un nuovo livello (persona a terra ferma). Il detector cerca pattern:

1. `MOTION` con intensità > soglia_fall per < 500 ms
2. Seguito da `STILL`/`EMPTY` anomalo (varianza più bassa del normale perché a terra)

### HW minimo

3 ESP32 (un solo ESP32 non dà abbastanza percorsi per distinguere caduta da movimento normale)

### Cosa aspettarsi

- Plausibile ma **non ancora validato** su dati reali
- Falsi positivi: oggetti che cadono, sedie spostate bruscamente
- Falsi negativi: cadute lente (es. appoggiarsi al muro e scivolare)
- Soglie da calibrare per ambiente

---

## 8. Visualizzazione Browser

**File**: `mapping/ui.html` + `csi/quadrants/ws_server.py`

### Come funziona

`ws_server.py` legge i frame UDP dagli ESP32, esegue PresenceDetector + BlobEstimator, e pubblica i risultati via **WebSocket** su `ws://localhost:8765`. `ui.html` si connette e renderizza:

- **Radar 3D** (Three.js): scena sonar-style con blob continuo
- **Heatmap grid**: probabilità per cella della griglia
- **Stato presenza**: EMPTY / STILL / MOTION
- **Vital signs**: BPM respiro, battito
- **Diagnostica**: rate Hz, percorsi attivi, calibrazione

### HW minimo

| Modalità | HW |
|----------|----|
| Dati simulati | Nessuno (`./tools/run_local.sh`) |
| Dati reali | 3 ESP32 + ws_server |

### Comandi

```bash
# Demo completa con simulazione (zero HW)
./tools/run_local.sh

# Solo server (aspettando ESP32 reali)
python3 -m csi.quadrants.ws_server \
  --udp-port 5005 --ws-port 8765 \
  --room 6x5 --grid 4x4 --enable-3d --room-height 3.0 \
  --rx "0.5,0.5;5.5,0.5;3.0,4.5"

# Poi apri nel browser:
# file:///.../mapping/ui.html?ws=ws://localhost:8765&room=6x5&grid=4x4
```

### Cosa aspettarsi

- ~10 Hz di aggiornamento
- Radar 3D opzionale (`--enable-3d`)
- Connessione automatica a `window.location` o parametro `?ws=`
- Se nessun dato per > 3 s, mostra "disconnected"

---

## 9. Record & Replay

**File**: `csi/csi_record.py`

### Come funziona

**Record**: legge il flusso CSI (da seriale UDP) e salva su file di testo con timestamp. Supporta auto-rotate ogni N secondi.

**Replay**: rilegge il file e produce lo stesso output a velocità controllata, utile per sviluppo offline, debug, e validazione algoritmi.

### HW minimo

Nessuno per replay. Per recording: 1+ ESP32.

### Comandi

```bash
# Record 30 secondi
python3 -m csi.csi_record --record --seconds 30

# Record con auto-rotate ogni 60 s
python3 -m csi.csi_record --record --rotate 60

# Replay a velocità originale
python3 -m csi.csi_record --replay csi_capture.txt

# Replay a 100 Hz (per test)
python3 -m csi.csi_record --replay csi_capture.txt --rate 100

# Replay via WebSocket (come se fossero ESP32 live)
python3 -m csi.csi_record --replay capture.txt --websocket :8765
```

### Cosa aspettarsi

- File di testo ~1 MB/minuto con 3 ESP32 a 100 Hz
- Replay fedele: stessi frame, stessi timestamp relativi
- Utile per creare dataset di training o debug

---

## 10. Firmware ESP32 — Cross-Ping 100 Hz

**File**: `firmware/esp32_radar3d/esp32_radar3d.ino`

### Come funziona

Ogni ESP32:
1. Si connette al WiFi (2.4 GHz, canale 6)
2. Trasmette frame **802.11 broadcast** a 100 Hz
3. Riceve i broadcast degli altri ESP32 via **callback CSI** (`esp_wifi`)
4. Impacchetta: magic `0xC5110003` + TX/RX node ID + RSSI + subcarrier I/Q + timestamp μs
5. Invia UDP al PC centrale

3 nodi × 3 percorsi = **9 percorsi CSI unici** con MAC fissi (no randomization).

### Configurazione

1. Copia `secrets.h.example` → `secrets.h` con SSID/password
2. In `network_config.h`:
   - `NODE_ID`: 0, 1, o 2 (diverso per ogni ESP32)
   - `NODE_MACS[3][6]`: MAC dei 3 ESP32
   - `UDP_TARGET_IP`: IP del PC centrale

### Comandi

```bash
# Scoprire MAC delle 3 schede
python3 tools/discover_macs.py

# Verifica connessione
# Collega un ESP32 via USB, apri serial monitor a 115200 baud
# Dovresti vedere: "ESP32 Radar 3D — NODE X" + timestamp

# Il PC centrale deve fare:
python3 -m csi.quadrants.ws_server --udp-port 5005 --ws-port 8765 ...
```

### Cosa aspettarsi

- **100 Hz** per nodo → 300 frame/s totali con 3 nodi
- Latenza UDP: < 5 ms in rete locale
- Canale fisso 6 (cambiabile nel firmware)
- Perdita pacchetti: ~1-5% in WiFi congestionato
- 9 percorsi (3 TX × 3 RX) stabili
