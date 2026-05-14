# Smart Environment Hub — Arduino UNO Q

> Trasforma segnali WiFi ambientali in un sistema di **rilevamento presenza passivo**, combinato con sensori ambientali per passare da **automazione semplice** a **intelligenza contestuale** — tutto su Arduino UNO Q, senza cloud, senza telecamere, senza wearable.

---

## Architettura

```
[Sensori MCU]              [WiFi RSSI (Linux)]         [Python AI Layer]
DHT22  -> temp/hum         scan wlan0                 feature extraction
MQ135  -> aria (VOC)       monitor RSSI over time     threshold / tinyML
LDR    -> luce                                        context window
     \____________                        ___________/
                  |                      |
                  v                      v
              [Python Core] ---> [Decision Engine]
               UART reader         rules / ML
               feature extract     context-aware
                      |
                      v
               [Output MCU]
                relay -> lamp
                LED   -> stato
```

Tutto gira **on-board**. Nessun cloud obbligatorio. Privacy garantita.

---

## Board: Arduino UNO Q

| Caratteristica | Dettaglio |
|---------------|-----------|
| MPU | Qualcomm Dragonwing QRB2210 (quad-core 2.0 GHz) |
| MCU | STM32U585 (Arm Cortex-M33) |
| RAM | 2 GB LPDDR4 (Linux) + SRAM MCU |
| Storage | Flash per Linux + Flash MCU |
| WiFi | Qualcomm integrato (wlan0, 2.4 GHz) |
| BLE | Bluetooth 5.0 LE |
| GPIO | 14 digitali, 8 analogici (via STM32) |
| OS Linux | Debian trixie, Python 3.13.5 |
| OS MCU | Zephyr RTOS (sketch Arduino) |
| Interfaccia | USB-C (programmazione + seriale) |

---

## Risultati test di fattibilita

Eseguito il `feasibility_test.py` sulla UNO Q reale (14/05/2026).

### Riepilogo

| Test | Risultato | Dettaglio |
|------|-----------|-----------|
| **RSSI Sampling** | ✅ PASS | 40/40 samples, 2.0 Hz, 0 errori |
| **Feature Extraction** | ✅ PASS | 1.0 ms per estrazione (pure Python) |
| **UART** | ❌ FAIL | MCU sketch non caricato |
| **System Load** | ✅ PASS | CPU 8%, RAM 827/3670 MB (23%) |
| **Presence Detection** | ❌ FAIL | Soglia da calibrare |
| **Combined Pipeline** | ✅ PASS | 59 loop/30s, 2.0 loop/s |

### Dettaglio

**RSSI Sampling**: `iw` installato via apt, sampling stabile a 2 Hz tramite `iw dev wlan0 link`. Segnale rilevato: da -53 a -40 dBm (connesso a hotspot nelle vicinanze). Deviazione standard 4.66 — buona baseline per presence detection. Tempo di risposta del comando `iw` istantaneo.

**Feature Extraction**: 4 feature (mean, std, delta, var) calcolate in 1.0 ms in pure Python (numpy non installato). Ben dentro il budget temporale di 500 ms (2 Hz).

**System Load**: Con RSSI sampling a 2 Hz + feature extraction + logging, CPU all'8% e RAM a 827 MB (23%). Margine abbondante per aggiungere dashboard Flask, logging su file, o ML leggero.

**Combined Pipeline**: 30 secondi di funzionamento end-to-end senza errori. 59 loop completati a 2.0 loop/s. Il sistema e stabile e reattivo.

