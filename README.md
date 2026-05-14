# Smart Environment Hub — Arduino UNO Q

> Rilevamento presenza passivo via RSSI WiFi + sensori ambientali su Arduino UNO Q.
> Comunicazione STM32↔Linux via **Bridge RPC** (MessagePack su Unix socket), non via seriale.

---

## Board: Arduino UNO Q

| Caratteristica | Dettaglio |
|---------------|-----------|
| **MPU** (Linux) | Qualcomm Dragonwing QRB2210 (quad-core 2.0 GHz, Debian trixie) |
| **MCU** (real-time) | STM32U585 (Arm Cortex-M33, 160 MHz, Zephyr RTOS) |
| **RAM** | 2 GB LPDDR4 (Linux) + 786 KB SRAM (MCU) |
| **WiFi** | Qualcomm integrato (wlan0, 2.4 GHz) |
| **Python** | 3.13.5 |
| **Comunicazione MPU↔MCU** | Bridge RPC (`arduino-router`, Unix socket msgpack) |
| **UART fisica** | `/dev/ttyHS1` a 115200 baud (gestita dal router) |

---

## Architettura — Bridge RPC

Sulla UNO Q la comunicazione tra STM32 e Qualcomm passa per il servizio **`arduino-router`** (Go), non per una `/dev/tty*` tradizionale.

```
STM32 (sketch)                 arduino-router                     Python
┌──────────────────┐    ┌─────────────────────────┐    ┌──────────────────┐
│ Bridge.begin()   │    │ /usr/bin/arduino-router  │    │ bridge_client.py  │
│ Bridge.provide() │◄──►│  --serial-port ttyHS1   │◄──►│ RouterRPC.call()  │
│ Monitor.println()│    │  --serial-baudrate 115200│    │ msgpack socket    │
└──────────────────┘    │  --unix-port .sock      │    └──────────────────┘
                        └─────────────────────────┘
```

Il router e preinstallato e attivo di default. Ascolta su `/var/run/arduino-router.sock` e si connette allo STM32 via `/dev/ttyHS1` a **115200 baud**.

**Perche non funziona `Serial.begin(9600)`?** Perche lo STM32 non espone una seriale classica verso Linux. `Serial.print` va al **USB CDC ACM** (visibile solo collegando la UNO Q a un PC). La comunicazione interna e gestita dal router via MessagePack RPC.

---

## Risultati test di fattibilita

Eseguito sulla UNO Q reale (14/05/2026, 2 iterazioni).

| Test | Run 1 | Run 2 | Dettaglio |
|------|-------|-------|-----------|
| **RSSI Sampling** | ✅ PASS | ✅ PASS | 2.0 Hz, 0 errori, jitter 0.001s |
| **Feature Extraction** | ✅ PASS | ✅ PASS | 0.99ms (pure Python, no numpy) |
| **UART tradizionale** | ❌ FAIL | ❌ FAIL | /dev/ttyHS1 e occupato dal router |
| **Bridge RPC** | 🔶 N/A | 🔶 N/A | Upload sketch su STM32 necessario |
| **System Load** | ✅ PASS | ✅ PASS | CPU 0.2%, RAM 20% |
| **Presence Detection** | ❌ FAIL | ✅ PASS | std 4.66→1.71 (soglia 2.0 fragile) |
| **Combined Pipeline** | ✅ PASS | ✅ PASS | 59 loop, 2.0/s, 0 errori |

### Scoperte chiave

- **RSSI**: `iw link` stabile a 2 Hz su wlan0. `/usr/sbin/iw`.
- **Niente `/proc/net/wireless`**: driver Qualcomm non espone statistiche raw.
- **Comunicazione MCU**: va via `arduino-router`, non via `/dev/tty*`.
- **Router attivo**: da boot, memoria 10.4 MB, CPU irrisoria.
- **Baud rate corretto**: 115200. Sketch legacy usa 9600 — mismatch.
- **Presenza**: rilevabile ma soglia fragile. Serve **adaptive delta** (non std fisso).

---

## Codice

### MCU — `feasibility_bridge.ino`

```cpp
#include "Arduino_RouterBridge.h"

void setup() {
    Bridge.begin();   // Connessione arduino-router via ttyHS1 a 115200
    Bridge.provide("get_sensors", read_and_return_csv);
    Bridge.provide("ping", ping);
    Bridge.provide("set_relay", set_relay);
    Monitor.begin();
    Monitor.println("MCU ready");
}
```

Espone RPC: `ping()`, `get_sensors()` (CSV: `ts,temp,hum,air,light`), `set_relay(0|1)`.

### Python — `bridge_client.py`

