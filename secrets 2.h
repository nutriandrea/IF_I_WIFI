// WiFi credentials per ESP32 CSI Firmware.
// Copia questo file in secrets.h e mettici le tue credenziali.
// secrets.h e' in .gitignore — NON committarlo.
//
// Per MONO-AP (default): singola rete WiFi
//   WIFI_SSID, WIFI_PASS
//
// Per MULTI-AP (3 telefoni hotspot):
//   Scommenta NUM_APS e AP_LIST, commenta WIFI_SSID/WIFI_PASS
//
// Gli AP devono essere a 2.4 GHz (ESP32 non supporta 5 GHz).
// Per stabilita CSI, fissa il canale dell'AP (auto-channel disturba la baseline).
// I telefoni in hotspot tipicamente usano canale 1, 6 o 11.

#pragma once

// --- MONO-AP mode ---
 #define WIFI_SSID "FASTWEB-ER3SF3"
 #define WIFI_PASS "RC6TXZXT9X"

// --- MULTI-AP mode (3 telefoni hotspot) ---
// #define NUM_APS 3
// #define AP_CAPTURE_SECONDS 3  // secondi di cattura per ogni AP prima dello switch

// #define AP_LIST { \
//     {"Telefono1_SSID", "Telefono1_PASS"}, \
//     {"Telefono2_SSID", "Telefono2_PASS"}, \
//     {"Telefono3_SSID", "Telefono3_PASS"}, \
}
