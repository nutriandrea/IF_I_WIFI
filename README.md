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
| MCU | ESP32-S3 (240 MHz dual-core) |
| RAM | 512 KB SRAM + 4 MB PSRAM |
| Storage | 16 MB Flash |
| WiFi | 2.4 GHz 802.11 b/g/n (integrated) |
| BLE | Bluetooth 5.0 LE |
| GPIO | 14 digitali, 8 analogici |
| Interfaccia | USB-C (programmazione + seriale) |
| Alimentazione | 5V USB o 7-12V Vin |

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

```python
import subprocess, time

def get_rssi() -> float:
    result = subprocess.check_output(
        "iw dev wlan0 link | grep signal", shell=True
    ).decode()
    val = float(result.split("signal:")[1].split("dBm")[0])
    return val
```

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

```python
def detect_presence(features: dict) -> bool:
    return features["rssi_std"] > 2.0 or features["rssi_delta"] > 5

def detect_absence(features: dict) -> bool:
    return features["rssi_std"] < 0.5 and features["rssi_delta"] < 1.5
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

```python
import serial

ser = serial.Serial('/dev/ttyACM0', 9600, timeout=1)

def read_sensors() -> tuple[float, float, int, int]:
    line = ser.readline().decode().strip()
    if not line:
        return None
    t, h, a, l = map(float, line.split(","))
    return t, h, int(a), int(l)
```

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
# Carica sketch Arduino
# Collega UNO Q via USB-C
# Apri Arduino IDE, seleziona "Arduino UNO Q", carica sketch

# Dipendenze Python
pip install numpy pyserial flask
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
