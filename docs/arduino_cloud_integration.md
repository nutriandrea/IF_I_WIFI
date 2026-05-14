# Arduino Cloud Integration

> Ponte bidirezionale tra sensori fisici e dashboard cloud, con controllo remoto degli attuatori.

---

## Architettura

```
[Sensori MCU] -> [Arduino UNO Q] -> (WiFi) -> [Arduino Cloud]
[Arduino Cloud] -> (WiFi) -> [Arduino UNO Q] -> [Relay / LED]
```

Comunicazione bidirezionale:
- **Device -> Cloud**: sensori (temperatura, aria, presenza, RSSI)
- **Cloud -> Device**: comandi (override luce, emoji LED matrix)

---

## 1. Creare un Thing su Arduino Cloud

### Passi

1. Vai su [Arduino Cloud](https://cloud.arduino.cc/)
2. Clicca **Create Thing**
3. Assegna nome: `SmartEnvironmentHub`
4. Associa la board: **Arduino UNO Q** (segui il wizard di provisioning)
5. Aggiungi le variabili:

| Variabile | Tipo | Direzione | Aggiornamento |
|-----------|------|-----------|---------------|
| `roomTemp` | float | READ | ON_CHANGE |
| `roomHumidity` | float | READ | ON_CHANGE |
| `airQuality` | int | READ | ON_CHANGE |
| `lightLevel` | int | READ | ON_CHANGE |
| `presence` | bool | READ | ON_CHANGE |
| `rssiStrength` | float | READ | ON_CHANGE |
| `lightOverride` | bool | READWRITE | ON_CHANGE |
| `ledCommand` | String | READWRITE | ON_CHANGE |

### Tipi di accesso

- **READ**: Solo invio dal device al cloud
- **READWRITE**: Bidirezionale (cloud puo modificare)

---

## 2. Provisioning della board

1. Collega UNO Q via USB-C
2. Segui il **wizard di provisioning** nell'Arduino Cloud:
   - Scarica e installa il plugin `Arduino Cloud Agent`
   - Configura WiFi (SSID + password)
   - I certificati vengono installati automaticamente
3. Tempo totale: ~5 min

---

## 3. Codice Arduino completo

```cpp
#include <ArduinoIoTCloud.h>
#include <WiFiConnection.h>
#include <DHT.h>

// === Pin definitions ===
#define DHTPIN  2
#define DHTTYPE DHT22
#define MQ135PIN A0
#define LDRPIN  A1
#define RELAYPIN 3
#define LEDPIN   4

DHT dht(DHTPIN, DHTTYPE);

// === Cloud variables ===
float roomTemp = 0;
float roomHumidity = 0;
int airQuality = 0;
int lightLevel = 0;
bool presence = false;
float rssiStrength = 0;
bool lightOverride = false;
String ledCommand = "";

// === Previous state for change detection ===
float prevTemp = -999;
int prevAir = -999;

void setup() {
    Serial.begin(9600);
    dht.begin();

    pinMode(RELAYPIN, OUTPUT);
    pinMode(LEDPIN, OUTPUT);
    digitalWrite(RELAYPIN, LOW);
    digitalWrite(LEDPIN, LOW);

    initCloud();
    ArduinoCloud.begin(ArduinoIoTPreferredConnection);
    setDebugMessageLevel(2);
    ArduinoCloud.printDebugInfo();
}

void initCloud() {
    ArduinoCloud.addProperty(roomTemp, READ, 1, ON_CHANGE);
    ArduinoCloud.addProperty(roomHumidity, READ, 1, ON_CHANGE);
    ArduinoCloud.addProperty(airQuality, READ, 1, ON_CHANGE);
    ArduinoCloud.addProperty(lightLevel, READ, 1, ON_CHANGE);
    ArduinoCloud.addProperty(presence, READ, 1, ON_CHANGE);
    ArduinoCloud.addProperty(rssiStrength, READ, 1, ON_CHANGE);
    ArduinoCloud.addProperty(lightOverride, READWRITE, 1, ON_CHANGE);
    ArduinoCloud.addProperty(ledCommand, READWRITE, 1, ON_CHANGE);
}

void loop() {
    ArduinoCloud.update();

    // Read sensors
    roomTemp = dht.readTemperature();
    roomHumidity = dht.readHumidity();
    airQuality = analogRead(MQ135PIN);
    lightLevel = analogRead(LDRPIN);

    // Apply cloud commands
    if (lightOverride) {
        digitalWrite(RELAYPIN, HIGH);
    }

    if (ledCommand == "ON") {
        digitalWrite(LEDPIN, HIGH);
    } else if (ledCommand == "OFF") {
        digitalWrite(LEDPIN, LOW);
    } else if (ledCommand == "BLINK") {
        digitalWrite(LEDPIN, !digitalRead(LEDPIN));
    }

    // Log serial
    Serial.print("Temp: ");
    Serial.print(roomTemp);
    Serial.print(" | Air: ");
    Serial.print(airQuality);
    Serial.print(" | Light: ");
    Serial.println(lightLevel);
    Serial.print(" | Override: ");
    Serial.println(lightOverride ? "ON" : "OFF");

    delay(5000);
}

void onLightOverrideChange() {
    if (!lightOverride) {
        digitalWrite(RELAYPIN, LOW);
    }
}

void onLedCommandChange() {
    Serial.print("LED command received: ");
    Serial.println(ledCommand);
}
```

---

## 4. Dashboard interattiva

### Widget consigliati

| Widget | Variabile | Tipo |
|--------|-----------|------|
| Gauge | `roomTemp` | Valore numerico + colore |
| Percentuale | `roomHumidity` | Barra progresso |
| Indicatore | `airQuality` | Semaforo (verde/giallo/rosso) |
| LED | `presence` | ON/OFF |
| Chart | `rssiStrength` | Grafico storico |
| Switch | `lightOverride` | ON/OFF controllabile |
| Dropdown | `ledCommand` | "ON", "OFF", "BLINK" |

### Setup dashboard

1. Clicca **Create Dashboard** nell'Arduino Cloud
2. Aggiungi widget dal pannello di destra
3. Collega ogni widget alla variabile corrispondente
4. Personalizza layout e colori

---

## 5. Collegamento al progetto

| Componente progetto | Variabile Cloud | Tipo |
|---------------------|----------------|------|
| Rilevamento presenza (RSSI) | `presence` | bool |
| DHT22 temperatura | `roomTemp` | float |
| DHT22 umidita | `roomHumidity` | float |
| MQ135 qualita aria | `airQuality` | int |
| LDR luminosita | `lightLevel` | int |
| RSSI segnale | `rssiStrength` | float |
| Relay lampadina | `lightOverride` | bool (READWRITE) |
| LED stato | `ledCommand` | String (READWRITE) |

---

## 6. Test rapido

### Verifica comunicazione device -> cloud

1. Apri la dashboard
2. Controlla che `roomTemp` e `airQuality` si aggiornino ogni ~5s
3. Copri il LDR -> `lightLevel` dovrebbe scendere
4. Muoviti davanti alla board -> `presence` dovrebbe diventare `true`

### Verifica comunicazione cloud -> device

1. Attiva lo switch `lightOverride` sulla dashboard
2. Il relay dovrebbe scattare (lampadina accesa)
3. Cambia `ledCommand` -> il LED deve rispondere

---

## 7. Troubleshooting

| Problema | Causa possibile | Soluzione |
|----------|----------------|-----------|
| Variabili non si aggiornano | WiFi disconnesso | Riavvia board, verifica provisioning |
| `lightOverride` non ha effetto | Callback mancante | Aggiungi `onLightOverrideChange()` |
| Board non appare in Cloud | Plugin Cloud Agent non installato | Reinstalla Arduino Cloud Agent |
| Valori sensori anomali | Cablaggio errato | Verifica pin e tensioni |
| DHT22 restituisce NaN | Sensore non inizializzato | Controlla cablaggio + delay(2000) in setup |
| MQ135 lettura fissa a 0 o 1023 | Pin sbagliato o GND flottante | Verifica MQ135PIN e connessione GND |
| Relay non commuta | Alimentazione insufficiente | Usa alimentatore esterno 5V per relay |

---

## 8. Esempio di deployment avanzato

### Unire RSSI presence + Cloud

```cpp
// Questa parte va chiamata dal processo Python
// che calcola presence via RSSI
void setPresence(bool detected) {
    presence = detected;
    if (detected && !lightOverride) {
        digitalWrite(RELAYPIN, HIGH);
    }
}
```

### Integrazione via Serial (Python -> Arduino)

```python
import serial

ser = serial.Serial('/dev/ttyACM0', 9600)

def send_presence_to_cloud(detected: bool):
    ser.write(f"PRESENCE:{int(detected)}\n".encode())
```

Lato Arduino:

```cpp
void loop() {
    ArduinoCloud.update();

    if (Serial.available()) {
        String cmd = Serial.readStringUntil('\n');
        if (cmd.startsWith("PRESENCE:")) {
            presence = cmd.substring(9).toInt() == 1;
        }
    }
    // ... lettura sensori ...
}
```

---

## Riferimenti

- [Arduino Cloud — Getting Started](https://docs.arduino.cc/arduino-cloud/getting-started/)
- [Arduino UNO Q Docs](https://docs.arduino.cc/hardware/uno-q)
- [ArduinoIoTCloud Library](https://github.com/arduino-libraries/ArduinoIoTCloud)
- [Edge Impulse + Arduino Cloud](https://docs.edgeimpulse.com/docs/deployment/arduino)
