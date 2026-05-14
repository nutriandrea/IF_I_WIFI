/*
 * ESP32 CSI Collector — Active Station mode
 *
 * Lo sketch:
 *  1. Connette l'ESP32 in WIFI_STA al tuo AP (vedi secrets.h)
 *  2. Manda ping ICMP al gateway a frequenza fissa (default 100 Hz)
 *  3. Registra una callback CSI che, per OGNI pacchetto ricevuto, stampa
 *     una riga CSV su Serial (USB)
 *
 * Compatibile con il formato del tool di S. M. Hernandez
 *   https://stevenmhernandez.github.io/ESP32-CSI-Tool/
 * cosi che i suoi script di analisi (Python/MATLAB) funzionino out-of-the-box.
 *
 * Formato output (una riga per sample):
 *   CSI_DATA,STA,<src_mac>,<rssi>,<rate>,<sig_mode>,<mcs>,<bandwidth>,
 *   <smoothing>,<not_sounding>,<aggregation>,<stbc>,<fec_coding>,<sgi>,
 *   <noise_floor>,<ampdu_cnt>,<channel>,<secondary_channel>,
 *   <local_timestamp>,<ant>,<sig_len>,<rx_state>,<real_time_set>,
 *   <real_timestamp_us>,<len>,[<i0> <q0> <i1> <q1> ...]
 *
 * Build (Arduino IDE):
 *   - Board: "ESP32 Dev Module" (arduino-esp32 core >= 2.0.4)
 *   - PSRAM: Disabled (non serve)
 *   - Upload Speed: 921600
 *   - Partition Scheme: Default 4MB
 *   - Tools > Core Debug Level: None
 *
 * Build (arduino-cli):
 *   arduino-cli compile -b esp32:esp32:esp32 esp32_csi_collector
 *   arduino-cli upload  -b esp32:esp32:esp32 -p /dev/cu.usbserial-XXXX esp32_csi_collector
 *
 * Prima dell'upload: copia secrets.h.example in secrets.h e metti SSID/PASS.
 */

#include <WiFi.h>
#include "esp_wifi.h"
#include "esp_wifi_types.h"
#include "ping/ping_sock.h"
#include "lwip/inet.h"

#include "secrets.h"   // definisce WIFI_SSID e WIFI_PASS

// ==========================================================================
// Config
// ==========================================================================
#ifndef CSI_SERIAL_BAUD
#define CSI_SERIAL_BAUD 921600
#endif

// Frequenza ping (Hz). 100 Hz e' un buon compromesso CSI/CPU per ESP32 classic.
// Se vedi "ping send error" abbassa a 50.
#ifndef PING_HZ
#define PING_HZ 100
#endif

// LED on-board (GPIO 2 sul DevKit v1)
#ifndef LED_PIN
#define LED_PIN 2
#endif

// Filtro: stampa CSI solo dei pacchetti provenienti dall'AP a cui siamo
// connessi (riduce rumore da pacchetti broadcast/altri AP).
#define CSI_ONLY_FROM_AP 1

// ==========================================================================
// Stato globale
// ==========================================================================
static uint8_t ap_bssid[6] = {0};
static volatile uint32_t csi_count = 0;
static volatile uint32_t ping_count = 0;

// ==========================================================================
// Callback CSI — invocata in interrupt-like context. Mantienila veloce.
// ==========================================================================
static void IRAM_ATTR wifi_csi_cb(void *ctx, wifi_csi_info_t *info) {
    if (!info || !info->buf) return;

#if CSI_ONLY_FROM_AP
    // Stampa solo se il pacchetto viene dal nostro AP
    if (memcmp(info->mac, ap_bssid, 6) != 0) return;
#endif

    csi_count++;
    wifi_pkt_rx_ctrl_t *rx = &info->rx_ctrl;

    // Costruisci la riga CSV. Usa Serial.printf per efficienza.
    Serial.printf("CSI_DATA,STA,"
                  "%02x:%02x:%02x:%02x:%02x:%02x,"   // MAC
                  "%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,"   // rssi..stbc
                  "%d,%d,%d,%d,%d,%u,%d,%d,%d,%d,"   // fec..real_time_set
                  "%lld,%d,[",                       // real_ts_us, len
                  info->mac[0], info->mac[1], info->mac[2],
                  info->mac[3], info->mac[4], info->mac[5],
                  rx->rssi, rx->rate, rx->sig_mode, rx->mcs,
                  rx->cwb, rx->smoothing, rx->not_sounding,
                  rx->aggregation, rx->stbc, rx->fec_coding,
                  rx->sgi, rx->noise_floor, rx->ampdu_cnt,
                  rx->channel, rx->secondary_channel,
                  rx->timestamp, rx->ant, rx->sig_len, rx->rx_state,
                  1,                              // real_time_set (host fa override)
                  (long long)esp_timer_get_time(),
                  info->len);

    int8_t *buf = info->buf;
    for (int i = 0; i < info->len; i++) {
        Serial.print(buf[i]);
        if (i < info->len - 1) Serial.print(' ');
    }
    Serial.println(']');
}

// ==========================================================================
// Ping callbacks (servono solo per contare ping riusciti)
// ==========================================================================
static void on_ping_success(esp_ping_handle_t hdl, void *args) {
    ping_count++;
    // Heartbeat LED ogni 100 ping
    if ((ping_count % 100) == 0) {
        digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    }
}

