/*
 * ESP32 CSI Bridge — UNO Q MCU side
 *
 * Ponte seriale tra ESP32 (CSI capture su D0/D1) e arduino-router.
 * Legge dati CSI dalla ESP32 su Serial1, li bufferizza e li espone
 * come RPC al Linux MPU via Arduino_RouterBridge.
 *
 * Cablaggio ESP32 → UNO Q:
 *   ESP32 GND  → UNO Q GND
 *   ESP32 5V   → UNO Q 5V
 *   ESP32 TX   → UNO Q D0 (MCU RX = Serial1 RX)
 *   ESP32 RX   → UNO Q D1 (MCU TX = Serial1 TX)
 *   Baud rate: 115200
 *
 * RPC funzioni esposte:
 *   csi_ping()         -> "pong" o errore
 *   csi_count()        -> int (frame bufferizzati)
 *   csi_read_all()     -> [string, ...] (frame CSI, svuota buffer)
 *   csi_clear()        -> true
 *
 * LED_BUILTIN:
 *   3 lampeggi rapidi  -> boot
 *   1 lungo            -> bridge OK
 *   2 rapidi + pausa   -> ESP32 non rilevata
 */

#include <Arduino_RouterBridge.h>

#define SERIAL_BAUD    115200
#define CSI_BUF_MAX    30           // max frame in buffer (RAM limit ~2 KB)
#define CSI_LINE_MAX   512          // max lunghezza singolo frame CSI

// ESP32 su D0 (RX) / D1 (TX) — Serial1 del MCU
#define ESP32_SERIAL  Serial1

// === Buffer circolare CSI ===
static char csi_buffer[CSI_BUF_MAX][CSI_LINE_MAX];
static int csi_buffer_count = 0;
static bool esp32_ok = false;

// === Timer per polling ESP32 ===
static unsigned long last_esp32_poll = 0;
#define ESP32_POLL_MS 50  // 20 Hz lettura da ESP32

// === LED blink helper ===
static unsigned long _blink_until = 0;
static int _blink_count = 0;
static int _blink_target = 0;
static bool _blink_state = false;
static int _blink_ms_on = 0;
static int _blink_ms_off = 0;

static void blink_start(int n, int ms_on = 80, int ms_off = 80) {
    _blink_target = n;
    _blink_count = 0;
    _blink_ms_on = ms_on;
    _blink_ms_off = ms_off;
    _blink_state = false;
    _blink_until = 0;
    digitalWrite(LED_BUILTIN, LOW);
}

static bool blink_tick() {
    if (_blink_target == 0) return false;
    if (_blink_count >= _blink_target) {
        if (millis() - _blink_until > 500) {
            _blink_target = 0;
            return false;
        }
        return true;
    }
    if (_blink_until == 0 || millis() >= _blink_until) {
        _blink_state = !_blink_state;
        digitalWrite(LED_BUILTIN, _blink_state ? HIGH : LOW);
        if (_blink_state) {
            _blink_until = millis() + _blink_ms_on;
        } else {
            _blink_count++;
            _blink_until = (_blink_count >= _blink_target)
                ? millis()
                : millis() + _blink_ms_off;
        }
    }
    return true;
}

// ============================================================
// ESP32 Communication
// ============================================================

static void esp32_flush() {
    while (ESP32_SERIAL.available()) ESP32_SERIAL.read();
}

static void esp32_send_cmd(const char* cmd) {
    ESP32_SERIAL.println(cmd);
}

static int esp32_read_line(char* buf, int max_len, int timeout_ms) {
    unsigned long deadline = millis() + timeout_ms;
    int idx = 0;
    while (millis() < deadline) {
        while (ESP32_SERIAL.available() && idx < max_len - 1) {
            char c = ESP32_SERIAL.read();
            if (c == '\n') {
                buf[idx] = '\0';
                return idx;
            }
            if (c != '\r') buf[idx++] = c;
        }
        if (idx > 0) {
            // timeout extended if we already started receiving
            deadline = millis() + 20;
        }
    }
    buf[idx] = '\0';
    return idx;
}

