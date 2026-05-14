/*
 * Feasibility Test — Bridge (MCU side, UNO Q native)
 *
 * Usa Arduino_RouterBridge (RPC) invece di Serial.begin/println.
 * L'arduino-router fa da ponte verso il Qualcomm Linux via /dev/ttyHS1.
 *
 * RPC funzioni esposte:
 *   ping()           -> true
 *   get_sensors()    -> "ts,temp,hum,air,light"
 *   set_relay(int)   -> bool
 *   set_led(int)     -> bool
 *
 * Pinout:
 *   D2  -> DHT22 OUT
 *   A0  -> MQ135 OUT
 *   A1  -> LDR OUT  (con partitore 10kΩ)
 *   D3  -> Relay IN
 *   D4  -> LED segnalazione
 */

#include "Arduino_RouterBridge.h"

#define DHTPIN  2
#define DHTTYPE DHT22
#define MQ135PIN A0
#define LDRPIN  A1
#define RELAYPIN 3
#define LEDPIN   4

DHT dht(DHTPIN, DHTTYPE);

unsigned long last_print = 0;
const unsigned long INTERVAL_MS = 2000;

// ============================================================
// RPC functions exposed to Linux side
// ============================================================

bool ping() {
    digitalWrite(LED_BUILTIN, HIGH);
    delay(50);
    digitalWrite(LED_BUILTIN, LOW);
    return true;
}

String get_sensors() {
    float t = dht.readTemperature();
    float h = dht.readHumidity();
    if (isnan(t) || isnan(h)) { t = 0; h = 0; }
    int air = analogRead(MQ135PIN);
    int light = analogRead(LDRPIN);
    unsigned long ts = millis() / 1000;

    return String(ts) + "," +
           String(t) + "," +
           String(h) + "," +
           String(air) + "," +
           String(light);
}

bool set_relay(int state) {
    digitalWrite(RELAYPIN, state == 1 ? HIGH : LOW);
    return state == 1;
}

bool set_led(int state) {
    digitalWrite(LEDPIN, state == 1 ? HIGH : LOW);
    return state == 1;
}

// ============================================================
// Setup
// ============================================================
void setup() {
    pinMode(RELAYPIN, OUTPUT);
    pinMode(LEDPIN, OUTPUT);
    pinMode(LED_BUILTIN, OUTPUT);
    digitalWrite(RELAYPIN, LOW);
    digitalWrite(LEDPIN, LOW);
    digitalWrite(LED_BUILTIN, LOW);

    dht.begin();

    // Connessione all'arduino-router via /dev/ttyHS1 a 115200 baud
    Bridge.begin();

    // Esponi funzioni RPC
    Bridge.provide("ping", ping);
    Bridge.provide("get_sensors", get_sensors);
    Bridge.provide("set_relay", set_relay);
    Bridge.provide("set_led", set_led);

    // Log sul Monitor (alternativa a Serial per testo)
    Monitor.begin();
    Monitor.println("BRIDGE: MCU ready — ping, get_sensors, set_relay, set_led");

    // Blink 3x conferma
    for (int i = 0; i < 3; i++) {
        digitalWrite(LED_BUILTIN, HIGH);
        delay(100);
        digitalWrite(LED_BUILTIN, LOW);
        delay(100);
    }

    delay(500);
}

// ============================================================
// Loop
// ============================================================
void loop() {
    unsigned long now = millis();

    if (now - last_print >= INTERVAL_MS) {
        last_print = now;

        float t = dht.readTemperature();
        float h = dht.readHumidity();
        if (isnan(t) || isnan(h)) { t = 0; h = 0; }
        int air = analogRead(MQ135PIN);
        int light = analogRead(LDRPIN);
        unsigned long ts = now / 1000;

        // CSV periodico sul Monitor (visibile da router)
        Monitor.print(ts);
        Monitor.print(",");
        Monitor.print(t);
        Monitor.print(",");
        Monitor.print(h);
        Monitor.print(",");
        Monitor.print(air);
        Monitor.print(",");
        Monitor.println(light);

        // LED heartbeat
        digitalWrite(LED_BUILTIN, HIGH);
        delay(50);
        digitalWrite(LED_BUILTIN, LOW);
    }
}
