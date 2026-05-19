/*
 * ESP32 CSI BLE — WiFi CSI Streaming via Bluetooth BLE
 *
 * Cattura CSI da pacchetti WiFi e li invia via BLE (Bluetooth Low Energy)
 * usando il Nordic UART Service (NUS). Compatibile con ArduinoBLE,
 * nRF Connect, e bridge UNO Q.
 *
 * Abbinamento:
 *   1. Carica questo sketch sull'ESP32 (Board: ESP32 Dev Module)
 *   2. Il nome BLE trasmesso e' "ESP32_CSI"
 *   3. L'UNO Q (con firmware uno_q_ble_bridge) si connette automaticamente
 *
 * Formato output (stesso del firmware UART):
 *   CSI:<seq>:<rssi>:<noise>:<rate>:<bw>:<sub_count>:<r0,i0,r1,i1,...>
 *
 * Comandi via BLE:
 *   ping    -> pong:OK
 *   start   -> avvia streaming CSI
 *   stop    -> ferma streaming
 *   status  -> report statistiche
 *
 * Dipendenze:
 *   ESP32 Arduino Core (Tools -> Board -> ESP32 Dev Module)
 *   BLEDevice library (built-in con ESP32 Arduino Core)
 */

#include <WiFi.h>
#include "esp_wifi.h"
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"

#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEServer.h>
#include <BLE2902.h>

// ============================================================
// CONFIG — modifica secrets.h con le tue credenziali WiFi
// ============================================================
#include "secrets.h"

// Supporto multi-AP
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
// Pin definitions + forward declarations
// ============================================================
#define LED_PIN          2
static void process_command(const char* cmd);
static void ble_send(const char* buf, int len);

// ============================================================
// BLE — Nordic UART Service (NUS)
// ============================================================

#define BLE_DEVICE_NAME     "ESP32_CSI"
// UUIDs standard NUS (compatibile con ArduinoBLE, nRF Connect, ecc.)
#define NUS_SERVICE_UUID    "6E400001-B5A3-F393-E0A9-E50E24DCCA9F"
#define NUS_TX_CHAR_UUID    "6E400003-B5A3-F393-E0A9-E50E24DCCA9F"  // notify
#define NUS_RX_CHAR_UUID    "6E400002-B5A3-F393-E0A9-E50E24DCCA9F"  // write

static BLEServer*        pServer       = nullptr;
static BLECharacteristic* pTxCharacteristic = nullptr;
static bool               deviceConnected  = false;
static bool               oldDeviceConnected = false;

// Ring buffer per output BLE (accumula righe prima di notify)
#define BLE_TX_BUF_SIZE    (512)
static char               ble_tx_buf[BLE_TX_BUF_SIZE];
static int                ble_tx_len = 0;

class MyServerCallbacks : public BLEServerCallbacks {
    void onConnect(BLEServer* p) override {
        deviceConnected = true;
        digitalWrite(LED_PIN, HIGH);
        Serial.println("BLE:connected");
    }
    void onDisconnect(BLEServer* p) override {
        deviceConnected = false;
        digitalWrite(LED_PIN, LOW);
        Serial.println("BLE:disconnected");
        // Restart advertising
        p->startAdvertising();
    }
};

class MyCallbacks : public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic* pCharacteristic) override {
        std::string rxValue = pCharacteristic->getValue();
        if (rxValue.empty()) return;

        while (!rxValue.empty() && (rxValue.back() == '\n' || rxValue.back() == '\r'))
            rxValue.pop_back();

        process_command(rxValue.c_str());
    }
};

static void ble_send(const char* buf, int len) {
    if (!deviceConnected || !pTxCharacteristic) return;

    // Se il buffer e' piccolo, manda diretto
    if (len <= BLE_TX_BUF_SIZE / 2) {
        pTxCharacteristic->setValue((uint8_t*)buf, len);
        pTxCharacteristic->notify();
        return;
    }

    // Altrimenti spezzetta
    int offset = 0;
    while (offset < len) {
        int chunk = min(len - offset, BLE_TX_BUF_SIZE / 2);
        pTxCharacteristic->setValue((uint8_t*)(buf + offset), chunk);
        pTxCharacteristic->notify();
        delay(2);  // breath
        offset += chunk;
    }
}

// ============================================================
// Config tecnica
// ============================================================
#define CSI_QUEUE_SLOTS  4
#define CSI_MAX_SUBCARRIERS 128

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

static csi_slot_t csi_slots[CSI_QUEUE_SLOTS];
static volatile int csi_head = 0;
static volatile int csi_tail = 0;
static volatile uint16_t csi_seq = 0;
static volatile uint32_t csi_dropped = 0;
static bool streaming = true;

// === CSI Callback ===
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

    if (esp_wifi_set_csi_config(&csi_config) != ESP_OK) return false;
    if (esp_wifi_set_csi(1) != ESP_OK) return false;
    return true;
}

// === Gestione comandi ===
static void process_command(const char* cmd) {
    if (strcmp(cmd, "ping") == 0) {
        ble_send("pong:OK\n", 7);
        Serial.println("CMD:ping -> pong");
    } else if (strcmp(cmd, "status") == 0) {
        char buf[128];
        int n = snprintf(buf, sizeof(buf),
                 "status:alive,seq=%u,dropped=%u,heap=%u\n",
                 csi_seq, csi_dropped, ESP.getFreeHeap());
        ble_send(buf, n);
        Serial.printf("CMD:status -> seq=%u dropped=%u\n", csi_seq, csi_dropped);
    } else if (strcmp(cmd, "start") == 0) {
        streaming = true;
        ble_send("start:OK\n", 8);
        Serial.println("CMD:start -> OK");
    } else if (strcmp(cmd, "stop") == 0) {
        streaming = false;
        ble_send("stop:OK\n", 7);
        Serial.println("CMD:stop -> OK");
    } else if (strlen(cmd) > 0) {
        char buf[64];
        int n = snprintf(buf, sizeof(buf), "unknown:%s\n", cmd);
        ble_send(buf, n);
    }
}

