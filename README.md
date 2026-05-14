# If-I-Wi-Fy — Wi-Fi Sensing on Arduino UNO Q

> *Ambient intelligence through the radio waves that already surround you.*

Privacy-preserving presence and motion detection using nothing but the Wi-Fi that already fills the room. Built on the **Arduino UNO Q** — the dual-brain board that fuses a Linux AI processor (Qualcomm Dragonwing QRB2210) with a real-time microcontroller (STM32U585) in a single device.

No cameras. No wearables. No new infrastructure. The walls hum with information; we just listen.

For the full vision and applications (eldercare, smart workplaces, energy management, privacy-first security, retail analytics, industrial safety), see [docs/VISION.md](docs/VISION.md).

---

## Two sensing pipelines, one platform

This repository contains two independent pipelines that share the same Arduino UNO Q hardware and the same Python orchestration layer. They differ in the physical signal they exploit.

| | **RSSI** | **CSI** |
|---|---|---|
| Signal | Received Signal Strength Indicator (coarse, single dBm value per packet) | Channel State Information (per-subcarrier amplitude + phase) |
| Hardware needed | UNO Q only | UNO Q + ESP32 |
| Sample rate | 2–20 Hz (`/proc/net/wireless`) | 10–100 Hz |
| Resolution | One number per packet | 64–128 complex numbers per packet |
| What it can detect | Coarse presence, motion bursts | Fine-grained motion, breathing rate, gait, posture (with more work) |
| Setup complexity | Trivial | Higher (ESP32 firmware + UART bridge) |
| Status in this repo | Working end-to-end on UNO Q | Working end-to-end on UNO Q (when ESP32+bridge are wired) |

You can start with RSSI and progress to CSI when finer sensing is needed. Both run **on-device** with no cloud dependency.

---

## Hardware

### Arduino UNO Q

The dual-brain board that makes everything possible.

| Component | Role |
|---|---|
| Qualcomm Dragonwing QRB2210 (quad ARM A53 @ 2 GHz, Debian Linux) | Python pipeline: signal acquisition, feature extraction, ML inference, dashboard |
| STM32U585 (ARM Cortex-M33, Zephyr OS) | Deterministic real-time I/O: sensor reads, actuators, relays, LED feedback |
| Dual-band Wi-Fi 5 (2.4 / 5 GHz) + Bluetooth 5.1 | The sensing substrate itself (and the bridge to the network) |
| 2 GB LPDDR4 RAM, 16 GB+ eMMC | Enough to train, run, and log ML pipelines on-device |
| USB-C, ~€60 retail | Deployable at consumer scale |

**MCU↔Linux communication** does NOT go via a classic `/dev/tty*`. The Qualcomm side talks to the STM32 through the `arduino-router` service — a Go daemon that exposes a MessagePack RPC socket at `/var/run/arduino-router.sock`. The router is preinstalled and running by default. Internally it bridges `/dev/ttyHS1` (115200 baud, internal UART) to the Unix socket. Python clients call `client.call("method_name", args)` and the STM32 responds.

### ESP32 (CSI pipeline only)

A classic **ESP32** (WROOM-32 / DevKit V1) with built-in WiFi. Required only for the CSI pipeline. The ESP32 is the only widely available chip whose drivers expose per-subcarrier CSI from received 802.11 frames.

---

## Pipeline 1 — RSSI (UNO Q standalone)

