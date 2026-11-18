# IF_I_WIFI

Experiments and tooling for WiFi Channel State Information (CSI) sensing with ESP32 devices. The project explores presence detection, motion estimation, position tracking, and basic vital-sign estimation from radio signals, with host-side processing and browser visualization.

## Overview

This repository includes firmware, host-side processing scripts, and visualization components for CSI-based sensing workflows. Depending on the setup, it can be used for:

- presence and motion detection
- grid-based or continuous position estimation
- real-time visualization (heatmap / radar-style views)
- experimental breathing-rate and heart-rate estimation
- recording and replaying CSI data for offline analysis

Results depend heavily on hardware placement, environment, calibration, and model quality.

## Capabilities

| Area | Description |
|---|---|
| Multi-node capture | Cross-ping setup with multiple ESP32 devices for stable CSI channels |
| Presence detection | Variance/state-based detection (empty / still / motion) |
| Position estimation | Grid classification and continuous `(x, y)` regression with smoothing |
| Visualization | Browser-based heatmap and 3D radar views over WebSocket |
| Signal processing | Filtering, spectral analysis, and CSI feature extraction utilities |
| Experimental vitals | Breathing and heart-rate estimation pipelines |
| Integration | Optional bridges for external consumers (for example MQTT/Home Assistant setups) |

## Architecture

### Cross-ping setup (recommended)

```text
┌──────────┐      ┌──────────┐      ┌──────────┐
│ ESP32 #0 │ <--> │ ESP32 #1 │ <--> │ ESP32 #2 │
└────┬─────┘      └────┬─────┘      └────┬─────┘
     │ UDP             │ UDP             │ UDP
     └─────────────────┴─────────────────┘
                       ▼
                ┌─────────────────┐      WebSocket      ┌──────────┐
                │ Host processing │ ──────────────────> │ Browser  │
                │ (Python / ML)   │                     │ UI       │
                └─────────────────┘                     └──────────┘
```

In this mode, nodes exchange frames at a fixed channel and produce stable `(tx, rx)` CSI pairs for downstream processing.

## Getting started

### Requirements

- 1-3 ESP32 boards (depending on experiment)
- Python 3.10+
- Arduino IDE / ESP32 toolchain for firmware flashing
- A host machine for processing and visualization

### 1. Flash firmware

Use the cross-ping firmware in `firmware/esp32_radar3d/`.

1. Copy `firmware/esp32_radar3d/secrets.h.example` to `secrets.h` and configure WiFi credentials.
2. Edit `firmware/esp32_radar3d/network_config.h`:
   - set `NODE_ID` for each board
   - configure node MAC addresses
   - set the host UDP target IP
3. Flash each board with the appropriate `NODE_ID`.

See [`firmware/esp32_radar3d/FLASHING.md`](firmware/esp32_radar3d/FLASHING.md) for board-specific details.

### 2. Train or calibrate a model

Example (grid-based positions):

```bash
python3 -m csi.csi_mac --positions --grid 3x3 --use-ml
```

Example (continuous blob tracking):

```bash
python3 -m csi.blob_cli --train \
  --udp-port 5005 \
  --seconds 30 \
  --points "0.5,0.5;2.0,0.5;1.25,3.0;0.5,5.5;2.0,5.5"
```

### 3. Run live processing

```bash
python3 -m csi.csi_mac --monitor --use-ml --udp-port 5005 --ws-port 8765
```

Or for continuous tracking:

```bash
python3 -m csi.blob_cli --monitor --udp-port 5005 --ws-port 8765
```

Then serve/open the visualization files from `mapping/` in a browser.

## Diagnostics

Use the included diagnostics to verify firmware mode, receiver rates, and source consistency:

```bash
python3 -m csi.diagnose_model
```

## Repository layout

- `firmware/` — ESP32 firmware variants and flashing notes
- `csi/` — host-side processing, training, and inference scripts
- `mapping/` — browser visualizations
- additional modules for signal processing, feature extraction, and integrations

## Notes

- This is an experimental/research-oriented codebase; interfaces and scripts may vary across setups.
- Sensing performance is environment-dependent and should be validated in the target space.
- Use responsibly and respect privacy and local regulations when deploying sensing systems.

## License

MIT