static void handle_commands() {
    if (Serial.available() > 0) {
        String cmd = Serial.readStringUntil('\n');
        cmd.trim();
        if (cmd.length() > 0) process_command(cmd.c_str());
    }
}

// === Output frame CSI via BLE ===
static void output_frames() {
    if (csi_tail == csi_head) return;

    csi_slot_t *slot = &csi_slots[csi_tail];
    int sub_count = slot->len / 2;

    // Costruisce la linea CSI
    char buf[4096];
    int pos = 0;

    pos += snprintf(buf + pos, sizeof(buf) - pos,
                    "CSI:%u:%d:%d:%u:%s:%d:",
                    slot->seq, slot->rssi, slot->noise_floor,
                    slot->rate, slot->bandwidth == 0 ? "20" : "40",
                    sub_count);

    for (int i = 0; i < slot->len && pos < (int)sizeof(buf) - 12; i++) {
        if (i > 0) buf[pos++] = ',';
        pos += snprintf(buf + pos, sizeof(buf) - pos, "%d", slot->data[i]);
    }
    buf[pos++] = '\n';
    buf[pos] = '\0';

    // Output: BLE + USB serial debug
    ble_send(buf, pos);
    Serial.write((uint8_t*)buf, pos);

    csi_tail = (csi_tail + 1) % CSI_QUEUE_SLOTS;
}

// === Multi-AP ===
static bool switch_ap(int ap_idx) {
    if (ap_idx < 0 || ap_idx >= NUM_APS) return false;
    if (ap_idx == current_ap) return true;

    Serial.printf("AP:switching %d -> %d\n", current_ap, ap_idx);
    WiFi.disconnect(false, true);
    delay(100);
    current_ap = ap_idx;
    WiFi.begin(AP_CREDS[current_ap].ssid, AP_CREDS[current_ap].pass);
    Serial.printf("AP:connecting %s...\n", AP_CREDS[current_ap].ssid);
    return true;
}

// ============================================================
// Setup BLE
// ============================================================
static bool setup_ble() {
    BLEDevice::init(BLE_DEVICE_NAME);
    pServer = BLEDevice::createServer();
    pServer->setCallbacks(new MyServerCallbacks());

    BLEService *pService = pServer->createService(NUS_SERVICE_UUID);

    // TX Characteristic (ESP32 -> Central, notify)
    pTxCharacteristic = pService->createCharacteristic(
        NUS_TX_CHAR_UUID,
        BLECharacteristic::PROPERTY_NOTIFY
    );
    pTxCharacteristic->addDescriptor(new BLE2902());

    // RX Characteristic (Central -> ESP32, write)
    BLECharacteristic *pRxCharacteristic = pService->createCharacteristic(
        NUS_RX_CHAR_UUID,
        BLECharacteristic::PROPERTY_WRITE_NR
    );
    pRxCharacteristic->setCallbacks(new MyCallbacks());

    pService->start();
    pServer->getAdvertising()->start();

    Serial.println("BLE:advertising...");
    return true;
}

// ============================================================
// Setup & Loop
// ============================================================
static bool csi_initialized = false;
static unsigned long last_status_report = 0;

void setup() {
    WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0);

    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);

    Serial.begin(115200);
    delay(500);  // Stabilizzazione

    // BLE
    setup_ble();

    // Segnale di boot
    for (int i = 0; i < 3; i++) {
        digitalWrite(LED_PIN, HIGH); delay(100);
        digitalWrite(LED_PIN, LOW);  delay(100);
    }

    Serial.println("\nESP32_CSI_BLE_READY");
    Serial.println("  Cerca \"ESP32_CSI\" via BLE dal computer o UNO Q");

    // Avvia WiFi
    Serial.print("WiFi:connecting...");
    WiFi.mode(WIFI_STA);
    WiFi.setTxPower(WIFI_POWER_8_5dBm);
    WiFi.begin(AP_CREDS[current_ap].ssid, AP_CREDS[current_ap].pass);
}

void loop() {
    handle_commands();

    // CSI init
    if (!csi_initialized && WiFi.isConnected()) {
        if (setup_csi()) {
            Serial.println("CSI:enabled");
            digitalWrite(LED_PIN, HIGH);
            csi_initialized = true;
        } else {
            Serial.println("CSI:ERROR");
            delay(1000);
        }
    }

    // Output frame CSI via BLE
    if (csi_initialized) {
        output_frames();

        unsigned long now = millis();
        if (now - last_status_report > 10000) {
            last_status_report = now;
            Serial.printf("stats:seq=%u,dropped=%u,connected=%s\n",
                          csi_seq, csi_dropped,
                          deviceConnected ? "yes" : "no");
        }
    }

    // Multi-AP switch
    if (NUM_APS > 1 && csi_initialized) {
        unsigned long elapsed = millis() - ap_stream_start;
        if (elapsed > AP_CAPTURE_SECONDS * 1000UL) {
            int next_ap = (current_ap + 1) % NUM_APS;
            switch_ap(next_ap);
            ap_stream_start = millis();
            delay(500);
        }
    }

    // Disconnection handling
    if (!deviceConnected && oldDeviceConnected) {
        delay(500);
        pServer->startAdvertising();
        Serial.println("BLE:restart advertising");
        oldDeviceConnected = deviceConnected;
    }
    if (deviceConnected && !oldDeviceConnected) {
        oldDeviceConnected = true;
    }

    delay(1);
}
