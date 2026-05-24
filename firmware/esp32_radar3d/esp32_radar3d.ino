/*
 * ESP32 Radar 3D — Cross-Ping CSI Firmware
 * ==========================================
 *
 * Firmware per 3× ESP32: ping reciproci 802.11 → cattura CSI → UDP a unità centrale.
 * Ispirato da:
 *   - ESP32-CSI-Tool (StevenMHernandez)  → callback CSI + configurazione
 *   - Wifi-3D-Fusion (MaliosDark)        → multi-source UDP streaming
 *   - RuView (ruvnet)                    → formato ADR-018, timestamp preciso
 *
 * ARCHITETTURA:
 *   Ogni nodo invia frame 802.11 broadcast a 100 Hz su canale fisso.
 *   Il CSI callback si attiva su OGNI frame ricevuto (inclusi i propri broadcast).
 *   Dal MAC source si risale al TX node ID.
 *   3 nodi × 3 percorsi = 9 percorsi TX→RX unici.
 *   Ogni frame viene impacchettato con timestamp e spedito via UDP al PC.
 *
 * OUTPUT UDP — magic 0xC5110003 (LE u32):
 *   [0..3]   Magic: 0xC5110003
 *   [4]      TX Node ID (0, 1, 2)
 *   [5]      RX Node ID (0, 1, 2)
 *   [6..7]   Numero subcarrier (LE u16)
 *   [8..11]  Sequence number (LE u32)
 *   [12]     RSSI (i8)
 *   [13]     Noise floor (i8)
 *   [14..15] Reserved
 *   [16..23] Timestamp microsecondi (LE i64)
 *   [24..]   I/Q pairs (int8_t, subcarrier × 2 byte)
 *
 * SETUP (modifica network_config.h):
 *   NODE_ID = 0, 1, o 2  (diverso per ogni scheda)
 *   MAC address dei 3 nodi in NODE_MACS
 *   UDP_TARGET_IP = IP del PC centrale
 *   WIFI_SSID / WIFI_PASS per la connessione WiFi
 *
 * DIPENDENZE:
 *   ESP32 Arduino Core (Tools → Board → ESP32 Dev Module)
 *   Usa esp_wifi internamente per CSI + 802.11 TX.
 */

#include <WiFi.h>
#include <WiFiUdp.h>
#include "esp_wifi.h"
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"
#include "network_config.h"
#include "secrets.h"

// ============================================================
// Costanti
// ============================================================
#define SERIAL_BAUD 115200
#define TX_INTERVAL_MS 10  // 100 Hz
#define STATS_INTERVAL_MS 5000
#define CHANNEL 6
#define MAX_SUBCARRIER 128

#define MAGIC_RADAR3D 0xC5110003
#define HEADER_SIZE 24  // header fisso 24 byte

// ============================================================
// Struttura pacchetto UDP
// ============================================================
#pragma pack(push, 1)
typedef struct {
  uint32_t magic;         // 0xC5110003
  uint8_t tx_node;        // chi ha trasmesso
  uint8_t rx_node;        // chi ha ricevuto (NODE_ID locale)
  uint16_t n_subcarrier;  // numero subcarrier
  uint32_t seq;           // sequenza progressiva
  int8_t rssi;            // RSSI del frame
  int8_t noise_floor;     // noise floor
  uint16_t reserved;      // padding
  int64_t timestamp_us;   // microsecondi ESP32
} radar3d_header_t;
#pragma pack(pop)

// ============================================================
// Globali
// ============================================================
WiFiUDP udp;
IPAddress remote_ip;

uint32_t seq_counter = 0;
uint32_t frames_tx = 0;
uint32_t csi_callbacks = 0;
int8_t iq_buffer[MAX_SUBCARRIER * 2];

// ============================================================
// Callback CSI
// ============================================================
static void wifi_csi_cb(void *ctx, wifi_csi_info_t *data) {
  wifi_csi_info_t *info = data;

  // Identifica TX node dal MAC source
  uint8_t *mac = info->mac;
  int tx_node = -1;
  for (int i = 0; i < 3; i++) {
    if (memcmp(mac, NODE_MACS[i], 6) == 0) {
      tx_node = i;
      break;
    }
  }
  if (tx_node < 0) return;

  // Legge I/Q raw dal buffer CSI
  int16_t *raw = (int16_t *)info->buf;
  uint16_t n_sub = info->len / 4;  // 2 int16 per subcarrier (I+Q)
  if (n_sub > MAX_SUBCARRIER) n_sub = MAX_SUBCARRIER;

  // Converte int16 → int8 (risparmia banda, info->type == CSI_RAW)
  for (uint16_t i = 0; i < n_sub; i++) {
    iq_buffer[i * 2 + 0] = (int8_t)(raw[i * 2] >> 8);      // I
    iq_buffer[i * 2 + 1] = (int8_t)(raw[i * 2 + 1] >> 8);  // Q
  }

  // Costruisce header
  radar3d_header_t hdr;
  hdr.magic = MAGIC_RADAR3D;
  hdr.tx_node = (uint8_t)tx_node;
  hdr.rx_node = NODE_ID;
  hdr.n_subcarrier = n_sub;
  hdr.seq = seq_counter++;
  hdr.rssi = (int8_t)info->rx_ctrl.rssi;  hdr.noise_floor = -90;
  hdr.reserved = 0;
  hdr.timestamp_us = esp_timer_get_time();

  // Spedisce via UDP
  udp.beginPacket(remote_ip, UDP_TARGET_PORT);
  udp.write((uint8_t *)&hdr, sizeof(radar3d_header_t));
  udp.write((uint8_t *)iq_buffer, n_sub * 2);
  udp.endPacket();

  csi_callbacks++;
}

