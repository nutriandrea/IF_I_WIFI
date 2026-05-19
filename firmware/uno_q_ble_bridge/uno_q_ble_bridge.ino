/*
 * UNO Q BLE Bridge — STM32 side
 *
 * Si connette via BLE all'ESP32 (firmware esp32_csi_ble.ino) e
 * bufferizza i frame CSI, esponendoli al Linux MPU via RPC.
 *
 * BLE: central role, si connette al periferico "ESP32_CSI"
 * usando il Nordic UART Service (NUS).
 *
 * RPC funzioni esposte:
 *   csi_ping()         -> "pong" o errore
 *   csi_count()        -> int (frame bufferizzati)
 *   csi_read_all()     -> string (frame CSI newline-separati, svuota buffer)
 *   csi_clear()        -> true
 *   csi_cmd(cmd)       -> invia comando all'ESP32
 *
 * LED_BUILTIN:
 *   3 lampeggi rapidi  -> boot
 *   1 lungo            -> BLE connesso + bridge OK
 *   2 rapidi + pausa   -> ESP32 non trovata
 *   lampeggio lento    -> scanning BLE in corso
 */

#include <ArduinoBLE.h>
#include <Arduino_RouterBridge.h>

// NUS UUIDs (stessi di esp32_csi_ble.ino)
#define NUS_SERVICE_UUID    "6E400001-B5A3-F393-E0A9-E50E24DCCA9F"
#define NUS_TX_CHAR_UUID    "6E400003-B5A3-F393-E0A9-E50E24DCCA9F"
#define NUS_RX_CHAR_UUID    "6E400002-B5A3-F393-E0A9-E50E24DCCA9F"

#define BLE_DEVICE_NAME     "ESP32_CSI"
#define BLE_SCAN_MS         3000
#define BLE_RECONNECT_MS    10000

#define CSI_BUF_MAX         10
#define CSI_LINE_MAX        4096

static BLEDevice peripheral;
static BLEService nusService;
static BLECharacteristic txChar;
static BLECharacteristic rxChar;
static bool bleConnected = false;
static unsigned long lastReconnect = 0;

static char csi_buffer[CSI_BUF_MAX][CSI_LINE_MAX];
static int csi_buffer_count = 0;

// === LED blink helper ===
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

// === BLE callbacks ===
static void onDisconnected(BLEDevice central) {
    bleConnected = false;
    digitalWrite(LED_BUILTIN, LOW);
}

static void onRXReceived(BLEDevice central, BLECharacteristic characteristic) {
    (void)central;
    (void)characteristic;
}

static void onTXNotified(BLEDevice central, BLECharacteristic characteristic) {
    (void)central;
    if (csi_buffer_count >= CSI_BUF_MAX) return;

    int len = characteristic.valueLength();
    if (len <= 0 || len >= CSI_LINE_MAX) return;

    char buf[CSI_LINE_MAX];
    memcpy(buf, characteristic.value(), len);
    buf[len] = '\0';

    char *line = buf;
    for (int i = 0; i < len; i++) {
        if (buf[i] == '\n') {
            buf[i] = '\0';
            int slen = strlen(line);
            if (slen > 0) {
                strncpy(csi_buffer[csi_buffer_count], line, CSI_LINE_MAX - 1);
                csi_buffer[csi_buffer_count][CSI_LINE_MAX - 1] = '\0';
                csi_buffer_count++;
                if (csi_buffer_count >= CSI_BUF_MAX) break;
            }
            line = buf + i + 1;
        }
    }
}

