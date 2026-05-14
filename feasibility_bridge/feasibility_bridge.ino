/*
 * Feasibility Test — Bridge (MCU side, UNO Q native) v2
 *
 * Usa Arduino_RouterBridge (RPC) su LPUART1 a 115200 baud.
 * L'arduino-router fa da ponte verso il Qualcomm Linux.
 *
 * La libreria Bridge crea automaticamente un thread Zephyr background
 * (updateEntryPoint) che chiama Bridge.update() in loop per processare
 * le RPC in arrivo dal router. Nessun polling manuale necessario.
 *
 * RPC funzioni esposte:
 *   ping()           -> true
 *   get_sensors()    -> "ts,temp,hum,air,light"
 *   set_relay(bool)  -> bool
 *   set_led(bool)    -> bool
 *   test_uart()      -> "LOOPBACK_OK" / "ESP32_OK" / "FAIL:*"
 *
 * LED_BUILTIN signaling:
 *   3 lampeggi rapidi all'avvio  -> boot
 *   1 lungo (~1s)                -> Bridge OK + metodi registrati
 *   5 rapidi + pausa (ciclico)   -> Bridge.begin() fallito, riprovo
 *   2 rapidi                     -> Bridge OK ma alcuni metodi non registrati
 *
 * Pinout:
 *   D2  -> DHT22 OUT
 *   A0  -> MQ135 OUT
 *   A1  -> LDR OUT  (con partitore 10kΩ)
 *   D3  -> Relay IN
 *   D4  -> LED segnalazione
 *   LED_BUILTIN -> stato bridge / heartbeat
 */

#include <Arduino_RouterBridge.h>
#include <DHT.h>

#define DHTPIN   2
#define DHTTYPE  DHT22
#define MQ135PIN A0
#define LDRPIN   A1
#define RELAYPIN 3
#define LEDPIN   4

// ESP32 su D0 (RX) / D1 (TX)
// Sulla UNO Q D0/D1 potrebbero mappare a Serial1 o Serial2.
// Prova Serial1 prima; cambia se non funziona.
#define ESP32_SERIAL  Serial1
#define ESP32_BAUD    115200
static bool esp32_ok = false;

DHT dht(DHTPIN, DHTTYPE);

// --- LED blink helper (non-blocking via millis) ---
static unsigned long _blink_until = 0;
static int _blink_count = 0;
static int _blink_target = 0;
static int _blink_ms_on = 0;
static int _blink_ms_off = 0;
static bool _blink_state = false;

static void blink_start(int n, int ms_on = 80, int ms_off = 80) {
    _blink_target = n;
    _blink_count = 0;
    _blink_ms_on = ms_on;
    _blink_ms_off = ms_off;
    _blink_state = false;  // start with LOW
    _blink_until = 0;      // re-start
    digitalWrite(LED_BUILTIN, LOW);
}

static bool blink_tick() {
    if (_blink_target == 0) return false;  // no blink active

    if (_blink_count >= _blink_target) {
        if (millis() - _blink_until > 500) {
            _blink_target = 0;  // blink sequence done, clear
            return false;
        }
        return true;  // still in "pause after sequence"
    }

    if (_blink_until == 0 || millis() >= _blink_until) {
        _blink_state = !_blink_state;
        digitalWrite(LED_BUILTIN, _blink_state ? HIGH : LOW);

        if (_blink_state) {
            // turned ON — schedule OFF
            _blink_until = millis() + _blink_ms_on;
        } else {
            // turned OFF — count and schedule next ON
            _blink_count++;
            _blink_until = (_blink_count >= _blink_target)
                           ? millis()      // stay off after last blink
                           : millis() + _blink_ms_off;
        }
    }

    return true;
}

// Stato bridge (int per compatibilita Arduino builder forward-declaration)
static const int BRIDGE_BOOT    = 0;
static const int BRIDGE_RETRY   = 1;
static const int BRIDGE_OK      = 2;
static const int BRIDGE_PARTIAL = 3;
static int bridge_state = BRIDGE_BOOT;
static unsigned long last_bridge_retry = 0;

// ============================================================
// RPC functions exposed to Linux side
// ============================================================