```bash
python3 bridge_client.py ping              # -> True
python3 bridge_client.py get_sensors       # -> "ts,24.5,55,320,412"
python3 bridge_client.py set_relay 1       # Accende relay
python3 bridge_client.py discover          # Scopre metodi registrati
```

Include msgpack encoder/decoder built-in. Nessuna dipendenza esterna.

### RSSI sampling

```python
IW = "/usr/sbin/iw"
def get_rssi():
    r = subprocess.check_output(f"{IW} dev wlan0 link", shell=True, timeout=3)
    return float(r.decode().split("signal:")[1].split("dBm")[0])
```

Risultato reale: **2.0 Hz, 0 errori**, segnale -47 a -36 dBm su wlan0.

### Presence detection (adaptive)

```python
class AdaptivePresenceDetector:
    def __init__(self, window_size=20, delta_threshold=1.5):
        self.window = deque(maxlen=window_size)
        self.delta_threshold = delta_threshold

    def update(self, rssi):
        self.window.append(rssi)
        if len(self.window) < 10:
            return False
        recent = list(self.window)[-5:]
        delta = abs(mean(recent) - mean(self.window))
        return delta > self.delta_threshold
```

**Calibrazione**: `python3 calibrate_presence.py --mode quick`

---

## Setup rapido

```bash
# Sul computer: carica sketch sullo STM32
# 1. Apri feasibility_bridge.ino in Arduino IDE
# 2. Board: Arduino UNO Q (STM32)
# 3. Carica via USB-C

# Sulla UNO Q (via SSH):
cd ~/ArduinoApps/ArduinoWifiSensing
git pull
sudo apt-get install -y iw
pip install msgpack

# Test comunicazione STM32
python3 bridge_client.py ping
python3 bridge_client.py get_sensors

# Calibrazione presenza
python3 calibrate_presence.py --mode quick

# Test di fattibilita completo
python3 feasibility_test.py
```

---

## File del progetto

| File | Ruolo |
|------|-------|
| `feasibility_test.py` | Test automatico (RSSI, features, load, bridge, pipeline) |
| `feasibility_bridge.ino` | **Sketch MCU con Arduino_RouterBridge (RPC)** |
| `feasibility_test.ino` | Sketch MCU legacy con Serial (solo per debug USB→PC) |
| `bridge_client.py` | Client Python per RPC via arduino-router Unix socket |
| `calibrate_presence.py` | Calibrazione soglia presenza RSSI |
| `arduino_cloud_integration.md` | Integrazione Arduino Cloud |
| `shopping_list.md` | Componenti e budget |
| `demo_24h_plan.md` | Piano demo 24h |
| `TESTING.md` | Guida esecuzione test |

```
[Sensori MCU]              [WiFi RSSI (Linux)]       [Decision Engine]
DHT22  -> temp/hum         scan wlan0                feature extraction
MQ135  -> aria (VOC)       monitor RSSI over time    adaptive threshold
LDR    -> luce                                        context window
     \_________                         ___________/
               |                       |
               v                       v
          [STM32 MCU]            [Python Core]
    Arduino_RouterBridge         bridge_client.py
    Bridge.provide("get_sensors") RPC via msgpack
           |                            |
           +------- arduino-router -----+
                  Unix socket RPC
                 /var/run/arduino-router.sock
                       |
                       v
                [Decision Engine]
              presence + context -> relay/LED
```

**Zero `/dev/tty*`**. La comunicazione STM32↔Linux avviene via **MessagePack RPC** attraverso il servizio `arduino-router` (Go), non via seriale tradizionale.

Tutto gira **on-board**. Nessun cloud obbligatorio. Privacy garantita.

---

## Board: Arduino UNO Q

| Caratteristica | Dettaglio |
|---------------|-----------|
| **MPU** (Linux) | Qualcomm Dragonwing QRB2210 (quad-core 2.0 GHz, Debian) |
| **MCU** (real-time) | STM32U585 (Arm Cortex-M33, 160 MHz, Zephyr RTOS) |
| RAM Linux | 2 GB LPDDR4 |
| RAM MCU | 786 KB SRAM |
| Flash MCU | 2 MB |
| WiFi | Qualcomm integrato (wlan0, 2.4 GHz) |
| BLE | Bluetooth 5.0 LE |
| GPIO | 14 digitali, 8 analogici (via STM32) |
| Python | 3.13.5 |
| **Comunicazione MPU↔MCU** | `arduino-router` (Unix socket `/var/run/arduino-router.sock`) |
| UART bridge | `/dev/ttyHS1` a **115200 baud** (gestito dal router) |
| Interfaccia | USB-C (programmazione + debug) |

---

## Risultati test di fattibilita

Eseguito sulla UNO Q reale (14/05/2026, 2 iterazioni). **5/7 test PASS** al secondo run.

