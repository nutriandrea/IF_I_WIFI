/*
 * ESP32 UART Test — ping/pong bridge per UNO Q
 *
 * Si collega via UART (RX/TX) all'Arduino UNO Q su D0/D1 (STM32 Serial1).
 * Risponde a "ping" con "pong:OK" — verifica base della comunicazione.
 *
 * Pinout ESP32 → UNO Q:
 *   ESP32 GND  → UNO Q GND
 *   ESP32 5V   → UNO Q 5V
 *   ESP32 RX   → UNO Q D1 (TX dello STM32)
 *   ESP32 TX   → UNO Q D0 (RX dello STM32)
 *
 * Baud rate: 115200
 *
 * LED_BUILTIN (GPIO2):
 *   3 lampeggi all'avvio → ESP32 vivo
 *   Lampeggio lungo      → ping ricevuto e risposto
 */
#define SERIAL_BAUD 115200
#define LED_PIN     2          // ESP32 built-in LED
void setup() {
    Serial.begin(SERIAL_BAUD);
    pinMode(LED_PIN, OUTPUT);
    // Segnale di boot: 3 lampeggi rapidi
    for (int i = 0; i < 3; i++) {
        digitalWrite(LED_PIN, HIGH);
        delay(100);
        digitalWrite(LED_PIN, LOW);
        delay(100);
    }
    // Messaggio di benvenuto — lo STM32 può leggerlo all'avvio
    Serial.println("ESP32_READY");
}
void loop() {
    if (Serial.available() > 0) {
        String cmd = Serial.readStringUntil('\n');
        cmd.trim();
        if (cmd == "ping") {
            // Ping classico → pong:OK
            Serial.println("pong:OK");
            // Feedback visivo: lampeggio lungo
            digitalWrite(LED_PIN, HIGH);
            delay(200);
            digitalWrite(LED_PIN, LOW);
        } else if (cmd == "status") {
            // Report stato ESP32
            Serial.print("status:alive,uptime=");
            Serial.println(millis() / 1000);
        } else {
            // Echo di qualsiasi altro comando
            Serial.print("echo:");
            Serial.println(cmd);
        }
    }
}