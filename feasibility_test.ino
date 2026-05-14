/*
 * Feasibility Test — Arduino UNO Q (MCU side)
 *
 * Invia sensori timestampati via Serial ogni 2s.
 * Il Python test.py legge questi dati per validare
 * la comunicazione UART tra MCU e Linux core.
 *
 * Pinout:
 *   D2  -> DHT22 OUT
 *   A0  -> MQ135 OUT
 *   A1  -> LDR OUT  (con partitore 10kΩ)
 *   D3  -> Relay IN
 *   D4  -> LED+
 */

#include <DHT.h>

#define DHTPIN  2
#define DHTTYPE DHT22
#define MQ135PIN A0
#define LDRPIN  A1
#define RELAYPIN 3
#define LEDPIN   4

DHT dht(DHTPIN, DHTTYPE);

unsigned long last_print = 0;
const unsigned long INTERVAL_MS = 2000;

void setup() {
    Serial.begin(9600);
    dht.begin();
    pinMode(RELAYPIN, OUTPUT);
    pinMode(LEDPIN, OUTPUT);
    digitalWrite(RELAYPIN, LOW);
    digitalWrite(LEDPIN, LOW);

    // Wait for sensors to stabilize
    delay(1000);
}

void loop() {
    unsigned long now = millis();

    if (now - last_print >= INTERVAL_MS) {
        last_print = now;

        float t = dht.readTemperature();
        float h = dht.readHumidity();
        int air = analogRead(MQ135PIN);
        int light = analogRead(LDRPIN);
        unsigned long ts = now / 1000;  // seconds since boot

        // Formato: timestamp,temp,humid,air,light
        Serial.print(ts);
        Serial.print(",");
        Serial.print(t);
        Serial.print(",");
        Serial.print(h);
        Serial.print(",");
        Serial.print(air);
        Serial.print(",");
        Serial.println(light);

        // Blink LED on each send
        digitalWrite(LEDPIN, HIGH);
        delay(50);
        digitalWrite(LEDPIN, LOW);
    }

    // Listen for commands from Python
    if (Serial.available() > 0) {
        char cmd = Serial.read();
        if (cmd == '1') {
            digitalWrite(RELAYPIN, HIGH);
            Serial.println("RELAY:ON");
        } else if (cmd == '0') {
            digitalWrite(RELAYPIN, LOW);
            Serial.println("RELAY:OFF");
        }
    }
}
