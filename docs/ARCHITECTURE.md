# Architecture

## Data Flow

```
 3× ESP32 (radar3d firmware)
    │ UDP multicast :5005, 100 Hz
    ▼
┌──────────────────────────────────────────────────────┐
│ csi_processor.py                                     │
│  • UDP listener → binary frame parser                │
│  • Frame demux by (tx_node, rx_node) → 9 CSI streams │
│  • WebSocket broadcast to browser clients            │
│  • CSV dump to disk                                  │
└────┬─────────────────────────────────────────────────┘
     │ CSI frames (amplitudes, phases, RSSI, sequence)
     ▼
┌──────────────────────────────────────────────────────────────────┐
│ Processing Pipelines (parallel, share no state)                  │
├────────────────┬────────────────┬───────────────┬────────────────┤
│ Presence       │ Position       │ Vitals        │ Classification │
│ detector.py    │ regressor.py   │ vitals/       │ classifier.py  │
│                │ blob_live.py   │ breathing_ml  │ multi_ap.py    │
│                │ blob3d/        │ sleep.py      │                │
│                │                │ doppler.py    │                │
├────────────────┴────────────────┴───────────────┴────────────────┤
│ Shared Signal Processing (csi/signal/)                            │
│ filter.py  stats.py  spectrogram.py  bvp.py  motion.py           │
│ fresnel.py  hampel.py  csi_ratio.py  features.py                  │
├──────────────────────────────────────────────────────────────────┤
│ Integration (csi/services/, csi/ha_bridge.py, csi/ws_client.py)  │
│ ServiceOrchestrator  HaBridge  SensingWsClient                    │
└──────────────────────────────────────────────────────────────────┘
```

## Module Map

### Data Ingestion

| Module | Files | Role |
|--------|-------|------|
| `csi/esp32_parser.py` | 1 | Binary UDP frame parser for ESP32 magic-number protocol (0xC511_0001/2/4). Dataclasses: `Esp32Frame`, `Esp32VitalsPacket`, `WasmOutputPacket` |
| `csi/csi_processor.py` | 1 | UDP listener (port 5005), frame demux across 9 TX×RX streams, WebSocket broadcast to `ws://:8765`, optional CSV dump |
| `csi/csi_record.py` | 1 | Offline CSI file recorder/replayer |

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
| `csi/ws_client.py` | 1 | `SensingWsClient` — asyncio WebSocket client for remote RuView-compatible sensing server. Yields typed `EdgeVitals`/`PoseData` messages |

### Tools

| File | Role |
|------|------|
| `csi/csi_plot.py` | Offline CSI data plotting |
| `csi/csi_mac.py` | MAC address scanner for ESP32 nodes |
| `csi/csi_ml.py` | Backward-compat shim re-exporting all symbols from `features.py`, `classifier.py`, `multi_ap.py`, `rssi_features.py`, `doppler.py`, `sleep.py`, `breathing_ml.py` |

### Firmware

| Path | Role |
|------|------|
| `firmware/esp32_radar3d/` | Cross-ping firmware: 3 ESP32 ping each other at 100 Hz → 9 stable CSI (TX,RX) channels. Channel 6 fixed. UDP broadcast |

## Dependencies

```
ESP32 UDP frames
      │
      ▼
esp32_parser.py ───┬─── csi_processor.py ─── WebSocket (:8765) ─── Browser UI
                   │
                   ├─── presence/detector.py
                   │
                   ├─── quadrants/blob_live.py ─── quadrants/ws_server.py
                   │         │
                   │         └─── quadrants/regressor.py
                   │
                   ├─── features.py ─── classifier.py / multi_ap.py
                   │
                   └─── vitals/pipeline.py
                             │
                             ├── vitals/preprocessor.py
                             ├── vitals/respiration.py ─── signal/filter.py
                             ├── vitals/heartrate.py  ─── signal/stats.py
                             └── vitals/anomaly.py
```

## Resource Budget

| Component | RAM | CPU | Storage |
|-----------|-----|-----|---------|
| ESP32 firmware | ~300 KB | Core 0 100%, Core 1 ~60% | ~844 KB flash |
| csi_processor (UDP+WS) | ~20 MB | <5% | — |
| PresenceDetector | ~100 KB | <1% | — |
| BlobEstimator | ~200 KB | <1% | — |
| PositionRegressor + Kalman | ~2 MB | <2% | ~50 KB (model) |
| CSIClassifier (RF) | ~10 MB | <5% | ~500 KB (model) |
| VitalSignPipeline | ~500 KB | <2% | — |
| Signal processing (all) | ~5 MB | <5% | — |
| **Total host** | **~50 MB** | **<15% on Cortex-A72** | — |