// Polling ESP32: legge linee CSI e le bufferizza
static void esp32_poll() {
    if (!esp32_ok) return;

    unsigned long now = millis();
    if (now - last_esp32_poll < ESP32_POLL_MS) return;
    last_esp32_poll = now;

    while (ESP32_SERIAL.available() && csi_buffer_count < CSI_BUF_MAX) {
        char line[CSI_LINE_MAX];
        int len = esp32_read_line(line, CSI_LINE_MAX, 10);
        if (len > 0) {
            strncpy(csi_buffer[csi_buffer_count], line, CSI_LINE_MAX - 1);
            csi_buffer[csi_buffer_count][CSI_LINE_MAX - 1] = '\0';
            csi_buffer_count++;
        } else {
            break;  // no more complete lines
        }
    }
}

// ============================================================
// RPC functions exposed to Linux side
// ============================================================

String csi_ping() {
    if (!esp32_ok) return String("ESP32_NOT_CONNECTED");

    esp32_flush();
    esp32_send_cmd("ping");
    char resp[64];
    int len = esp32_read_line(resp, sizeof(resp), 500);
    if (len > 0) {
        return String("pong:") + String(resp);
    }
    return String("ESP32_NO_RESPONSE");
}

int csi_count() {
    return csi_buffer_count;
}

String csi_read_all() {
    // Pack all buffered frames into one big string, newline-separated
    // Each frame is a single line
    String result;
    for (int i = 0; i < csi_buffer_count; i++) {
        if (i > 0) result += "\n";
        result += String(csi_buffer[i]);
    }
    csi_buffer_count = 0;  // svuota buffer
    return result;
}

bool csi_clear() {
    csi_buffer_count = 0;
    esp32_flush();
    return true;
}

// ============================================================
// Bridge init
// ============================================================

static int register_methods() {
    bool ok = true;
    ok &= Bridge.provide("csi_ping", csi_ping);
    ok &= Bridge.provide("csi_count", csi_count);
    ok &= Bridge.provide_safe("csi_read_all", csi_read_all);
    ok &= Bridge.provide("csi_clear", csi_clear);
    return ok ? 2 : 3;  // BRIDGE_OK or BRIDGE_PARTIAL
}

static int try_bridge_init() {
    if (Bridge.is_started()) return register_methods();
    if (!Bridge.begin()) return 1;  // BRIDGE_RETRY
    return register_methods();
}

// ============================================================
// Setup & Loop
// ============================================================

void setup() {
    pinMode(LED_BUILTIN, OUTPUT);
    digitalWrite(LED_BUILTIN, LOW);

    // 3 lampeggi = boot
    blink_start(3, 80, 80);

    // Serial1 = D0/D1 verso ESP32
    ESP32_SERIAL.begin(SERIAL_BAUD);
    delay(200);
    esp32_flush();

    // Prova a parlare con ESP32
    esp32_send_cmd("ping");
    char resp[64];
    int len = esp32_read_line(resp, sizeof(resp), 600);
    if (len > 0 && strstr(resp, "pong") != NULL) {
        esp32_ok = true;
    } else {
        // ESP32 non risponde — continuiamo lo stesso
        esp32_ok = false;
    }

    // Inizializza bridge
    int state = try_bridge_init();
    if (state == 2) {
        blink_start(1, 500, 0);  // 1 lungo = OK
    } else if (!esp32_ok) {
        blink_start(2, 80, 80);  // 2 rapidi = bridge OK ma no ESP32
    }

    last_esp32_poll = millis();
}

void loop() {
    blink_tick();

    // Poll ESP32 per nuovi dati CSI
    esp32_poll();

    // Bridge.update() gestito automaticamente da Zephyr thread
    delay(1);
}
