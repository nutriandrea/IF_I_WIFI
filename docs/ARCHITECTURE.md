# Architecture

## Data Flow

```
 3× ESP32 (radar3d firmware)
    │ UDP unicast :5005, 100 Hz
    ▼
┌─────────────────────────────────────────────────────────────┐
│ ws_server.py (Python)                                       │
│  • UDP listener → binary frame parser                       │
│  • Frame demux by (tx_node, rx_node) → 9 CSI streams        │
│  • WebSocket broadcast to browser (ws://:8765)             │
│  • CSV dump to disk                                         │
│  • UDP relay raw frames → :5006 (Rust)                     │
└────┬────────────────────────────────────────────────────────┘
     │ CSI frames (amplitudes + phases + RSSI + sequence)
     │         │
     │         ├─── UDP relay (raw I/Q) ───►┌──────────────────────────────┐
     │         │                            │ sensing-server (Rust/RuView) │
     ▼         ▼                            │  • WS stream /ws/sensing     │
┌──────────────────────────┐               │  • REST API :8080            │
│ Python Processing        │               │  • UI server (RuView)        │
│ Pipelines (parallel)     │               │  • Vitals: breathing, HR     │
│ Presence  Position       │               │  • Pose estimation           │
│ Vitals    Classification │               │  • Multi-person tracking     │
│ Signal Processing        │               │  • MQTT bridge               │
│ Integration Services     │               └──────────────────────────────┘
└──────────────────────────┘
```

## Module Map

### Data Ingestion

| Module | Files | Role |
|--------|-------|------|
| `csi/esp32_parser.py` | 1 | Binary UDP frame parser for ESP32 magic-number protocol (0xC511_0001/2/4). Dataclasses: `Esp32Frame`, `Esp32VitalsPacket`, `WasmOutputPacket` |
| `csi/csi_processor.py` | 1 | UDP listener (port 5005), frame demux across 9 TX×RX streams, WebSocket broadcast to `ws://:8765`, optional CSV dump |
| `csi/csi_record.py` | 1 | Offline CSI file recorder/replayer |
| `csi/quadrants/ws_server.py` | 1 | Production UDP→WebSocket bridge (port 5005 → WS 8765). Passes parsed frames to pipelines. Supports `--relay-port` to forward raw UDP to RuView Rust `sensing-server` on :5006 |
| `bin/sensing-server` | 1 | RuView Rust sensing-server binary (4.2 MB). UDP input on :5006, WebSocket `/ws/sensing`, HTTP UI on :8080. Built from `ruvnet/RuView` |

### Presence Detection

| Module | Files | Role |
|--------|-------|------|
| `csi/presence/detector.py` | 1 | `PresenceDetector` — variance-based state machine: EMPTY / STILL / MOTION. No ML. Calibration ~3s empty room. Configurable hysteresis + dwell time |
| `csi/presence/monitor_cli.py` | 1 | CLI monitor for presence state |

### Position Tracking

| Module | Files | Role |
|--------|-------|------|
| `csi/quadrants/blob_live.py` | 1 | `BlobEstimator` — variance-weighted RX centroid. No ML. Returns `(x, y)` + uncertainty |
| `csi/quadrants/regressor.py` | 1 | `PositionRegressor` — RandomForest multi-output + KalmanFilter2D. LOO-cell cross-validation. Motion detection via velocity hysteresis |
| `csi/quadrants/ws_server.py` | 1 | WebSocket server multiplexer. Serves presence + position + heatmap to browser |
| `csi/blob3d/tracker.py` | 1 | `BlobTracker3D` — rough `(x, y, z)` with height macro-class from multi-RX variance ratios |
| `csi/blob_cli.py` | 1 | CLI for position regressor training and live tracking |

### Classification (ML)

| Module | Files | Role |
|--------|-------|------|
| `csi/features.py` | 1 | Feature extraction functions: `extract_csi_profile`, `csi_window_to_vector`, per-source variants. Statistical features (mean, std, percentiles, etc.) per subcarrier group |
| `csi/classifier.py` | 1 | `CSIClassifier` — sklearn RF on CSI feature vectors. Train/save/load CLI |
| `csi/multi_ap.py` | 1 | `MultiAPCSIClassifier` — cross-node RSSI-weighted feature fusion. Multi-RX → single prediction |

### Vitals