Detects presence and motion from the WiFi signal strength of `wlan0` (the UNO Q's own radio). No extra hardware required.

### How it works

1. `/proc/net/wireless` is polled at 10–20 Hz for the RSSI of the last frame received from the AP the UNO Q is associated to. Zero `subprocess` overhead.
2. A short sliding window (~30 samples) extracts statistical features: mean, std, gradient, consecutive same-sign run length, and signal_avg from `iw station dump`.
3. A multi-metric scoring detector fuses these signals into a `presence_score`. Above a calibrated threshold → presence.

### Files

| File | What it does |
|---|---|
| [enhanced_presence.py](enhanced_presence.py) | Main detector. Modes: `quick` / `baseline` / `movement` / `analyze` / `monitor` |
| [calibrate_presence.py](calibrate_presence.py) | Legacy calibration (std + adaptive delta strategies) |
| [monitor_presence.py](monitor_presence.py) | Monitor-mode capture (requires root, optional) |
| [decision_engine.py](decision_engine.py) | Orchestrator: RSSI → score → STM32 relay/LED action via bridge RPC |
| [feasibility_test.py](feasibility_test.py) | End-to-end smoke test (RSSI, features, system load, bridge, combined pipeline) |
| [feasibility_bridge/](feasibility_bridge/) | STM32 sketch — exposes `ping`, `get_sensors`, `set_relay`, `set_led` RPCs |
| [feasibility_test/](feasibility_test/) | Legacy STM32 sketch (USB-CDC only, no router bridge) |
| [bridge_client.py](bridge_client.py) | CLI wrapper for arduino-router RPC (useful for ad-hoc calls) |

### Quick start

```bash
# On the UNO Q via SSH
cd ~/ArduinoApps/ArduinoWifiSensing
git pull
sudo apt-get install -y iw

# Quick calibration (30s baseline + 30s movement + analysis)
python3 enhanced_presence.py --mode quick

# Live monitoring with calibrated thresholds
python3 enhanced_presence.py --mode monitor

# Full orchestrator (RSSI + sensor RPCs + relay/LED)
python3 decision_engine.py
```

For the STM32 side (sensors + relay): upload [feasibility_bridge/feasibility_bridge.ino](feasibility_bridge/feasibility_bridge.ino) via Arduino IDE (Board: "Arduino UNO Q (STM32)").

### Known limit

The Qualcomm WiFi driver on UNO Q reports RSSI that alternates ±10 dBm sample-to-sample (likely dual antenna / dual MAC queue artifact). The `enhanced_presence` detector compensates with EMA filtering and signal_avg fusion, but residual noise overlaps with human-motion signal. RSSI alone gives **coarse** presence detection with non-trivial false-positive rates. For sub-second, room-mapping, or biometric-grade sensing, use the CSI pipeline.

---

## Pipeline 2 — CSI (UNO Q + ESP32)

Detects presence and motion from per-subcarrier amplitude and phase of WiFi packets received by the ESP32. Far more sensitive than RSSI because it captures **multipath fingerprinting** — the way a person's body reshapes the radio environment is visible per subcarrier, not just as a single dBm number.

### Credits

The ESP32 CSI capture firmware is adapted from the **[ESP32-CSI-Tool](https://stevenmhernandez.github.io/ESP32-CSI-Tool/)** by Steven M. Hernandez, which exposes the ESP-IDF `esp_wifi_set_csi_rx_cb()` API in a usable form. We adapted the output format (`CSI:<seq>:<rssi>:<noise>:<rate>:<bw>:<sub_count>:<r0,i0,...>`) so the UNO Q's STM32 bridge can stream it via the `arduino-router` to the Linux side. The Python parser also accepts the original `CSI_DATA,...` format for compatibility with Hernandez's analysis scripts.

### Architecture

```
   ESP32                    UNO Q STM32                  UNO Q Linux
┌─────────────┐    UART    ┌──────────────┐    RPC    ┌──────────────┐
│ CSI capture │──921600───▶│ csi_bridge   │──msgpack─▶│ csi_processor│
│ (Hernandez) │  D0/D1     │ buffer       │  router   │ detect/log   │
└─────────────┘            └──────────────┘           └──────────────┘
```

The ESP32 connects to a 2.4 GHz AP (your home WiFi), captures CSI from every received packet (beacons + responses), and streams CSV frames on its UART. The STM32 bridges those frames into the Linux side as RPC responses. Python parses, runs the detector, logs.

### Files

| File | What it does |
|---|---|
| [csi_processor.py](csi_processor.py) | Main CSI pipeline on Linux. Modes: `--ping` / `--monitor` / `--calibrate` / `--benchmark` / `--analyze`. Includes `parse_csi_line` and `CSIDetector` classes |
| [csi_mac.py](csi_mac.py) | Standalone: read CSI directly from ESP32's USB on a Mac/PC. Bypasses UNO Q entirely. Useful for testing CSI without the UART bridge |
| [esp32_csi_firmware/](esp32_csi_firmware/) | ESP32 sketch — captures CSI and streams CSV |
| [esp32_csi_bridge/](esp32_csi_bridge/) | STM32 sketch — buffers ESP32 frames and exposes `csi_ping`, `csi_count`, `csi_read_all`, `csi_clear` RPCs |
| [test_esp32_uart/](test_esp32_uart/) | STM32 sketch to verify the UART link to ESP32 |

### Wiring (ESP32 → UNO Q)

```
ESP32 GND  →  UNO Q GND          (mandatory)
ESP32 5V   →  UNO Q 5V
ESP32 TX   →  UNO Q D0           (STM32 Serial1 RX)
ESP32 RX   →  UNO Q D1           (STM32 Serial1 TX)
Baud rate: 921600 (or 115200 if you flashed the diagnostic build)
```

### Quick start

**On a host computer (Mac/PC):**
```bash
cd esp32_csi_firmware
cp secrets.h.example secrets.h    # then edit SSID/PASS, AP must be 2.4 GHz!
```

Open `esp32_csi_firmware.ino` in Arduino IDE. Board: **ESP32 Dev Module**. Upload Speed: **115200** (more reliable than 921600 with cheap USB-Serial chips). Upload to ESP32 via USB. Watch the Serial Monitor at the firmware's baud rate (currently 115200 in the diagnostic build) for `ESP32_CSI_READY` → `WiFi:connecting...` → `WiFi:OK,<ip>` → `CSI:enabled`.

**On the UNO Q (full pipeline through the STM32 bridge):**
```bash
# 1. Upload esp32_csi_bridge.ino to the STM32 via Arduino IDE
#    (Board: "Arduino UNO Q (STM32)")
# 2. Wire ESP32 to D0/D1 as above
# 3. Power-cycle / reset both

cd ~/ArduinoApps/ArduinoWifiSensing
git pull
pip install msgpack
python3 csi_processor.py --ping       # → ESP32: pong:OK
python3 csi_processor.py --monitor    # live presence detection
```

**On a Mac (CSI without UNO Q, ESP32 only):**
```bash
pip install pyserial
python3 csi_mac.py --monitor          # autodetects /dev/cu.usbserial-*
python3 csi_mac.py --capture --seconds 60 --label test1
python3 csi_mac.py --calibrate --seconds 30
```

### Why CSI matters

RSSI gives one number per packet. CSI gives 64–128 (one per subcarrier). When a person enters the room, RSSI may barely budge, but the per-subcarrier multipath structure changes dramatically — different subcarriers experience different constructive/destructive interference from the new body. The variance across subcarriers, the temporal evolution of phase, and cross-subcarrier covariance are all rich features that academic literature has exploited for breathing-rate estimation, gait recognition, fall detection, and pose estimation. This repo provides the substrate.

---

## Repository layout

```
.
├── README.md                           # this file
├── docs/
│   ├── VISION.md                       # full If-I-Wi-Fy vision (use cases, market)
│   ├── TESTING.md                      # how to run tests
│   ├── arduino_cloud_integration.md    # optional Arduino Cloud sync
│   ├── demo_24h_plan.md                # demo playbook
│   └── shopping_list.md                # hardware BOM
│
├── enhanced_presence.py                # RSSI pipeline (main)
├── calibrate_presence.py
├── monitor_presence.py
├── decision_engine.py
├── feasibility_test.py
├── bridge_client.py                    # arduino-router RPC client (CLI)
│
├── csi_processor.py                    # CSI pipeline (main)
├── csi_mac.py                          # CSI without UNO Q (Mac/PC standalone)
│
├── feasibility_bridge/                 # STM32 sketch: sensors + relay (RSSI pipeline)
├── feasibility_test/                   # STM32 sketch: legacy USB-only
├── esp32_csi_firmware/                 # ESP32 sketch: CSI capture
├── esp32_csi_bridge/                   # STM32 sketch: ESP32→UNO Q bridge
├── test_esp32_uart/                    # STM32 sketch: UART link tester
│
├── test_csi_processor.py               # Python tests
├── test_detectors.py
└── csi_logs/                           # CSI capture output (gitignored)
```

---

## Setup

### Common dependencies (UNO Q Linux side)

```bash
sudo apt-get install -y iw python3-pip
pip install msgpack pyserial
```

`iw` is needed by the RSSI pipeline. `msgpack` is needed by anything that talks to `arduino-router` (the bridge). `pyserial` is needed by `csi_mac.py` (host-side standalone CSI).

### Mac / PC (CSI standalone, no UNO Q)

```bash
pip install pyserial
# msgpack NOT required — csi_processor.py imports it lazily only when
# RouterClient.connect() is called
```

### STM32 sketches

Use Arduino IDE with the **Arduino UNO Q (STM32)** core selected. You must install the **Arduino_RouterBridge** library via Library Manager (the core ships a stub that errors out otherwise).

Only one of `feasibility_bridge` or `esp32_csi_bridge` can run on the STM32 at a time — they expose different RPC method sets. Choose based on which pipeline you're testing.

### ESP32 sketch

Arduino IDE with the **esp32** core (Espressif) and Board: **ESP32 Dev Module**. The CSI API uses `esp_wifi_set_csi()` from ESP-IDF, exposed by the Arduino core. No external library needed.

Set Upload Speed to **115200** (or **460800** max) — 921600 fails on many USB-Serial chips and cables.

---

## Troubleshooting

**Upload to ESP32 fails with "chip stopped responding"**: lower Tools > Upload Speed to 115200, use a short data-quality USB cable, plug directly into the host (no hub), optionally hold BOOT during the whole upload.

**Serial Monitor shows only garbage**: Arduino IDE 2.x has a known bug where the baud-rate dropdown doesn't always take effect. Close and reopen Serial Monitor, or use `screen /dev/cu.usbserial-XXX 115200` from a terminal.

**ESP32 boots then reset-loops**: USB hub current-limited. Plug ESP32 directly into the host. The firmware has brownout detector disabled and TX power reduced to mitigate; if it still loops, the supply path is too weak.

**`csi_processor.py --ping` returns `ESP32_NOT_CONNECTED`**: the STM32 bridge can't reach the ESP32 via UART. Verify wiring (TX/RX not swapped, GND shared), baud match (both at 921600 in production firmware), and that the ESP32 is actually running its firmware (Serial Monitor shows boot messages).

**RSSI presence detection gives constant false positives**: the Qualcomm driver alternates RSSI between two values. This is known. Use `signal_avg` (which `enhanced_presence` does) and a higher gradient threshold (`--grad-threshold 3.0`).

For more, see [docs/TESTING.md](docs/TESTING.md).

---

## Privacy by construction

Everything runs on the edge. No frames are sent to the cloud. No biometric features are extracted. The signals we exploit (RSSI, CSI amplitude/phase per subcarrier) cannot be reversed into identifying information about the persons present. This is privacy-preserving **as a property of the physics**, not as a promise from a manufacturer.

See [docs/VISION.md](docs/VISION.md) for the broader argument.

---

## License

To be decided. The Steven Hernandez ESP32-CSI-Tool that the ESP32 firmware adapts is under its own license — consult the upstream repository for terms.