bool ping() {
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
// UART test — auto-detect: loopback (D0-D1 shorted) o ESP32
// ============================================================

static String _uart_readln(int timeout_ms) {
    unsigned long deadline = millis() + timeout_ms;
    while (millis() < deadline) {
        if (ESP32_SERIAL.available() > 0) {
            String line = ESP32_SERIAL.readStringUntil('\n');
            line.trim();
            if (line.length() > 0) return line;
        }
    }
    return "";
}

String test_uart() {
    esp32_ok = false;

    // Svuota buffer seriale
    while (ESP32_SERIAL.available()) ESP32_SERIAL.read();
    delay(50);
    while (ESP32_SERIAL.available()) ESP32_SERIAL.read();

    // --- Tentativo 1: ESP32 ping/pong ---
    ESP32_SERIAL.println("ping");
    String resp = _uart_readln(600);
    if (resp == "pong:OK" || resp.indexOf("pong:OK") >= 0) {
        esp32_ok = true;
        return String("ESP32_OK");
    }

    // Svuota buffer
    while (ESP32_SERIAL.available()) ESP32_SERIAL.read();
    delay(50);
    while (ESP32_SERIAL.available()) ESP32_SERIAL.read();

    // --- Tentativo 2: loopback (D0-D1 cortocircuitati con jumper) ---
    ESP32_SERIAL.println("LOOPBACK_TEST");
    resp = _uart_readln(600);
    if (resp == "LOOPBACK_TEST") {
        return String("LOOPBACK_OK");
    }

    // Fallback: riporta cosa abbiamo ricevuto (se qualcosa)
    if (resp.length() > 0) {
        return String("FAIL:unexpected=") + resp.substring(0, 40);
    }
    return String("FAIL:no_response");
}

// ============================================================
// Tentativo di inizializzazione bridge (definita prima di setup)
// ============================================================
static int register_methods() {
    // NOTA: i metodi safe (get_sensors) vengono processati dal __loopHook
    // automatico nel loop di Arduino. I metodi normali dal thread Zephyr.
    bool ok = true;
    ok &= Bridge.provide("ping", ping);
    ok &= Bridge.provide_safe("get_sensors", get_sensors);
    ok &= Bridge.provide("set_relay", set_relay);
    ok &= Bridge.provide("set_led", set_led);
    ok &= Bridge.provide_safe("test_uart", test_uart);

    return ok ? BRIDGE_OK : BRIDGE_PARTIAL;
}

static int try_bridge_init() {
    if (Bridge.is_started()) {
        // Re-register after router reset
        return register_methods();
    }

    if (!Bridge.begin()) {
        return BRIDGE_RETRY;
    }

    return register_methods();
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

    // Inizializza UART verso ESP32 (D0/D1)
    ESP32_SERIAL.begin(ESP32_BAUD);
    // Svuota buffer iniziale
    delay(200);
    while (ESP32_SERIAL.available()) ESP32_SERIAL.read();

    // Segnale di boot: 3 lampeggi rapidi
    blink_start(3, 80, 80);

    // Primo tentativo di inizializzazione bridge
    bridge_state = try_bridge_init();
    if (bridge_state == BRIDGE_OK || bridge_state == BRIDGE_PARTIAL) {
        Monitor.begin();
        Monitor.print("BRIDGE: sensor sketch v2, state=");
        Monitor.println(bridge_state == BRIDGE_OK ? "OK" : "PARTIAL");
    }

    // Test UART su D0/D1 (loopback o ESP32)
    String uart_status = test_uart();
    if (bridge_state == BRIDGE_OK || bridge_state == BRIDGE_PARTIAL) {
        Monitor.print("UART: ");
        Monitor.println(uart_status);
    }
}

// ============================================================
// Loop
// ============================================================
void loop() {
    unsigned long now = millis();

    // --- LED blink handling (non-blocking) ---
    blink_tick();

    // --- Bridge retry logic ---
    if (bridge_state == BRIDGE_RETRY) {
        if (now - last_bridge_retry >= 5000) {
            last_bridge_retry = now;
            bridge_state = try_bridge_init();
            if (bridge_state == BRIDGE_OK || bridge_state == BRIDGE_PARTIAL) {
                Monitor.begin();
                Monitor.println("BRIDGE: reconnected after retry");
            }
        }
        // LED segnalazione retry: 5 rapidi, pausa, ripeti
        static unsigned long last_blink = 0;
        if (now - last_blink >= 2000) {
            last_blink = now;
            blink_start(5, 60, 60);
        }
    }

    // --- LED stato OK / PARTIAL ---
    else if (bridge_state == BRIDGE_OK) {
        static unsigned long last_heartbeat = 0;
        if (now - last_heartbeat >= 3000) {
            last_heartbeat = now;
            blink_start(1, 600, 0);  // 1 lungo = tutto ok
        }
    }
    else if (bridge_state == BRIDGE_PARTIAL) {
        static unsigned long last_heartbeat = 0;
        if (now - last_heartbeat >= 3000) {
            last_heartbeat = now;
            blink_start(2, 100, 100);  // 2 rapidi = registrazione parziale
        }
    }

    // --- Sensor sampling periodico (2 Hz) su Monitor ---
    if (bridge_state == BRIDGE_OK || bridge_state == BRIDGE_PARTIAL) {
        if (Monitor.is_connected()) {
            static unsigned long last_print = 0;
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
            }
        }
    }
}