| Module | Files | Role |
|--------|-------|------|
| `csi/vitals/types.py` | 1 | `VitalStatus`, `VitalEstimate`, `VitalReading`, `CsiFrame` dataclasses |
| `csi/vitals/preprocessor.py` | 1 | `CsiVitalPreprocessor` — EMA residual extraction (alpha=0.05). Removes slow DC drift from amplitude |
| `csi/vitals/respiration.py` | 1 | `BreathingExtractor` — IIR bandpass (0.1-0.5 Hz) + zero-crossing BPM. Confidence via SNR |
| `csi/vitals/heartrate.py` | 1 | `HeartRateExtractor` — IIR bandpass (0.8-2.0 Hz) + autocorrelation peak HR |
| `csi/vitals/anomaly.py` | 1 | `VitalAnomalyDetector` — Welford z-score per vital. Apnea / tachycardia / bradycardia alerts |
| `csi/vitals/store.py` | 1 | `VitalSignStore` — rolling window (3600 readings), stats (mean, min, max, valid_fraction) |
| `csi/vitals/pipeline.py` | 1 | `VitalSignPipeline` — orchestrates preprocessor → breathing → heartrate → anomaly → store |
| `csi/breathing_ml.py` | 1 | `PhaseBreathingEstimator` — phase-based BPM from unwrapped phase. Standalone, pure-numpy |
| `csi/sleep.py` | 1 | `SleepQualityAnalyzer` — sleep stage heuristic (awake/light/deep) from breathing regularity. Apnea detection |
| `csi/doppler.py` | 1 | `DopplerShiftExtractor` — FFT-based Doppler profile from phase difference. Spectral band power features |

### Signal Processing (`csi/signal/`)

| Module | Role |
|--------|------|
| `spectrogram.py` | STFT spectrogram with configurable window (rect/hann/hamming/blackman). Power or magnitude |
| `bvp.py` | Body Velocity Profile — velocity-resolved energy distribution from CSI temporal matrix |
| `fresnel.py` | Fresnel zone geometry for breathing amplitude modeling |
| `features.py` | Per-component feature containers: `AmplitudeFeatures`, `PhaseFeatures`, `CorrelationFeatures`, `DopplerFeatures`, `PowerSpectralDensity` |
| `motion.py` | `MotionDetector` — fused motion score from amplitude/phase/doppler components |
| `filter.py` | `BiquadFilter` — Butterworth biquad IIR (lowpass/bandpass/highpass), multi-section cascade, per-sample streaming |
| `stats.py` | `WelfordOnline` — streaming mean/variance (O(1) memory, numerically stable). `RunningMinMax` |
| `hampel.py` | Hampel filter for outlier removal in 1D signals |
| `csi_ratio.py` | CSI ratio H1/H2 processing for antenna-pair noise cancellation |
| `phase_sanitizer.py` | Phase unwrap → Z-score outlier removal → moving average smooth |

### Integration

| Module | Files | Role |
|--------|-------|------|
| `csi/services/` | 3 | `ServiceOrchestrator`, `HealthCheckService`, `MetricsService` — async service lifecycle, component health, time-series metrics |
| `csi/ha_bridge.py` | 1 | `HaBridge` — Home Assistant MQTT discovery + state publishing. 6 sensors + 3 binary sensors per node |
| `csi/ws_client.py` | 1 | `SensingWsClient` — asyncio WebSocket client for remote RuView-compatible sensing server. Yields typed `EdgeVitals`/`PoseData`/`SensingUpdate` messages |

### Tools & Experimental

| File | Role |
|------|------|
| `csi/csi_plot.py` | Offline CSI data plotting |
| `csi/csi_mac.py` | MAC address scanner for ESP32 nodes |
| `csi/csi_ml.py` | Backward-compat shim re-exporting all symbols from `features.py`, `classifier.py`, `multi_ap.py`, `rssi_features.py`, `doppler.py`, `sleep.py`, `breathing_ml.py` |
| `experimental/diag_paths.py` | UDP path diagnostic — tabella 3×3 TX×RX attivi |
| `experimental/discover_macs.py` | One-shot MAC discovery via serial |
| `experimental/inject_radar3d_frames.py` | Simulatore UDP (finge 3 ESP32 con movimento) |
| `experimental/run_local.sh` | Demo script: ws_server + simulatore + browser |

### Rust Host (`host-rust/`)

| Crate | Files | Role |
|-------|-------|------|
| `crates/ifi-core` | 1 | `CsiFrame` codec (ADR-018), core types. Pure Rust, no allocator |
| `crates/ifi-dsp` | 1 | Streaming Hampel filter, coherence gate. Ported from RuView |
| `crates/ifi-transport` | 1 | UDP receiver + bounded ring buffer. Blocking interface |
| `crates/ifi-cli` | 1 | `ifiwifi-capture` binary — single-binary UDP capture + logging |

