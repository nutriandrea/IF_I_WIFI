/*
 * ESP32 CSI Firmware — WiFi CSI Streaming per UNO Q
 *
 * Cattura Channel State Information (CSI) da pacchetti WiFi
 * e li invia in formato CSV via Serial a 115200 baud.
 *
 * Cablaggio ESP32 → UNO Q:
 *   ESP32 GND → UNO Q GND
 *   ESP32 5V  → UNO Q 5V
 *   ESP32 TX  → UNO Q D0
 *   ESP32 RX  → UNO Q D1
 *
 * Formato output:
 *   CSI:<seq>:<mac_12hex>:<rssi>:<noise>:<rate>:<bw>:<sub_count>:<r0,i0,r1,i1,...>
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
#include <WiFiUdp.h>
#include "esp_wifi.h"
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"

// ============================================================
// CONFIG — modifica secrets.h con le tue credenziali WiFi
// ============================================================
#include "secrets.h"

// Modalità AP: scommenta per far diventare ESP32 un access point.
// I PC si connettono direttamente all'ESP32 e lo pingano.
// Utile per demo: ogni PC ha MAC diverso nel CSI.
// #define CSI_AP_MODE
#ifdef CSI_AP_MODE
#define AP_SSID "ESP32-CSI"
#define AP_PASS "csi12345"
#define AP_IP 192, 168, 4, 1
#endif

// Supporto multi-AP: se NUM_APS > 1, il firmware cicla tra AP_LIST
#ifndef NUM_APS
#define NUM_APS 1
#endif

#ifndef AP_CAPTURE_SECONDS
#define AP_CAPTURE_SECONDS 3
#endif

#ifndef AP_LIST
#define AP_LIST {{WIFI_SSID, WIFI_PASS}}
#endif

typedef struct {
    const char* ssid;
    const char* pass;
} ap_cred_t;

static const ap_cred_t AP_CREDS[NUM_APS] = AP_LIST;
static int current_ap = 0;
static unsigned long ap_stream_start = 0;

// ============================================================
// Config tecnica
// ============================================================
#define SERIAL_BAUD      115200   // 115200 e' il piu' affidabile con CH340 + cavi non perfetti
#define CSI_QUEUE_SLOTS  4
#define CSI_MAX_SUBCARRIERS 128  // sufficiente per HT40

#define LED_PIN          2  // ESP32 built-in LED

// === UDP streaming (ispirato da RuView ADR-018) ===
#define UDP_TARGET_PORT  5005
#define UDP_FRAME_MAGIC  0xC5110001   // ADR-018 magic number
#define UDP_HEADER_SIZE  20
#define UDP_MAX_FRAME    (UDP_HEADER_SIZE + CSI_MAX_SUBCARRIERS * 2)
#define MAX_OUTPUT_HZ    50
#define MIN_OUTPUT_INTERVAL_US (1000000 / MAX_OUTPUT_HZ)

static WiFiUDP udp_client;
static IPAddress udp_target_ip(192, 168, 1, 100);  // default, sovrascrivibile via comando
static bool udp_ready = false;

enum OutputMode { OUT_SERIAL, OUT_UDP };
static OutputMode out_mode = OUT_SERIAL;  // default serial (retrocompat)
static unsigned long last_output_us = 0;

// ============================================================
// Struttura frame CSI
// ============================================================
typedef struct {
    uint16_t seq;
    uint8_t mac[6];       // MAC del trasmettitore
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
    memcpy(slot->mac, info->mac, 6);
    slot->rssi = info->rx_ctrl.rssi;
    slot->noise_floor = info->rx_ctrl.noise_floor;
    slot->rate = info->rx_ctrl.rate;
    slot->bandwidth = 0;
    slot->len = copy_len;
    memcpy(slot->data, info->buf, copy_len);

    csi_head = next;
}

// === Setup CSI ===
static wifi_csi_config_t csi_config = {
    .lltf_en = 1,
    .htltf_en = 1,
    .stbc_htltf2_en = 0,
    .ltf_merge_en = 1,
    .channel_filter_en = 0,
    .manu_scale = 0,
    .shift = 0,
};

static bool setup_csi() {
    esp_wifi_set_csi_rx_cb(&csi_callback, NULL);

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
        Serial.print(ESP.getFreeHeap());
        Serial.print(",out=");
        Serial.print(out_mode == OUT_UDP ? "udp" : "serial");
        Serial.print(",rate=");
        Serial.print(MAX_OUTPUT_HZ);
        Serial.print("hz,streaming=");
        Serial.println(streaming ? "ON" : "OFF");
    } else if (cmd == "start") {
        streaming = true;
        Serial.println("start:OK");
        digitalWrite(LED_PIN, HIGH);
    } else if (cmd == "stop") {
        streaming = false;
        Serial.println("stop:OK");
        digitalWrite(LED_PIN, LOW);
    } else if (cmd.startsWith("$ serial")) {
        out_mode = OUT_SERIAL;
        Serial.println("out:serial");
    } else if (cmd.startsWith("$ udp")) {
        if (WiFi.status() != WL_CONNECTED
            #ifdef CSI_AP_MODE
            && WiFi.softAPgetStationNum() == 0
            #endif
        ) {
            Serial.println("out:udp_no_wifi");
            return;
        }
        String ip = cmd.substring(5);
        ip.trim();
        if (ip.length() > 0) {
            udp_target_ip.fromString(ip);
        }
        if (!udp_ready) {
            udp_client.begin(UDP_TARGET_PORT);
            udp_ready = true;
        }
        out_mode = OUT_UDP;
        Serial.print("out:udp ");
        Serial.println(udp_target_ip);
    } else if (cmd.startsWith("$ rate ")) {
        // Nota: rate limit è compile-time via MAX_OUTPUT_HZ
        Serial.print("rate:");
        Serial.print(MAX_OUTPUT_HZ);
        Serial.println("hz");
    }
}

// === Helper: stampa MAC come 12 cifre esadecimali ===
static void print_mac(const uint8_t *mac) {
    for (int i = 0; i < 6; i++) {
        if (mac[i] < 0x10) Serial.print("0");
        Serial.print(mac[i], HEX);
    }
}

// === Output frame seriale (formato testo, retrocompatibile) ===
static void output_frame_serial(csi_slot_t *slot) {
    int sub_count = slot->len / 2;

    Serial.print("CSI:");
    Serial.print(slot->seq);
    Serial.print(":");
    print_mac(slot->mac);
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
}

// === Output frame UDP (formato binario ADR-018) ===
// Layout:
//   [0..3]   Magic: 0xC5110001 (LE u32)
//   [4]      Node ID (0-255)
//   [5]      Numero antenne
//   [6..7]   Numero subcarrier (LE u16)
//   [8..11]  Frequenza MHz (LE u32)
//   [12..15] Sequence number (LE u32)
//   [16]     RSSI (i8)
//   [17]     Noise floor (i8)
//   [18..19] Reserved (zero)
//   [20..]   I/Q pairs (int8_t, per-subcarrier)
static void output_frame_udp(csi_slot_t *slot) {
    if (!udp_ready) return;

    uint8_t buf[UDP_MAX_FRAME];
    uint16_t n_sub = slot->len / 2;
    size_t frame_size = UDP_HEADER_SIZE + slot->len;
    if (frame_size > UDP_MAX_FRAME) frame_size = UDP_MAX_FRAME;

    // Magic
    uint32_t magic = UDP_FRAME_MAGIC;
    memcpy(&buf[0], &magic, 4);
    // Node ID
    buf[4] = 0;
    // Numero antenne
    buf[5] = 1;
    // Numero subcarrier
    memcpy(&buf[6], &n_sub, 2);
    // Frequenza (default 2412 MHz)
    uint32_t freq = 2412;
    memcpy(&buf[8], &freq, 4);
    // Sequence number
    memcpy(&buf[12], &slot->seq, 4);
    // RSSI / Noise floor
    buf[16] = (uint8_t)(int8_t)slot->rssi;
    buf[17] = (uint8_t)(int8_t)slot->noise_floor;
    // Reserved
    buf[18] = 0;
    buf[19] = 0;
    // I/Q data
    memcpy(&buf[UDP_HEADER_SIZE], slot->data, slot->len);

    udp_client.beginPacket(udp_target_ip, UDP_TARGET_PORT);
    udp_client.write(buf, frame_size);
    udp_client.endPacket();
}

// === Output con rate limiting + dispatch mode ===
static void drain_output() {
    if (csi_tail == csi_head) return;

    // Rate limiting: max MAX_OUTPUT_HZ frames/sec
    unsigned long now = micros();
    if (now - last_output_us < MIN_OUTPUT_INTERVAL_US) return;
    last_output_us = now;

    csi_slot_t *slot = &csi_slots[csi_tail];

    if (out_mode == OUT_UDP) {
        output_frame_udp(slot);
    } else {
        output_frame_serial(slot);
    }

    csi_tail = (csi_tail + 1) % CSI_QUEUE_SLOTS;
}

static bool csi_initialized = false;
static unsigned long last_wifi_check = 0;

// ============================================================
// Setup & Loop
// ============================================================
void setup() {
    // ----------------------------------------------------------
    // Hardening per alimentazione marginale (USB hub, cavo lungo, ecc.)
    //  - Disabilita il brownout detector hardware: evita reset spuri
    //    sui picchi di corrente del WiFi quando il VCC scende sotto ~2.7V.
    //  - Ritardo di stabilizzazione prima di toccare il radio.
    //  - WiFi TX power ridotta da 19.5dBm (default) a 8.5dBm: ~3-4x
    //    meno corrente di picco in trasmissione, range comunque ampio.
    // Se l'ESP32 risulta stabile con alimentazione diretta dal Mac,
    // queste linee non hanno effetti collaterali significativi.
    // ----------------------------------------------------------
    WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0);

    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);

    Serial.begin(SERIAL_BAUD);

    // Segnale di boot: 3 lampeggi (anche utile come "rampa" lenta verso WiFi)
    for (int i = 0; i < 3; i++) {
        digitalWrite(LED_PIN, HIGH);
        delay(100);
        digitalWrite(LED_PIN, LOW);
        delay(100);
    }

    Serial.println("ESP32_CSI_READY");

    // Stabilizzazione alimentazione prima del WiFi
    delay(500);

    // Avvia WiFi NON bloccante — loop() risponde subito ai comandi
#ifdef CSI_AP_MODE
    WiFi.mode(WIFI_AP);
    IPAddress local_ip(AP_IP);
    IPAddress gateway(AP_IP);
    IPAddress subnet(255, 255, 255, 0);
    WiFi.softAPConfig(local_ip, gateway, subnet);
    WiFi.softAP(AP_SSID, AP_PASS);
    Serial.print("WiFi:AP_IP=");
    Serial.print(WiFi.softAPIP());
    Serial.print(" ssid=");
    Serial.println(AP_SSID);
#else
    Serial.print("WiFi:connecting...");
    WiFi.mode(WIFI_STA);
    // Riduce la potenza TX per stare nei limiti USB (8.5dBm ≈ 7mW).
    // Range tipico in casa: ancora sufficiente per stare connessi all'AP.
    WiFi.setTxPower(WIFI_POWER_8_5dBm);
    WiFi.begin(AP_CREDS[current_ap].ssid, AP_CREDS[current_ap].pass);
#endif
}

void loop() {
    handle_commands();          // Risponde a "ping" IMMEDIATAMENTE

    // Heartbeat di vita (1 Hz, indipendente dal WiFi). Se vedi questi
    // tick il chip e' vivo. Se NON li vedi, il chip e' in reset loop
    // o l'upload non e' andato a buon fine.
    static unsigned long last_heartbeat = 0;
    unsigned long now_hb = millis();
    if (now_hb - last_heartbeat >= 1000) {
        last_heartbeat = now_hb;
        Serial.print("HB:");
        Serial.print(now_hb / 1000);
        Serial.print("s wifi=");
        Serial.print(WiFi.status());
        Serial.print(" heap=");
        Serial.println(ESP.getFreeHeap());
    }

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
                ap_stream_start = millis();

                if (NUM_APS > 1) {
                    Serial.print("AP:");
                    Serial.println(current_ap);
                }

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
        drain_output();

        // Multi-AP: switch al prossimo AP dopo AP_CAPTURE_SECONDS
        if (NUM_APS > 1) {
            unsigned long elapsed = millis() - ap_stream_start;
            if (elapsed >= (AP_CAPTURE_SECONDS * 1000UL)) {
                esp_wifi_set_csi(false);  // disable CSI
                WiFi.disconnect(true);
                csi_initialized = false;

                current_ap = (current_ap + 1) % NUM_APS;

                Serial.print("AP_SWITCH:");
                Serial.println(current_ap);

                // Avvia connessione al nuovo AP immediatamente
                WiFi.begin(AP_CREDS[current_ap].ssid, AP_CREDS[current_ap].pass);

                last_wifi_check = 0;  // forza check immediato
                digitalWrite(LED_PIN, LOW);
            }
        }
    }

    delay(1);
}