static void on_ping_timeout(esp_ping_handle_t hdl, void *args) {
    // silente — il CSI si raccoglie comunque dai beacon
}

// ==========================================================================
// CSI setup
// ==========================================================================
static bool csi_setup() {
    wifi_csi_config_t csi_cfg = {};
    csi_cfg.lltf_en           = true;
    csi_cfg.htltf_en          = true;
    csi_cfg.stbc_htltf2_en    = true;
    csi_cfg.ltf_merge_en      = true;
    csi_cfg.channel_filter_en = false;   // false = piu sample, true = piu pulito
    csi_cfg.manu_scale        = false;
    csi_cfg.shift             = 0;

    if (esp_wifi_set_csi_config(&csi_cfg) != ESP_OK) return false;
    if (esp_wifi_set_csi_rx_cb(&wifi_csi_cb, NULL) != ESP_OK) return false;
    if (esp_wifi_set_csi(true) != ESP_OK) return false;
    return true;
}

// ==========================================================================
// Ping setup (verso il gateway)
// ==========================================================================
static esp_ping_handle_t ping_handle = NULL;

static bool ping_setup() {
    IPAddress gw = WiFi.gatewayIP();
    if (gw == IPAddress(0,0,0,0)) return false;

    ip_addr_t target = {};
    target.type = IPADDR_TYPE_V4;
    target.u_addr.ip4.addr = (uint32_t)gw;

    esp_ping_config_t cfg = ESP_PING_DEFAULT_CONFIG();
    cfg.target_addr = target;
    cfg.count       = ESP_PING_COUNT_INFINITE;
    cfg.interval_ms = 1000 / PING_HZ;
    cfg.timeout_ms  = 100;
    cfg.task_stack_size = 4096;

    esp_ping_callbacks_t cbs = {};
    cbs.on_ping_success = on_ping_success;
    cbs.on_ping_timeout = on_ping_timeout;
    cbs.on_ping_end     = NULL;
    cbs.cb_args         = NULL;

    if (esp_ping_new_session(&cfg, &cbs, &ping_handle) != ESP_OK) return false;
    if (esp_ping_start(ping_handle) != ESP_OK) return false;
    return true;
}

// ==========================================================================
// Setup
// ==========================================================================
void setup() {
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);

    Serial.begin(CSI_SERIAL_BAUD);
    delay(200);
    Serial.println();
    Serial.println("# ESP32 CSI Collector starting");
    Serial.printf("# SSID=%s  ping_hz=%d  baud=%d\n",
                  WIFI_SSID, PING_HZ, CSI_SERIAL_BAUD);

    // Connessione WiFi
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);                 // CSI ha bisogno di RX attivo costante
    WiFi.begin(WIFI_SSID, WIFI_PASS);

    Serial.print("# connecting");
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED) {
        delay(250);
        Serial.print('.');
        digitalWrite(LED_PIN, !digitalRead(LED_PIN));
        if (millis() - t0 > 30000) {
            Serial.println("\n# TIMEOUT connecting to AP. Reboot.");
            delay(2000);
            ESP.restart();
        }
    }
    Serial.println();
    Serial.printf("# connected. ip=%s gw=%s rssi=%d ch=%d\n",
                  WiFi.localIP().toString().c_str(),
                  WiFi.gatewayIP().toString().c_str(),
                  WiFi.RSSI(), WiFi.channel());

    // Salva BSSID dell'AP per filtrare CSI
    memcpy(ap_bssid, WiFi.BSSID(), 6);
    Serial.printf("# ap_bssid=%02x:%02x:%02x:%02x:%02x:%02x\n",
                  ap_bssid[0], ap_bssid[1], ap_bssid[2],
                  ap_bssid[3], ap_bssid[4], ap_bssid[5]);

    if (!csi_setup()) {
        Serial.println("# ERROR: csi_setup failed");
        while (1) { digitalWrite(LED_PIN, !digitalRead(LED_PIN)); delay(100); }
    }
    Serial.println("# CSI enabled");

    if (!ping_setup()) {
        Serial.println("# WARN: ping_setup failed — CSI verra' raccolto solo dai beacon");
    } else {
        Serial.printf("# ping started @ %d Hz\n", PING_HZ);
    }

    Serial.println("# stream begin (CSI_DATA rows follow)");
    digitalWrite(LED_PIN, HIGH);
}

// ==========================================================================
// Loop — stats ogni 10s (commenti che iniziano con '#': il receiver li ignora)
// ==========================================================================
void loop() {
    static uint32_t last_stats = 0;
    static uint32_t last_csi = 0;
    static uint32_t last_ping = 0;

    uint32_t now = millis();
    if (now - last_stats >= 10000) {
        uint32_t csi_d  = csi_count  - last_csi;
        uint32_t ping_d = ping_count - last_ping;
        last_csi = csi_count;
        last_ping = ping_count;
        last_stats = now;

        if (WiFi.status() != WL_CONNECTED) {
            Serial.println("# WARN: WiFi disconnected, restarting");
            delay(500);
            ESP.restart();
        }

        Serial.printf("# stats: csi=%u/10s (%.1f Hz)  ping=%u/10s  rssi=%d\n",
                      csi_d, csi_d / 10.0f, ping_d, WiFi.RSSI());
    }
    delay(50);
}