### Docs (`docs/`)

| Doc | Role |
|-----|------|
| `docs/decisions/` | Architecture Decision Records (ADR): extraction rationale, merge decisions |
| `docs/csi-frame-format.md` | ADR-018 wire format (header layout, payload I/Q, channel→freq map) |
| `docs/hardware-support.md` | Hardware compatibility matrix (ESP32 variants, host platforms, UNO Q roadmap) |
| `docs/what-was-cut.md` | Audit trail di ciò che è stato tagliato da RuView e perché |

### Experiments (`experiments/`)

| File | Role |
|------|------|
| `experiments/README.md` | Experiment policy — lavoro non ancora validato vive qui, non in `host-*` |

### Firmware

| Path | Role |
|------|------|
| `firmware/esp32_radar3d/` | Cross-ping firmware: 3 ESP32 ping each other at 100 Hz → 9 stable CSI (TX,RX) channels. Channel 6 fixed. UDP broadcast |

## Dependencies

```
┌─────────────────────────────────────────────────────────────────┐
│ 3× ESP32 (radar3d firmware) — UDP unicast :5005                 │
└─────────────────────┬───────────────────────────────────────────┘
                      │ raw I/Q frames (0xC5110001/2/3)
                      ▼
┌──────────────────────────────────────────────┐
│ esp32_parser.py   ←───   csi_processor.py    │
│ parse_esp32_vitals    parse_csi_radar3d      │
│ parse_esp32_frame     parse_csi_binary       │
│ parse_wasm_output     parse_csi_crossping    │
└──────────┬───────────────────────┬───────────┘
           │ parsed dicts          │ raw frames (relay :5006)
           ▼                       ▼
┌──────────────────────┐   ┌──────────────────────┐
│ quadrants/ws_server  │   │ host-rust (ifi-*)    │
│  → presence/detector │   │  + bin/sensing-server│
│  → blob_live         │   └──────────────────────┘
│  → regressor         │
│  → blob3d/tracker    │
│  → WebSocket (:8765) │
└──────────┬───────────┘
           │ JSON messages (~10 Hz)
           ▼
┌───────────────────┐
│ Browser UI (ui.html)│
│ 3D scene + heatmap │
└───────────────────┘

Python dependency chain (internal):
  presence/detector.py  ←───  quadrants/ws_server
  quadrants/blob_live.py ←─── quadrants/ws_server
  quadrants/regressor.py ←─── quadrants/ws_server
  blob3d/tracker.py      ←─── quadrants/ws_server
  features.py            ←─── classifier.py / multi_ap.py
  vitals/pipeline.py     ←─── vitals/preprocessor.py
                               ├── vitals/respiration.py ←── signal/filter.py
                               ├── vitals/heartrate.py  ←── signal/stats.py
                               └── vitals/anomaly.py
  signal/hampel.py       ←─── (standalone, used by vitals)
  signal/csi_ratio.py    ←─── (standalone)
  signal/phase_sanitizer ←─── (standalone)
```

## Parallel Launch

Use `run_parallel.sh` to start both Python and Rust runtimes simultaneously:

```bash
# With real ESP32 hardware
./run_parallel.sh

# With simulated data (no HW needed)
./run_parallel.sh --simulate

# Rebuild Rust binary first
./run_parallel.sh --build-rust --simulate
```

This starts:
- `ws_server.py` on UDP :5005, WS :8765, relay → :5006
- `bin/sensing-server` on UDP :5006, WS :8766, HTTP :8080 (RuView UI)

## Resource Budget

| Component | RAM | CPU | Storage |
|-----------|-----|-----|---------|
| ESP32 firmware | ~300 KB | Core 0 100%, Core 1 ~60% | ~844 KB flash |
| Python ws_server (UDP+WS) | ~20 MB | <5% | — |
| Rust sensing-server | ~30 MB | <10% | 4.2 MB (binary) |
| PresenceDetector | ~100 KB | <1% | — |
| BlobEstimator | ~200 KB | <1% | — |
| PositionRegressor + Kalman | ~2 MB | <2% | ~50 KB (model) |
| CSIClassifier (RF) | ~10 MB | <5% | ~500 KB (model) |
| VitalSignPipeline | ~500 KB | <2% | — |
| Signal processing (all) | ~5 MB | <5% | — |
| **Total host (Python + Rust)** | **~80 MB** | **<25% on Cortex-A72** | — |
