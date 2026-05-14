# Smart Environment Hub — Piano Demo 24h

> Sistema che **rileva presenza passivamente** via WiFi (dinamiche RSSI) + sensori ambientali, costruisce **contesto nel tempo**, e **reagisce intelligentemente** — tutto on-board su Arduino UNO Q.

---

## Architettura

```
[Sensori MCU]      [WiFi RSSI (Linux)]      [Python AI]
DHT22 MQ135 LDR    scan wlan0               feature extraction
                   monitor RSSI over time   soglie / tinyML
                      |                          |
                      v                          v
               [Python Core]             [Decision Engine]
                UART / feature            regole + contesto
                      |
                      v
                 [Output MCU]
                  relay -> lamp
                  LED   -> stato
```

---

## Fase 1 — Setup hardware (0-4 h)

### Obiettivo
Breadboard cablata con tutti i sensori funzionanti.

### Checklist

- [ ] DHT22 collegato (VCC->5V, GND->GND, OUT->D2)
- [ ] MQ135 collegato (VCC->5V, GND->GND, OUT->A0)
- [ ] LDR + partitore 10kΩ collegato (OUT->A1)
- [ ] Relay module collegato (VCC->5V, GND->GND, IN->D3)
- [ ] LED + 220Ω collegato (ANODO->D4, CATODO->GND)
- [ ] Breadboard alimentata (5V e GND dai binari)
- [ ] Arduino UNO Q collegato via USB-C
- [ ] Verifica: Serial Monitor mostra letture sensori

### Codice Arduino (upload subito)

```cpp
#include <DHT.h>

#define DHTPIN  2
#define DHTTYPE DHT22
#define MQ135PIN A0
#define LDRPIN  A1
#define RELAYPIN 3
#define LEDPIN   4

DHT dht(DHTPIN, DHTTYPE);

void setup() {
    Serial.begin(9600);
    dht.begin();
    pinMode(RELAYPIN, OUTPUT);
    pinMode(LEDPIN, OUTPUT);
    digitalWrite(RELAYPIN, LOW);
    digitalWrite(LEDPIN, LOW);
}

void loop() {
    float t = dht.readTemperature();
    float h = dht.readHumidity();
    int air_q = analogRead(MQ135PIN);
    int light = analogRead(LDRPIN);

    Serial.print(t);
    Serial.print(",");
    Serial.print(h);
    Serial.print(",");
    Serial.print(air_q);
    Serial.print(",");
    Serial.println(light);

    delay(2000);
}
```

### Verifica successo
```
Serial output example:
25.3,60.2,420,850
24.9,61.0,430,832
```

---

## Fase 2 — WiFi RSSI + logging (4-8 h)

### Obiettivo
Script Python che campiona RSSI del WiFi e salva logs.

### Checklist

- [ ] Test `iw dev wlan0 link` via SSH/su UNO Q
- [ ] Script RSSI sampling funzionante
- [ ] Log su file CSV (timestamp, rssi)
- [ ] Acquisizione: 5 minuti di dati in stanza vuota
- [ ] Acquisizione: 5 minuti con movimento

### Codice Python — RSSI sampler

```python
#!/usr/bin/env python3
import subprocess, time, csv, sys
from datetime import datetime

def get_rssi() -> float | None:
    try:
        result = subprocess.check_output(
            "iw dev wlan0 link | grep signal",
            shell=True, timeout=5
        ).decode()
        val = float(result.split("signal:")[1].split("dBm")[0])
        return val
    except (subprocess.TimeoutExpired, IndexError, ValueError):
        return None

def log_rssi(csv_writer, duration_min: int = 5):
    start = time.time()
    end = start + duration_min * 60

    while time.time() < end:
        rssi = get_rssi()
        if rssi is not None:
            ts = datetime.now().isoformat()
            csv_writer.writerow([ts, rssi])
            print(f"{ts},{rssi:.1f}dBm")
        time.sleep(0.5)

if __name__ == "__main__":
    filename = f"rssi_log_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "rssi_dbm"])
        print(f"Logging to {filename}")
        log_rssi(writer, duration_min=int(sys.argv[1]) if len(sys.argv) > 1 else 5)
    print(f"Saved: {filename}")
```

