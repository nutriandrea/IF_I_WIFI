/*
 * ESP32 CSI Firmware — WiFi CSI Streaming per UNO Q
 *
 * Cattura Channel State Information (CSI) da pacchetti WiFi
 * e li invia in formato CSV via Serial a 921600 baud.
 *
 * Cablaggio ESP32 → UNO Q:
 *   ESP32 GND → UNO Q GND
 *   ESP32 5V  → UNO Q 5V
 *   ESP32 TX  → UNO Q D0
 *   ESP32 RX  → UNO Q D1
 *
 * Formato output:
 *   CSI:<seq>:<rssi>:<noise>:<rate>:<bw>:<sub_count>:<r0,i0,r1,i1,...>
 *
 * Comandi via Serial (tipo ./csi_processor.py):
 *   ping    -> pong:OK
 *   start   -> avvia streaming CSI
 *   stop    -> ferma streaming
 *   status  -> report statistiche
 *
 * WiFi Credentials: modifica WIFI_SSID e WIFI_PASS qui sotto.
 *                   
 * Dipendenze: ESP32 Arduino Core (Tools → Board → ESP32 Dev Module)
 *             Usa ESP-IDF esp_wifi_set_csi() internamente.
 */

#include <WiFi.h>
#include "esp_wifi.h"

// ============================================================
// CONFIG — modifica secrets.h con le tue credenziali WiFi
// ============================================================
#include "secrets.h"

// ============================================================
// Config tecnica
// ============================================================
#define SERIAL_BAUD      921600
#define CSI_QUEUE_SLOTS  4
#define CSI_MAX_SUBCARRIERS 128  // sufficiente per HT40

#define LED_PIN          2  // ESP32 built-in LED

// ============================================================
// Struttura frame CSI
// ============================================================
typedef struct {
    uint16_t seq;
    int8_t rssi;
    int8_t noise_floor;
    uint16_t rate;
    uint8_t bandwidth;
    uint16_t len;
    int8_t data[CSI_MAX_SUBCARRIERS * 2];
} csi_slot_t;

// === Ring buffer ISR-safe ===
static csi_slot_t csi_slots[CSI_QUEUE_SLOTS];
static volatile int csi_head = 0;
static volatile int csi_tail = 0;
static volatile uint16_t csi_seq = 0;
static volatile uint32_t csi_dropped = 0;
static bool streaming = true;

// === CSI Callback (ISR context!) ===
static void IRAM_ATTR csi_callback(void *ctx, wifi_csi_info_t *info) {
    if (!streaming) return;
    if (!info || !info->buf || info->len <= 0) return;

    int next = (csi_head + 1) % CSI_QUEUE_SLOTS;
    if (next == csi_tail) {
        csi_dropped++;
        return;
    }

    csi_slot_t *slot = &csi_slots[csi_head];
    uint16_t copy_len = (info->len > CSI_MAX_SUBCARRIERS * 2)
                        ? CSI_MAX_SUBCARRIERS * 2 : info->len;

    slot->seq = ++csi_seq;
    slot->rssi = info->rx_ctrl.rssi;
    slot->noise_floor = info->rx_ctrl.noise_floor;
    slot->rate = info->rx_ctrl.rate;
    slot->bandwidth = 0;
    slot->len = copy_len;
    memcpy(slot->data, info->buf, copy_len);

    csi_head = next;
}

// === Setup CSI ===
static bool setup_csi() {
    esp_wifi_set_csi_rx_cb(&csi_callback, NULL);

    wifi_csi_config_t csi_config = {
        .lltf_en = 1,
        .htltf_en = 1,
        .stbc_htltf2_en = 0,
        .ltf_merge_en = 1,
        .channel_filter_en = 0,
        .manu_scale = 0,
        .shift = 0,
    };

    esp_err_t err;
    err = esp_wifi_set_csi_config(&csi_config);
    if (err != ESP_OK) return false;

    err = esp_wifi_set_csi(1);
    if (err != ESP_OK) return false;

    return true;
}

// === Gestione comandi seriali ===
static void handle_commands() {
    if (Serial.available() <= 0) return;

    String cmd = Serial.readStringUntil('\n');
    cmd.trim();

    if (cmd == "ping") {
        Serial.println("pong:OK");
    } else if (cmd == "status") {
        Serial.print("status:alive,seq=");
        Serial.print(csi_seq);
        Serial.print(",dropped=");
        Serial.print(csi_dropped);
        Serial.print(",heap=");
        Serial.println(ESP.getFreeHeap());
    } else if (cmd == "start") {
        streaming = true;
        Serial.println("start:OK");
        digitalWrite(LED_PIN, HIGH);
    } else if (cmd == "stop") {
        streaming = false;
        Serial.println("stop:OK");
        digitalWrite(LED_PIN, LOW);
    }
}

// === Output frame CSI su Serial ===
static void output_frames() {
    if (csi_tail == csi_head) return;

    csi_slot_t *slot = &csi_slots[csi_tail];
    int sub_count = slot->len / 2;

    Serial.print("CSI:");
    Serial.print(slot->seq);
    Serial.print(":");
    Serial.print(slot->rssi);
    Serial.print(":");
    Serial.print(slot->noise_floor);
    Serial.print(":");
    Serial.print(slot->rate);
    Serial.print(":");
    Serial.print(slot->bandwidth == 0 ? "20" : "40");
    Serial.print(":");
    Serial.print(sub_count);
    Serial.print(":");
    for (int i = 0; i < slot->len; i++) {
        if (i > 0) Serial.print(",");
        Serial.print(slot->data[i]);
    }
    Serial.println();

    csi_tail = (csi_tail + 1) % CSI_QUEUE_SLOTS;
}

static bool csi_initialized = false;
static unsigned long last_wifi_check = 0;

// ============================================================
// Setup & Loop
// ============================================================
void setup() {
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);

    Serial.begin(SERIAL_BAUD);

    // Segnale di boot: 3 lampeggi
    for (int i = 0; i < 3; i++) {
        digitalWrite(LED_PIN, HIGH);
        delay(100);
        digitalWrite(LED_PIN, LOW);
        delay(100);
    }

    Serial.println("ESP32_CSI_READY");

    // Avvia WiFi NON bloccante — loop() risponde subito ai comandi
    Serial.print("WiFi:connecting...");
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
}

void loop() {
    handle_commands();          // Risponde a "ping" IMMEDIATAMENTE

    // WiFi + CSI init non bloccante (ogni 500ms)
    if (!csi_initialized) {
        unsigned long now = millis();
        if (now - last_wifi_check >= 500) {
            last_wifi_check = now;

            if (WiFi.status() == WL_CONNECTED) {
                csi_initialized = true;
                Serial.println();
                Serial.print("WiFi:OK,");
                Serial.println(WiFi.localIP());
                digitalWrite(LED_PIN, HIGH);

                if (setup_csi()) {
                    Serial.println("CSI:enabled");
                } else {
                    Serial.println("CSI:FAILED");
                }
            } else {
                Serial.print(".");
            }
        }
    }

    // Output CSI solo quando inizializzato
    if (csi_initialized) {
        output_frames();
    }

    delay(1);
}
