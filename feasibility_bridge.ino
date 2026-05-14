/*
 * Feasibility Test — Bridge (MCU side, UNO Q native)
 *
 * Usa Arduino_RouterBridge (RPC) su LPUART1 a 115200 baud.
 * L'arduino-router fa da ponte verso il Qualcomm Linux.
 *
 * Basato su simple_bridge.ino (libreria ufficiale Arduino_RouterBridge).
 *
 * RPC funzioni esposte:
 *   ping()           -> true
 *   get_sensors()    -> "ts,temp,hum,air,light"
 *   set_relay(bool)  -> bool
 *   set_led(bool)    -> bool
 *
 * Pinout:
 *   D2  -> DHT22 OUT
 *   A0  -> MQ135 OUT
 *   A1  -> LDR OUT  (con partitore 10kΩ)
 *   D3  -> Relay IN
 *   D4  -> LED segnalazione
 *   LED_BUILTIN -> heartbeat / error blink
 */

#include <Arduino_RouterBridge.h>
#include <DHT.h>

#define DHTPIN   2
#define DHTTYPE  DHT22
#define MQ135PIN A0
#define LDRPIN   A1
#define RELAYPIN 3
#define LEDPIN   4

DHT dht(DHTPIN, DHTTYPE);

// LED blink helper: n lampeggi brevi
static void blink_n(int n, int ms = 80) {
    for (int i = 0; i < n; i++) {
        digitalWrite(LED_BUILTIN, HIGH);
        delay(ms);
        digitalWrite(LED_BUILTIN, LOW);
        if (i < n - 1) delay(ms);
    }
}

// ============================================================
// RPC functions exposed to Linux side
// ============================================================

bool ping() {
    blink_n(1, 30);
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

bool set_relay(bool state) {
    digitalWrite(RELAYPIN, state ? HIGH : LOW);
    return state;
}

bool set_led(bool state) {
    digitalWrite(LEDPIN, state ? HIGH : LOW);
    return state;
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

    // Inizializza bridge RPC su Serial1 (LPUART1, 115200 baud)
    if (!Bridge.begin()) {
        // Errore critico: bridge non inizializzato
        while (true) {
            blink_n(5, 100);  // 5 lampeggi rapidi = errore bridge
            delay(1000);
        }
    }

    // Registra funzioni RPC — verifica ogni return value
    bool ok = true;
    ok &= Bridge.provide("ping", ping);
    ok &= Bridge.provide_safe("get_sensors", get_sensors);
    ok &= Bridge.provide("set_relay", set_relay);
    ok &= Bridge.provide("set_led", set_led);

    // Inizializza Monitor (stream TCP per debug)
    Monitor.begin();

    if (ok) {
        Monitor.println("BRIDGE: all methods registered OK");
        blink_n(2, 150);  // 2 lampeggi = tutto OK
    } else {
        Monitor.println("BRIDGE: some methods FAILED to register");
        blink_n(4, 150);  // 4 lampeggi = registration error
    }

    delay(500);
}

// ============================================================
// Loop
// ============================================================
void loop() {
    // Stream periodico dati sensori sul Monitor (2 Hz)
    static unsigned long last_print = 0;
    unsigned long now = millis();

    if (now - last_print >= 2000) {
        last_print = now;

        float t = dht.readTemperature();
        float h = dht.readHumidity();
        if (isnan(t) || isnan(h)) { t = 0; h = 0; }
        int air = analogRead(MQ135PIN);
        int light = analogRead(LDRPIN);
        unsigned long ts = now / 1000;

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
        delay(30);
        digitalWrite(LED_BUILTIN, LOW);
    }
}