### Riepilogo

| Test | 1° run | 2° run | Dettaglio |
|------|--------|--------|-----------|
| **RSSI Sampling** | ✅ PASS | ✅ PASS | 2.0 Hz, 0 errori su 40 campioni |
| **Feature Extraction** | ✅ PASS | ✅ PASS | ~1.0 ms per estrazione (pure Python) |
| **UART** | ❌ FAIL | ❌ FAIL | Sketch usa `Serial` — **serve `Arduino_RouterBridge`** |
| **System Load** | ✅ PASS | ✅ PASS | CPU 0.2%, RAM 20% |
| **Presence Detection** | ❌ FAIL | ✅ PASS | std varia: 4.66 (1°) / 1.71 (2°) |
| **Combined Pipeline** | ✅ PASS | ✅ PASS | 59 loop, 2.0/s, 0 errori |

### Scoperte chiave

| Scoperta | Dettaglio |
|----------|-----------|
| **RSSI via `iw`** | Stabile a 2 Hz su wlan0. Usa `/usr/sbin/iw` |
| **Niente `/proc/net/wireless`** | Driver Qualcomm non espone statistiche raw |
| **Comunicazione MCU** | Non via `/dev/tty*` — usa **RPC via `arduino-router`** (Unix socket) |
| **Router attivo** | `systemctl status arduino-router` — in esecuzione da boot |
| **Porta STM32** | `/dev/ttyHS1` a 115200 baud (gestita dal router, non accessibile direttamente) |
| **Socket** | `/var/run/arduino-router.sock` (rw-rw-rw-) |
| **Protocollo** | MessagePack RPC, star topology |
| **Baud rate corretto** | 115200 (il router usa questo, non 9600) |
| **Carico sistema** | Irrisorio: CPU 0.2%, RAM 20% anche con sensing attivo |
| **Presenza** | Rilevabile ma soglia dipende dall'ambiente (std 1.7-4.7) |

---

## Comunicazione STM32 ↔ Linux

Sulla UNO Q, il processore Linux (Qualcomm) e il microcontrollore (STM32) comunicano attraverso l'**Arduino Router**, un servizio Go che implementa un **MessagePack RPC Router** a topologia stellare.

### Architettura di comunicazione

```
STM32 (sketch)                 arduino-router (Go)            Linux (Python)
┌──────────────────┐    ┌─────────────────────────┐    ┌──────────────────┐
│ Bridge.begin()   │    │ /usr/bin/arduino-router  │    │ bridge_client.py  │
│ Bridge.provide() │◄──►│  --serial-port ttyHS1   │◄──►│ RouterRPC.call()  │
│ Monitor.println()│    │  --serial-baudrate 115200│    │ msgpack encode    │
└──────────────────┘    │  --unix-port .sock      │    └──────────────────┘
                        └─────────────────────────┘
                                  │
                           Monitor TCP
                          (Arduino IDE)
```

Il router e preinstallato e attivo di default sulla UNO Q:

```
● arduino-router.service - Arduino Router Service
  Active: active (running) since boot
  Main PID: 573 (arduino-router)
  Memory: 10.4M (peak: 11.1M)
```

### MCU side (sketch Arduino)

```cpp
#include "Arduino_RouterBridge.h"

Bridge.begin();                        // Connessione al router (via ttyHS1, 115200 baud)
Bridge.provide("ping", ping);          // Espone funzione RPC
Bridge.provide("get_sensors", get_sensors);

Monitor.begin();                       // Testo output (alternativa a Serial)
Monitor.println("MCU ready");
```

### Linux side (Python)

```python
from bridge_client import RouterRPC

rpc = RouterRPC()
result = rpc.call("ping")            # Chiama funzione sullo STM32
sensors = rpc.call("get_sensors")    # "ts,temp,hum,air,light"
```

Oppure da terminale:

```bash
python3 bridge_client.py ping
python3 bridge_client.py get_sensors
python3 bridge_client.py discover   # Scopre metodi registrati
```

---

## Pipeline software

### 1. RSSI sampling

```python
import subprocess, shutil

IW = shutil.which("iw") or "/usr/sbin/iw"

def get_rssi() -> float | None:
    try:
        r = subprocess.check_output(
            f"{IW} dev wlan0 link", shell=True, timeout=3,
            stderr=subprocess.DEVNULL).decode()
        return float(r.split("signal:")[1].split("dBm")[0])
    except Exception:
        return None
```

Risultato reale sulla UNO Q: **2.0 Hz stabili, 0 errori**.

### 2. Feature extraction

```python
from statistics import mean, stdev

def extract_features(window: list[float]) -> dict:
    return {"mean": mean(window), "std": stdev(window) if len(window)>=2 else 0,
            "delta": max(window)-min(window), "var": stdev(window)**2 if len(window)>=2 else 0}
```