### Verifica successo
Il file CSV contiene almeno 500 campioni. La varianza e > 2.0 in presenza di movimento.

---

## Fase 3 — Feature extraction + presence (8-12 h)

### Obiettivo
Rilevare presenza in tempo reale dalle feature RSSI.

### Checklist

- [ ] Feature extraction (mean, std, delta, var)
- [ ] Soglie calibrate sulla stanza di test
- [ ] Presenza rilevata correttamente (>80% accuratezza)
- [ ] Falso positivo < 10% in stanza vuota
- [ ] Integrazione con lettura seriale sensori

### Codice — Feature extractor + detector

```python
#!/usr/bin/env python3
import numpy as np
import time
from collections import deque

WINDOW_SIZE = 40     # 20s a 0.5Hz
STD_THRESHOLD = 2.0
DELTA_THRESHOLD = 5.0

class PresenceDetector:
    def __init__(self, window_size=WINDOW_SIZE):
        self.window = deque(maxlen=window_size)

    def update(self, rssi: float) -> dict:
        self.window.append(rssi)
        return self.extract_features()

    def extract_features(self) -> dict | None:
        if len(self.window) < 10:  # need minimum samples
            return None
        arr = np.array(self.window)
        return {
            "mean":  float(np.mean(arr)),
            "std":   float(np.std(arr)),
            "delta": float(np.max(arr) - np.min(arr)),
            "var":   float(np.var(arr)),
        }

    def detect(self, features: dict | None) -> bool:
        if features is None:
            return False
        return features["std"] > STD_THRESHOLD or features["delta"] > DELTA_THRESHOLD

# Usage
detector = PresenceDetector()
while True:
    rssi = get_rssi()
    if rssi is not None:
        feats = detector.update(rssi)
        presence = detector.detect(feats)
        if feats:
            print(f"RSSI={rssi:.1f} std={feats['std']:.2f} presence={presence}")
    time.sleep(0.5)
```

### Calibrazione soglie

| Ambiente | STD tipico vuoto | STD tipico occupato | Soglia consigliata |
|----------|-----------------|---------------------|--------------------|
| Ufficio/studio | 0.3-1.0 | 2.5-6.0 | > 2.0 |
| Salotto | 0.5-1.5 | 3.0-8.0 | > 2.5 |
| Stanza piccola | 0.2-0.8 | 2.0-5.0 | > 1.8 |

Regola le soglie se necessario (modifica STD_THRESHOLD e DELTA_THRESHOLD).

---

## Fase 4 — Decision engine (12-16 h)

### Obiettivo
Unire presenza + sensori in un motore decisionale completo.

### Checklist

- [ ] Presenza -> relay ON
- [ ] Assenza 5 min -> relay OFF
- [ ] Calo temp rapido -> alert finestra
- [ ] MQ135 crescente -> alert aria
- [ ] LDR alto + no presenza -> auto shutoff luce
- [ ] LED verde = ok, rosso = alert

### Codice — Decision engine

```python
#!/usr/bin/env python3
import serial, time
from collections import deque

# Config
SERIAL_PORT = "/dev/ttyACM0"
BAUD = 9600
ABSENCE_TIMEOUT = 300  # 5 min
TEMP_DROP_THRESHOLD = 3.0  # °C drop fast
AIR_WINDOW_SIZE = 30       # 1 min di campioni

class DecisionEngine:
    def __init__(self):
        self.ser = serial.Serial(SERIAL_PORT, BAUD, timeout=1)
        self.relay_on = False
        self.last_presence_time = time.time()
        self.temp_history = deque(maxlen=5)
        self.air_history = deque(maxlen=AIR_WINDOW_SIZE)
        self.presence = False

    def read_sensors(self):
        try:
            line = self.ser.readline().decode().strip()
            if not line:
                return None
            t, h, a, l = map(float, line.split(","))
            self.temp_history.append(t)
            self.air_history.append(a)
            return {"temp": t, "hum": h, "air": int(a), "light": int(l)}
        except (ValueError, serial.SerialException):
            return None

    def decide(self, presence: bool, sensors: dict):
        self.presence = presence

        # 1. Presenza -> relay ON
        if presence:
            self.relay_on = True
            self.last_presence_time = time.time()

        # 2. Assenza prolungata -> relay OFF
        elif time.time() - self.last_presence_time > ABSENCE_TIMEOUT:
            self.relay_on = False

        # 3. Finestra aperta?
        if len(self.temp_history) >= 3:
            temp_drop = self.temp_history[-1] - self.temp_history[0]
            if temp_drop < -TEMP_DROP_THRESHOLD:
                print("[ALERT] Finestra aperta? Calo temp di {:.1f}C".format(temp_drop))

        # 4. Aria stagnante?
        if len(self.air_history) >= AIR_WINDOW_SIZE:
            air_trend = self.air_history[-1] - self.air_history[0]
            if air_trend > 100:
                print("[ALERT] Aria stagnante — Ventilare! (MQ135 +{})".format(air_trend))

        # 5. Luce lasciata accesa?
        if sensors and sensors["light"] > 800 and not presence:
            print("[ALERT] Luce accesa ma nessuno presente")

        return self.relay_on

    def apply_output(self):
        self.ser.write(b"1" if self.relay_on else b"0")
```