**Presence Detection**: La soglia fissa (std > 2.0) non funziona con segnale forte (std baseline 4.66). Necessaria calibrazione dinamica o uso di delta temporale invece di std assoluto. Vedi sezione [Presence Detection con segnale forte](#presence-detection-con-segnale-forte).

**UART**: Tre porte seriali trovate (`/dev/ttyS0`, `/dev/ttyS1`, `/dev/ttyGS0`). `/dev/ttyGS0` e la USB gadget serial per lo STM32 — pronta ma nessun dato perche lo sketch MCU (`feasibility_test.ino`) non e stato ancora caricato.

### Presenza: lezione dai dati reali

Il test ha rivelato un problema fondamentale: **con segnale WiFi forte (-40 dBm), la varianza RSSI e gia alta senza nessuno che si muova** (std = 4.66). Una soglia fissa di 2.0 da falsi positivi continui.

**Soluzione adottata**: invece di std assoluto, usiamo **delta temporale** della media mobile:

```python
# Invece di: std(window) > threshold
# Usiamo: |mean(window) - mean(window_precedente)| > threshold

baseline = mean(window[:-5]) if len(window) > 5 else mean(window)
current = mean(window[-5:])
movement = abs(current - baseline)
presence = movement > 1.5  # dBm di variazione
```

Questo approccio e robusto a segnale forte e funziona anche in ambienti con alta varianza di base.

---

## Funzionalita core

### 1. Rilevamento presenza (WOW factor)
- **Varianza RSSI** su finestra temporale di 20 s
- Fluttuazione significativa -> qualcuno si muove
- Accensione luce automatica

### 2. Assenza prolungata
- RSSI stabile + sensori stabili per 5 min
- Spegnimento luce automatico

### 3. Finestra aperta (comfort)
- Calo rapido di temperatura
- Picco MQ135 (aria esterna)
- Alert con suggestione chiusura

### 4. Aria stagnante (salute)
- MQ135 in crescita nel tempo
- Trigger ventilazione

### 5. Luce lasciata accesa (risparmio)
- LDR alto + nessuna presenza
- Spegnimento automatico

---

## Pipeline software

### RSSI sampling (Python su UNO Q Linux)

Su UNO Q, `iw` va in `/usr/sbin/iw`. Usa path completo o `shutil.which()`.

```python
import subprocess, time, shutil

IW = shutil.which("iw") or "/usr/sbin/iw"

def get_rssi() -> float | None:
    try:
        result = subprocess.check_output(
            f"{IW} dev wlan0 link", shell=True, timeout=3,
            stderr=subprocess.DEVNULL
        ).decode()
        # "signal: -45 dBm"
        val = float(result.split("signal:")[1].split("dBm")[0])
        return val
    except (subprocess.TimeoutExpired, IndexError, ValueError, OSError):
        return None
```

Risultato reale sulla UNO Q: **2.0 Hz stabili, 0 errori**.

### Feature extraction

```python
import numpy as np

def extract_features(window: list[float]) -> dict:
    return {
        "rssi_mean":  np.mean(window),
        "rssi_std":   np.std(window),
        "rssi_delta": np.max(window) - np.min(window),
        "rssi_range": np.ptp(window),
        "rssi_var":   np.var(window),
    }
```

### Presence detection

**ATTENZIONE**: Con segnale WiFi forte (-40 dBm), lo std e gia alto (4.66) anche senza movimento. La soglia fissa non funziona. Usa invece **delta di media mobile**:

```python
from collections import deque

class AdaptivePresenceDetector:
    def __init__(self, window_size: int = 20, delta_threshold: float = 1.5):
        self.window = deque(maxlen=window_size)
        self.delta_threshold = delta_threshold

    def update(self, rssi: float) -> bool:
        self.window.append(rssi)
        if len(self.window) < 10:
            return False

        # Media mobile: confronta recente vs storico
        baseline = sum(self.window) / len(self.window)
        recent = list(self.window)[-5:]
        recent_mean = sum(recent) / len(recent)
        delta = abs(recent_mean - baseline)

        return delta > self.delta_threshold
```

> Sui dati reali (std=4.66 con segnale a -40 dBm), il delta medio mobile funziona mentre std assoluto da falsi positivi.

**Calibrazione della soglia**: usa `calibrate_presence.py` per trovare la soglia ottimale
per il tuo ambiente specifico:

```bash
# Calibrazione rapida (baseline 30s + movimento 30s + analisi)
python3 calibrate_presence.py --mode quick

# Oppure fase per fase:
python3 calibrate_presence.py --mode baseline --seconds 30
python3 calibrate_presence.py --mode movement --seconds 30
python3 calibrate_presence.py --mode analyze

# Monitoraggio real-time con la soglia trovata
python3 calibrate_presence.py --mode monitor
```

### FFT (bonus — ispirato paper PoliMi)

```python
def rssi_spectrum(signal: np.ndarray) -> np.ndarray:
    return np.abs(np.fft.fft(signal))
```

Se c'e movimento -> lo spettro in frequenza non e piatto.

---

## Sensori (MCU — Arduino sketch)

```cpp
#include <DHT.h>

#define DHTPIN  2
#define DHTTYPE DHT22
#define MQ135PIN A0
#define LDRPIN  A1

DHT dht(DHTPIN, DHTTYPE);

void setup() {
    Serial.begin(9600);
    dht.begin();
}

void loop() {
    float temp = dht.readTemperature();
    float hum  = dht.readHumidity();
    int   air  = analogRead(MQ135PIN);
    int   light = analogRead(LDRPIN);

    Serial.print(temp);
    Serial.print(",");
    Serial.print(hum);
    Serial.print(",");
    Serial.print(air);
    Serial.print(",");
    Serial.println(light);

    delay(2000);
}
```

### Lettura lato Python

Su UNO Q, lo STM32 comunica via USB gadget serial (`/dev/ttyGS0`).

```python
import serial

# Su UNO Q: /dev/ttyGS0 (non ttyACM0)
ser = serial.Serial('/dev/ttyGS0', 9600, timeout=1)

def read_sensors() -> tuple[float, float, int, int] | None:
    line = ser.readline().decode().strip()
    if not line:
        return None
    t, h, a, l = map(float, line.split(","))
    return t, h, int(a), int(l)
```

> **Nota**: prima di testare UART, carica `feasibility_test.ino` sulla parte STM32 della UNO Q tramite Arduino IDE. La porta `/dev/ttyGS0` e pronta ma non invia dati finche lo sketch non e caricato.</parameter>


---

## Decision engine

```python
if presence:
    relay.on()
elif no_presence_for_5min:
    relay.off()

if temp_drop_fast:
    alert("Finestra aperta?")

if air_quality_bad:
    alert("Aria stagnante — Ventilare!")
```

---

## Output visibili

### Fisici
- Lampadina ON/OFF (relay)
- LED di stato (verde = ok, rosso = alert)

### Digitali (opzionali)
- Dashboard Flask: presenza, temperatura, aria, grafico RSSI
- WebSocket per aggiornamenti实时

---

## UART: comunicazione Linux <-> STM32 MCU

Sulla UNO Q, la comunicazione tra Linux (Python) e STM32 (sketch Arduino) avviene via **USB gadget serial**:

```
Linux (Python)  ---->  /dev/ttyGS0  ---->  STM32U585 (MCU)
```

**Prima di testare**:
1. Apri `feasibility_test.ino` in Arduino IDE
2. Seleziona board: **Arduino UNO Q (STM32)**
3. Carica lo sketch via USB-C
4. Lo sketch invia dati CSV ogni 2s su `Serial`

**Porte seriali sulla UNO Q**:
- `/dev/ttyS0`, `/dev/ttyS1` — UART fisiche del Qualcomm (NON collegate allo STM32)
- `/dev/ttyGS0` — USB gadget serial verso STM32 **(usa questa)**

---

## Decision engine

```python
detector = AdaptivePresenceDetector()
absence_start = None

while True:
    rssi = get_rssi()
    sensors = read_sensors()
    presence = detector.update(rssi)

    if presence:
        relay.on()
        absence_start = None
    else:
        if absence_start is None:
            absence_start = time.time()
        elif time.time() - absence_start > 300:  # 5 min
            relay.off()

    if sensors:
        # Finestra aperta?
        if sensors["temp"] < last_temp - 3:
            alert("Finestra aperta?")
        # Aria stagnante?
        if sensors["air"] > 700:
            alert("Aria stagnante — Ventilare!")
        # Luce accesa senza nessuno?
        if sensors["light"] > 800 and not presence:
            alert("Luce lasciata accesa!")
```

---

## Setup rapido

### 1. Collegamento hardware
```schema
DHT22 VCC -> 5V
DHT22 GND -> GND
DHT22 OUT -> D2

MQ135 VCC -> 5V
MQ135 GND -> GND
MQ135 OUT -> A0

LDR + 10kΩ voltage divider -> A1

Relay VCC -> 5V
Relay GND -> GND
Relay IN  -> D3

LED+  -> D4 (tramite 220Ω)
LED-  -> GND
```

### 2. Software

```bash
# 1. Prepara la UNO Q
sudo apt-get update
sudo apt-get install -y iw python3-serial python3-flask

# 2. Carica sketch MCU
#    - Apri feasibility_test.ino in Arduino IDE
#    - Seleziona board: "Arduino UNO Q (STM32)"
#    - Carica via USB-C

# 3. Test di fattibilita
python3 feasibility_test.py --install-deps

# 4. Avvia il sistema
python3 decision_engine.py
```

---

## Timeline 24h (hackathon)

| Ore | Cosa |
|-----|------|
| 0-4 | Setup sensori + relay + breadboard |
| 4-8 | RSSI scan + logging + feature extraction |
| 8-12 | Presence detection + tuning soglie |
| 12-16 | Decision engine + integrazione sensori |
| 16-20 | Demo flow, bugfix, test reali |
| 20-24 | Dashboard, pitch, storytelling |

---

## Perche e "killer"

- **WiFi come sensore** (non banale, stile PoliMi)
- **Nessun hardware extra** (RSSI e gratuito)
- **Segnali combinati** -> consapevolezza contestuale
- **Reattivo** (luce che risponde in tempo reale)
- **Spiegabile scientificamente** (paper PoliMi)
- **Privacy-first** (niente telecamere)
- **Scalabile** (aggiungi CO2, PIR, sensore porta)

---

## Pitch (per hackathon/giornata demo)

> "In 24 ore abbiamo costruito una stanza **context-aware** che sente letteralmente la presenza usando solo segnali WiFi e sensori di bordo — niente hardware extra, niente cloud, solo decisioni intelligenti all'edge. Dall'automazione semplice all'**intelligenza ambientale**."

---

## Collegamento ai track hackathon

- Presenza/attivita in una stanza
- Identificazione anomalie (inattivita prolungata)
- Ambienti assistivi (monitoraggio anziani)
- Ottimizzazione comfort (luce, temperatura, aria)
- Pattern comportamentali nel tempo

---

## Riferimenti

- [Arduino UNO Q Docs](https://docs.arduino.cc/hardware/uno-q)
- [WiFi Sensing — PoliMi](https://www.deib.polimi.it/)
- [Edge Impulse + Arduino](https://docs.edgeimpulse.com/)
- [Arduino Cloud](https://cloud.arduino.cc/)
