/*
 * network_config.h — ESP32 Radar 3D
 * ===================================
 * Configurazione unica per ogni scheda.
 * I 3 ESP32 devono avere NODE_ID diverso (0, 1, 2).
 * Gli indirizzi MAC di tutti e 3 vanno scoperti una volta
 * (Serial.printf("%02X...") nel setup) e inseriti qui sotto.
 * UDP_TARGET_IP = IP del PC centrale che esegue run.py.
 */

#ifndef NETWORK_CONFIG_H
#define NETWORK_CONFIG_H

// ============================================================
// NODE ID — UNICO PER SCHEDA (0, 1, 2)
// ============================================================
#define NODE_ID 1 // <-- CAMBIA per ogni ESP32!

// ============================================================
// MAC dei 3 nodi radar (ESP32)
// ============================================================
// Scopri il MAC di ogni scheda: collega seriale, NODE_ID=0...2, guarda serial.
// Poi riempi qui sotto nell'ordine [0, 1, 2].
static const uint8_t NODE_MACS[3][6] = {
  { 0xE4, 0xB0, 0x63, 0xAE, 0xEE, 0x30 },  // NODE 0 (pier)
  { 0xE4, 0xB0, 0x63, 0xAF, 0x00, 0x50 },  // NODE 1 (mio)
  { 0x20, 0xE7, 0xC8, 0xB4, 0xBF, 0x08 },  // NODE 2 (vins)
};

// ============================================================
// WiFi + UDP target
// ============================================================
#include "secrets.h"  // WIFI_SSID, WIFI_PASS

#define UDP_TARGET_IP   "172.20.10.9"    // IP del PC (da ifconfig en0)
#define UDP_TARGET_PORT 5005         // stessa porta di run.py (--udp-port 5005 default)

#endif  // NETWORK_CONFIG_H