// === BLE connection ===
static bool ble_scan_and_connect() {
    if (bleConnected) return true;

    BLE.scan(0);

    unsigned long start = millis();
    while (millis() - start < BLE_SCAN_MS) {
        BLEDevice dev = BLE.available();
        if (dev) {
            if (strcmp(dev.localName(), BLE_DEVICE_NAME) == 0) {
                BLE.stopScan();
                peripheral = dev;

                if (peripheral.connect()) {
                    if (peripheral.discoverAttributes()) {
                        nusService = peripheral.service(NUS_SERVICE_UUID);
                        if (nusService) {
                            txChar = nusService.characteristic(NUS_TX_CHAR_UUID);
                            rxChar = nusService.characteristic(NUS_RX_CHAR_UUID);
                            if (txChar && txChar.canSubscribe()) {
                                txChar.setEventHandler(BLEUpdated, onTXNotified);
                                if (txChar.subscribe()) {
                                    bleConnected = true;
                                    digitalWrite(LED_BUILTIN, HIGH);
                                    peripheral.setEventHandler(BLEDisconnected, onDisconnected);
                                    return true;
                                }
                            }
                        }
                    }
                    peripheral.disconnect();
                }
            }
        }
    }

    BLE.stopScan();
    return false;
}

static void ble_try_reconnect() {
    if (bleConnected) return;
    unsigned long now = millis();
    if (now - lastReconnect < BLE_RECONNECT_MS) return;
    lastReconnect = now;
    ble_scan_and_connect();
}

static bool ble_send_cmd(const char* cmd) {
    if (!bleConnected || !rxChar) return false;
    rxChar.writeValue(cmd, strlen(cmd));
    return true;
}

// === RPC functions ===
String csi_ping() {
    if (!bleConnected) {
        if (ble_scan_and_connect()) {
            return String("pong:OK");
        }
        return String("ESP32_NOT_CONNECTED");
    }

    ble_send_cmd("ping");

    unsigned long deadline = millis() + 500;
    while (millis() < deadline) {
        BLE.poll();
        for (int i = 0; i < csi_buffer_count; i++) {
            if (strstr(csi_buffer[i], "pong") != NULL) {
                String resp = String(csi_buffer[i]);
                csi_buffer_count = 0;
                return String("pong:") + resp;
            }
        }
        delay(10);
    }

    return String("ESP32_NO_RESPONSE");
}

int csi_count() {
    return csi_buffer_count;
}

String csi_read_all() {
    String result;
    for (int i = 0; i < csi_buffer_count; i++) {
        if (i > 0) result += "\n";
        result += String(csi_buffer[i]);
    }
    csi_buffer_count = 0;
    return result;
}

bool csi_clear() {
    csi_buffer_count = 0;
    return true;
}

String csi_cmd(const String& cmd) {
    if (!bleConnected) return String("NOT_CONNECTED");
    ble_send_cmd(cmd.c_str());

    unsigned long deadline = millis() + 1000;
    String resp;
    while (millis() < deadline) {
        BLE.poll();
        if (csi_buffer_count > 0) {
            resp += String(csi_buffer[0]);
            if (resp.length() > 0 && resp[resp.length() - 1] == '\n') {
                break;
            }
        }
        delay(10);
    }
    csi_buffer_count = 0;
    return resp.length() > 0 ? resp : String("OK");
}

// === Bridge init ===
static int register_methods() {
    bool ok = true;
    ok &= Bridge.provide("csi_ping", csi_ping);
    ok &= Bridge.provide("csi_count", csi_count);
    ok &= Bridge.provide_safe("csi_read_all", csi_read_all);
    ok &= Bridge.provide("csi_clear", csi_clear);
    ok &= Bridge.provide_safe("csi_cmd", csi_cmd);
    return ok ? 2 : 3;
}

static int try_bridge_init() {
    if (Bridge.is_started()) return register_methods();
    if (!Bridge.begin()) return 1;
    return register_methods();
}

// === Setup & Loop ===
void setup() {
    pinMode(LED_BUILTIN, OUTPUT);
    digitalWrite(LED_BUILTIN, LOW);

    blink_start(3, 80, 80);

    if (!BLE.begin()) {
        blink_start(5, 80, 80);
        return;
    }

    BLE.setEventHandler(BLEDisconnected, onDisconnected);

    bool esp32_ok = ble_scan_and_connect();

    int state = try_bridge_init();
    if (state == 2 && esp32_ok) {
        blink_start(1, 500, 0);
    } else if (!esp32_ok) {
        blink_start(2, 80, 80);
    }

    lastReconnect = millis();
}

void loop() {
    blink_tick();
    BLE.poll();
    ble_try_reconnect();
    delay(1);
}