// ============================================================
// Inietta frame 802.11 broadcast (ping)
// ============================================================
static void inject_ping() {
  uint8_t pkt[32];
  memset(pkt, 0, sizeof(pkt));

  // Data frame + From DS
  pkt[0] = 0x08;
  pkt[1] = 0x02;

  // Address 1: Broadcast
  memset(&pkt[4], 0xFF, 6);

  // Address 2: MAC sorgente = NODE_MACS[NODE_ID]
  memcpy(&pkt[10], NODE_MACS[NODE_ID], 6);

  // Address 3: BSSID = stesso MAC
  memcpy(&pkt[16], NODE_MACS[NODE_ID], 6);

  // Sequence control
  static uint16_t seq = 0;
  pkt[22] = seq & 0xFF;
  pkt[23] = (seq >> 8) & 0xFF;
  seq += 16;

  esp_wifi_80211_tx(WIFI_IF_STA, pkt, sizeof(pkt), false);
  frames_tx++;
}

// ============================================================
// Setup
// ============================================================
void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(500);

  Serial.printf("\n=== ESP32 Radar 3D — NODE %d ===\n", NODE_ID);

  // Disabilita brownout detector (CSI richiede CPU stabile)
  WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0);

  // CPU alla massima frequenza per CSI stabile
  setCpuFrequencyMhz(240);

  // WiFi STA
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  int att = 0;
  while (WiFi.status() != WL_CONNECTED && att < 40) {
    delay(500);
    Serial.print(".");
    att++;
  }
  if (WiFi.status() != WL_CONNECTED) {

    Serial.println("\nWiFi FAIL — reboot");
    delay(3000);
    ESP.restart();
  }

  uint8_t mac[6];
  WiFi.macAddress(mac);
  Serial.print("\nESP32 MAC Address: ");
  Serial.println(WiFi.macAddress());
  remote_ip.fromString(UDP_TARGET_IP);

  udp.begin(WiFi.localIP(), UDP_TARGET_PORT);

  // === Config CSI ===
  esp_wifi_set_promiscuous(true);
  uint8_t ch = WiFi.channel();
  Serial.printf("Canale WiFi: %d\n", ch);
  esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);

  wifi_csi_config_t csi_conf = { 0 };
#if CONFIG_SOC_WIFI_HE_SUPPORT
  /* HE-capable chip (C6/C5): bitfield struct wifi_csi_acquire_config_t.
   *  Fields differ between MAC version 3 and non-3. */
  csi_conf.enable = 1;
  csi_conf.acquire_csi_legacy = 1;
  csi_conf.acquire_csi_ht20 = 1;
  csi_conf.acquire_csi_ht40 = 1;
#if CONFIG_SOC_WIFI_MAC_VERSION_NUM == 3
  csi_conf.acquire_csi_force_lltf = 1;
  csi_conf.acquire_csi_vht = 1;
#endif
  csi_conf.acquire_csi_su = 1;
  csi_conf.acquire_csi_mu = 1;
  csi_conf.acquire_csi_dcm = 1;
#else
  /* Legacy ESP32: bool / uint8_t struct */
  csi_conf.lltf_en = 1;
  csi_conf.htltf_en = 1;
  csi_conf.stbc_htltf2_en = 0;
  csi_conf.ltf_merge_en = 1;
  csi_conf.channel_filter_en = 0;
  csi_conf.manu_scale = 0;
  csi_conf.shift = 0;
#endif
  ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_conf));
  ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(&wifi_csi_cb, NULL));
  ESP_ERROR_CHECK(esp_wifi_set_csi(true));

  Serial.println("CSI attivo. Cross-ping 100 Hz...\n");
  Serial.println("tx_node rx_node n_sub seq rssi ts");
  Serial.println(WiFi.status());
  Serial.println(WiFi.SSID());
}

// ============================================================
// Loop
// ============================================================
static unsigned long last_tx = 0;
static unsigned long last_stats = 0;

void loop() {
  unsigned long now = millis();

  if (now - last_tx >= TX_INTERVAL_MS) {
    inject_ping();
    last_tx = now;
  }

  if (now - last_stats >= STATS_INTERVAL_MS) {
    Serial.printf("[%d] TX:%u CSI:%u\n", NODE_ID, frames_tx, csi_callbacks);
    last_stats = now;
  }
}