---

## Fase 5 — Demo flow + bugfix (16-20 h)

### Checklist

- [ ] Scenario 1: entro nella stanza -> luce accesa (subito)
- [ ] Scenario 2: esco -> luce si spegne dopo 5 min
- [ ] Scenario 3: finestra aperta -> alert sul terminale
- [ ] Scenario 4: aria viziata -> alert ventilazione
- [ ] Scenario 5: luce accesa senza nessuno -> alert
- [ ] Test continuativo 30 min senza crash
- [ ] LED feedback funzionante

### Scenari di test rapidi

| Test | Cosa fare | Risultato atteso |
|------|-----------|------------------|
| Presenza | Cammina davanti alla board 10s | Luce ON entro 5s |
| Assenza | Esci dalla stanza, aspetta 5 min | Luce OFF |
| Finestra | Apri finestra vicina | Alert terminale |
| Aria | Chiudi stanza 10 min | Alert ventilazione |
| Luce fissa | Copri LDR, esci dalla stanza | Alert luce accesa |

---

## Fase 6 — Dashboard + storytelling (20-24 h)

### Checklist

- [ ] Flask dashboard avviata
- [ ] Presenza: ON/OFF visibile
- [ ] Grafico RSSI in tempo reale (Chart.js)
- [ ] Temperatura, umidita, qualita aria
- [ ] Storico 5 min
- [ ] Pitch preparato
- [ ] README aggiornato con foto/video demo

### Mini dashboard Flask

```python
#!/usr/bin/env python3
from flask import Flask, jsonify, render_template
from collections import deque
import threading

app = Flask(__name__)
data = {
    "presence": False,
    "rssi": deque(maxlen=100),
    "temp": 0, "hum": 0, "air": 0,
}

@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/api/status")
def api_status():
    return jsonify({
        "presence": data["presence"],
        "temp": data["temp"],
        "humidity": data["hum"],
        "air_quality": data["air"],
        "rssi_history": list(data["rssi"]),
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
```

---

## Riepilogo timeline

| Ora | Fase | Consegna | Critico? |
|-----|------|----------|----------|
| 0-4 | Setup HW | Breadboard + sensori funzionanti | Si |
| 4-8 | RSSI | Sampling + CSV log | Si |
| 8-12 | Presence | Rilevamento funzionante | Si |
| 12-16 | Engine | Decisioni automatiche | Si |
| 16-20 | Bugfix | 5 scenari OK | Si |
| 20-24 | Dashboard | Mini UI + pitch | No (se manca tempo) |

---

## Contingenza (se qualcosa si rompe)

| Problema | Soluzione |
|----------|-----------|
| MQ135 non stabile | Sostituisci con sensore VOC semplice |
| DHT22 letture errate | Controlla cablaggio, prova DHT11 |
| RSSI non disponibile | Usa PIR come fallback |
| Relay non commuta | Verifica alimentazione 5V separata |
| Serial non funziona | Ricarica sketch, riavvia Python |

---

## Pitch finale

> "In 24 ore abbiamo costruito una stanza **context-aware** che sente la presenza usando solo segnali WiFi e sensori di bordo — niente hardware extra, niente cloud, solo decisioni intelligenti all'edge. Dall'automazione semplice all'**intelligenza ambientale**."