**1.0 ms** per estrazione in pure Python (numpy non installato).

### 3. Presence detection (adattiva)

Con segnale WiFi forte, lo std ambiente puo variare da 1.7 a 4.7 dBm. La soglia fissa non funziona.

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
        baseline = mean(list(self.window)[:-5]) if len(self.window) > 5 else mean(self.window)
        recent = mean(list(self.window)[-5:])
        return abs(recent - baseline) > self.delta_threshold
```

**Calibrazione**: usa `calibrate_presence.py` per trovare la soglia ottimale:

```bash
python3 calibrate_presence.py --mode quick   # baseline + movement + analisi
python3 calibrate_presence.py --mode monitor # test real-time
```

### 4. Decision engine

```python
rpc = RouterRPC()
detector = AdaptivePresenceDetector()
absence_start = None

while True:
    rssi = get_rssi()
    presence = detector.update(rssi)

    if presence:
        rpc.call("set_relay", 1)        # Accendi luce
        absence_start = None
    else:
        if absence_start is None:
            absence_start = time.time()
        elif time.time() - absence_start > 300:  # 5 min
            rpc.call("set_relay", 0)    # Spegni luce

    sensors = rpc.call("get_sensors")    # Lettura periodica da MCU
    # temp, hum, air, light = map(float, sensors.split(",")[1:])

    time.sleep(0.5)
```

---

## File del progetto

| File | Descrizione |
|------|-------------|
| `feasibility_test.py` | Test automatico (RSSI, features, load, pipeline) |
| `feasibility_bridge.ino` | **Sketch MCU** con `Arduino_RouterBridge` (RPC, non Serial) |
| `bridge_client.py` | Client Python per socket RPC (`/var/run/arduino-router.sock`) |
| `calibrate_presence.py` | Calibrazione soglia presence detection |
| `feasibility_test.ino` | Sketch MCU legacy (Serial, per USB direct to PC) |
| `arduino_cloud_integration.md` | Guida integrazione Arduino Cloud |
| `shopping_list.md` | Componenti con priorita |
| `demo_24h_plan.md` | Piano dimostrazione 24h |
| `TESTING.md` | Guida esecuzione test |

---

## Setup rapido

### 1. Hardware (collegamento sensori allo STM32)

```schema
DHT22 VCC -> 5V       MQ135 VCC -> 5V       LDR + 10kΩ partitore -> A1
DHT22 GND -> GND      MQ135 GND -> GND      Relay VCC -> 5V
DHT22 OUT -> D2       MQ135 OUT -> A0       Relay GND -> GND
                                             Relay IN  -> D3
LED+ -> D4 (con 220Ω) / LED- -> GND
```

### 2. Software

```bash
# Sulla UNO Q (via SSH)
sudo apt-get install -y iw python3-serial python3-pip
pip install msgpack

# Pull ultimo codice
cd ~/ArduinoApps/ArduinoWifiSensing
git pull

# Carica sketch MCU via Arduino IDE
#   - Apri feasibility_bridge.ino
#   - Board: Arduino UNO Q (STM32)
#   - Carica via USB-C

# Test RSSI sampling
python3 feasibility_test.py --install-deps

# Test comunicazione Bridge
python3 bridge_client.py ping
python3 bridge_client.py get_sensors
python3 bridge_client.py discover

# Calibrazione presenza
python3 calibrate_presence.py --mode quick

# Avvia decision engine
python3 decision_engine.py
```

---

## Comunicazione: perche non uso Serial

Sulla UNO Q, lo STM32 **non** espone una `/dev/tty*` classica verso il Linux. La comunicazione e gestita dal servizio `arduino-router` che:

1. Ascolta su `/var/run/arduino-router.sock` (Unix socket, world-writable)
2. Usa **MessagePack RPC** per invocare funzioni tra i due processori
3. Si connette allo STM32 via `/dev/ttyHS1` a **115200 baud** (UART interna Qualcomm↔STM32)
4. Supporta anche Monitor TCP (usato dall'Arduino IDE Serial Monitor)

Lo sketch `feasibility_test.ino` originale usa `Serial.begin(9600)` — questo **non funziona** sulla UNO Q per due motivi:
- Il baud rate del router e **115200**, non 9600
- `Serial.print` non va al router — va al **USB CDC ACM** (solo se collegato a PC via USB)

La soluzione e `feasibility_bridge.ino`, che usa `Arduino_RouterBridge`:
- `Bridge.provide("name", func)` espone funzioni RPC
- `Monitor.println()` manda testo al router (alternativa a `Serial`)
- `Bridge.begin()` si connette automaticamente al router
