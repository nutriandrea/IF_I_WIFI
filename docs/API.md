# API Reference

## csi.features

### `extract_csi_profile(amplitudes, phases, rssi, n_sub, n_ant) -> dict`
Extract statistical features from one CSI frame (antenna-averaged).

- **Input**: `amplitudes` (list[float]), `phases` (list[float]), `rssi` (int), `n_sub` (int), `n_ant` (int)
- **Output**: dict with mean, std, min, max, 25/50/75 percentiles, skew, kurtosis, SNR for both amplitude and phase. RSSI mean + std across antennas.

### `csi_window_to_vector(frames, n_sub, n_ant) -> np.ndarray`
Aggregate a window of CSI frames into a flat feature vector.

- **Input**: `frames` (list of dict from `extract_csi_profile`), `n_sub` (int), `n_ant` (int)
- **Output**: 1D np.ndarray of concatenated statistical features.

## csi.classifier

### `CSIClassifier`
RandomForest-based CSI grid cell classifier.

| Method | Signature | Description |
|--------|-----------|-------------|
| `train` | `(X, y)` | Train RF on feature matrix `X` and labels `y` |
| `train_custom` | `(X, y, params)` | Train with custom RF params (n_estimators, max_depth, etc.) |
| `predict` | `(X) -> np.ndarray` | Predict class labels |
| `predict_proba` | `(X) -> np.ndarray` | Predict class probabilities |
| `save` | `(path)` | Save model to .joblib |
| `load` | `(path)` | Load model from .joblib |
| `save_custom` | `(path, params)` | Save model + training metadata |
| `trained` | `-> bool` | Whether model has been trained |
| `ready` | `-> bool` | Whether model is ready for inference |

CLI: `python -m csi.classifier [train|predict|eval]`

## csi.multi_ap

### `MultiAPCSIClassifier`
Multi-antenna-port RSSI-weighted CSI classifier.

| Method | Signature | Description |
|--------|-----------|-------------|
| `add_frame` | `(node_id, amplitudes, phases, rssi, n_sub, n_ant)` | Add a CSI frame from one node |
| `train` | `(labels)` | Train after collecting frames from all nodes |
| `predict_proba` | `() -> np.ndarray` | Predict probability distribution from current window |
| `predict` | `() -> int` | Predict most likely cell |
| `save` | `(path)` | Save classifier |
| `load` | `(path)` | Load classifier |
| `trained` | `-> bool` | Whether trained |
| `ready` | `-> bool` | Whether ready to predict |

Frame collection: expects frames from multiple nodes (e.g., 3 ESP32s). Features are fused using cross-node RSSI weighting.

## csi.rssi_features

### `RSSIFeatures`
Container for RSSI-derived features.

| Method | Output |
|--------|--------|
| `to_dict()` | dict of all feature values |
| `to_vector()` | flat np.ndarray of all features |

Constants: `RSSI_FEATURE_NAMES` — list of 12 feature names (CUSUM, FFT band powers, temporal stats, skew, kurtosis).

### `RSSIFeatureExtractor`
Extract RSSI features from a stream of RSSI values.

| Method | Signature | Description |
|--------|-----------|-------------|
| `extract` | `(rssi_values: list) -> RSSIFeatures` | Extract features from RSSI time series (CUSUM, FFT 0-3Hz band, temporal stats) |

## csi.doppler

### `DopplerShiftExtractor`
FFT-based Doppler shift estimation from CSI phase differences.

| Method | Signature | Description |
|--------|-----------|-------------|
| `add_frame` | `(phases: np.ndarray)` | Add a frame's phase values |
| `compute` | `() -> dict` | Compute Doppler profile. Returns dict with mean, std, max positive/negative, abs max, band power (0.5-10 Hz) |
| `ready` | `-> bool` | Whether enough frames accumulated |
| `n_frames` | `-> int` | Number of frames collected |

Constants: `DOPPLER_FEATURE_NAMES` — list of 7 feature names.

## csi.sleep

### `SleepQualityAnalyzer`
Sleep quality estimation from breathing regularity.

| Method | Signature | Description |
|--------|-----------|-------------|
| `analyze` | `(breathing_features: dict) -> dict` | Returns: `stage` (awake/light/deep), `confidence`, `apnea_detected` (bool) |
| `reset` | `()` | Reset internal state |

Heuristic rules: deep sleep = high regularity + low rate; apnea = breathing drops below threshold for window.

Constants: `SLEEP_FEATURE_NAMES` — list of 7 names.

## csi.breathing_ml

### `PhaseBreathingEstimator`
Breathing rate estimation from unwrapped CSI phase.

| Method | Signature | Description |
|--------|-----------|-------------|
| `add_frame` | `(phase: np.ndarray)` | Add sanitized phase frame |
| `estimate` | `() -> dict` | Compute BPM via FFT peak. Returns `{bpm, confidence, valid}` |
| `bpm` | `-> float` | Current BPM estimate |
| `ready` | `-> bool` | Whether enough data (>=150 frames) |
| `reset` | `()` | Clear state |

Freq range: 0.1-0.5 Hz (6-30 BPM). Requires ~3s at 50 Hz.

## csi.phase_sanitizer

### `PhaseSanitizer`
CSI phase cleaning pipeline.

| Method | Signature | Description |
|--------|-----------|-------------|
| `sanitize` | `(phase: np.ndarray) -> np.ndarray` | Full pipeline: unwrap → outlier → smooth |
| `unwrap_phase` | `(phase: np.ndarray) -> np.ndarray` | Remove 2π discontinuities |
| `remove_outliers` | `(phase: np.ndarray) -> np.ndarray` | Z-score outlier rejection + linear interpolation |
| `smooth_phase` | `(phase: np.ndarray) -> np.ndarray` | Moving average (configurable window) |
| `phase_difference` | `(phase: np.ndarray, axis=0) -> np.ndarray` | Frame-to-frame phase diff |

Params: `unwrap_method` ('numpy'/'scipy'), `outlier_threshold` (default 3.0), `smoothing_window` (default 5).

## csi.signal

### `BiquadFilter`
Multi-section Butterworth biquad IIR filter.

```python
bf = BiquadFilter(sample_rate=100.0, cutoff_low=0.1, cutoff_high=0.5)
# Per-sample streaming:
for sample in stream:
    filtered = bf.process(sample)
# Batch processing:
output = bf.filter(samples)
```

| Method | Signature | Description |
|--------|-----------|-------------|
| `process` | `(x: float) -> float` | Filter one sample |
| `filter` | `(samples: list[float]) -> list[float]` | Filter entire buffer |
| `reset` | `()` | Zero all section states |

Params: `sample_rate`, `cutoff` (for lowpass/highpass), `cutoff_low`+`cutoff_high` (bandpass), `order` (even, default 2).

### `WelfordOnline`
Streaming mean/variance (O(1) memory).

```python
w = WelfordOnline()
for x in stream:
    w.update(x)
    print(f"mean={w.mean():.2f}, std={w.std():.2f}")
```

| Method | Output |
|--------|--------|
| `mean()` | float — arithmetic mean |
| `variance()` | float — sample or population variance |
| `std()` | float — standard deviation |
| `z_score(value)` | float — distance from mean in std |
| `merge(other)` | None — combine two WelfordOnline instances |
| `to_dict()` | dict — `{count, mean, variance, std}` |

### `RunningMinMax`
Tracks rolling min, max, range.

### `Spectrogram`
```python
spec = compute_spectrogram(signal, sample_rate, config)
spec.to_db()  # -> np.ndarray in dB
```

### `BodyVelocityProfile`
```python
bvp = extract_bvp(csi_amplitude_matrix, sample_rate, config)
# bvp.data: (n_velocity_bins x n_time_frames)
```

### `MotionDetector`
```python
md = MotionDetector(config)
result = md.detect_human(amplitude_features, phase_features, doppler_features)
# result: HumanDetectionResult with human_detected, confidence, motion_score
```

### `FresnelGeometry`
```python
fg = FresnelGeometry(d_tx_body=2.0, d_body_rx=1.5, frequency=5.8e9)
fg.fresnel_radius(n=1)              # 1st Fresnel zone radius
fg.phase_change(displacement_m=0.01)  # phase shift from 1cm chest movement
```

### `CsiRatioProcessor`
```python
crp = CsiRatioProcessor()
ratio = crp.process(csi_complex)  # (n_antennas, n_subcarriers) -> (n_pairs, n_subcarriers)
```

## csi.vitals

### `VitalSignPipeline`
Full vitals pipeline, single entry point.

```python
pipeline = VitalSignPipeline(n_subcarriers=56, sample_rate=100.0)
reading, alerts = pipeline.process_frame(amplitudes, phases, sample_rate, sample_index)
# reading: VitalReading with respiratory_rate + heart_rate + signal_quality
# alerts: list of AnomalyAlert
```

### Data types

| Class | Fields |
|-------|--------|
| `VitalEstimate` | `value_bpm`, `confidence` (0-1), `status` (Valid/Degraded/Unreliable/Unavailable) |
| `VitalReading` | `respiratory_rate` (VitalEstimate), `heart_rate` (VitalEstimate), `subcarrier_count`, `signal_quality`, `timestamp_secs` |
| `VitalStatus` | `Valid`, `Degraded`, `Unreliable`, `Unavailable` |

## csi.presence

### `PresenceDetector`
Variance-based presence state machine.

```python
pd = PresenceDetector(empty_mult=1.8, move_mult=2.5, window_s=1.0, dwell_ms=500)
pd.add_frame(ampl_mean, rssi)  # per CSI path, call once per frame
reading = pd.current_reading()  # -> PresenceReading
```

| Method | Description |
|--------|-------------|
| `add_frame(ampl_mean, rssi)` | Feed one CSI amplitude measurement |
| `current_reading()` | Get current state + confidence |
| `is_calibrated()` | Whether baseline collected |
| `reset_calibration()` | Clear baseline, restart calibration |

States: `EMPTY`, `STILL`, `MOTION`. Configurable multipliers, hysteresis, dwell time.

## csi.quadrants

### `BlobEstimator`
No-ML position estimation via variance-weighted RX centroid.

```python
be = BlobEstimator(rx_positions=[(x0,y0), (x1,y1), ...], window_s=1.0)
be.add_frame(rx_idx, ampl_variance)
estimate = be.estimate()  # -> BlobEstimate(x, y, x_std, y_std)
```

### `PositionRegressor`
ML-based position regressor with Kalman smoothing.

```python
pr = PositionRegressor(rx_positions, room_dims, grid_shape)
pr.train(X, y)              # Train RF multi-output regressor
pred = pr.predict(X)        # Predict (x, y)
estimate = pr.update(x, y)  # Kalman-filtered position
pr.train_continuous(data)   # Online training from labeled data
```

| Method | Description |
|--------|-------------|
| `train(X, y)` | Train RF from feature matrix + target coordinates |
| `predict(X)` | Predict raw `(x, y)` |
| `update(x, y)` | Kalman-filtered `PositionEstimate(x, y, speed, motion, ...)` |
| `train_continuous(data)` | Incremental training from `dict[tuple, list]` |

## csi.esp32_parser

### `parse_esp32_frame(buf: bytes) -> Esp32Frame | None`
Parse binary UDP packet into typed frame.

### `parse_esp32_vitals(buf: bytes) -> Esp32VitalsPacket | None`
Parse magic 0xC511_0002 vitals packet.

## csi.ha_bridge

### `HaBridge`
Home Assistant MQTT integration.

```python
bridge = HaBridge(node_id="esp32_0", mqtt_host="core-mosquitto")
bridge.connect()
bridge.publish_vitals(
    breathing_rate_bpm=16.2,
    heart_rate_bpm=72.0,
    presence=True,
    motion=True,
    breathing_confidence=0.85,
    heart_rate_confidence=0.72,
)
```

| Method | Description |
|--------|-------------|
| `connect()` | Connect to MQTT broker |
| `disconnect()` | Disconnect from MQTT broker |
| `publish_discovery()` | Publish HA MQTT discovery config (6 sensors + 3 binary sensors) |
| `publish_vitals(...)` | Publish vitals to HA state topic |

Published entities: breathing_rate, heart_rate, breathing_confidence, heart_rate_confidence, motion_energy, rssi (sensors) + presence, motion, fall_detected (binary sensors).

## csi.ws_client

### `SensingWsClient`
Asyncio WebSocket client for RuView-compatible sensing server.

```python
client = SensingWsClient("ws://192.168.1.100:8765/ws/sensing")
async with client:
    async for msg in client.stream():
        if isinstance(msg, EdgeVitals):
            print(f"BR={msg.breathing_rate_bpm}, HR={msg.heartrate_bpm}")
```

Message types: `EdgeVitals`, `ConnectionEstablished`, `PoseData`, `WsMessage` (fallback).

## csi.services

### `ServiceOrchestrator`
Async service lifecycle manager.

```python
orch = ServiceOrchestrator(settings={"health_check_interval_s": 30.0})
await orch.initialize()
await orch.start()
# ... run ...
await orch.shutdown()
```

### `HealthCheckService`
Component-level health monitoring.

```python
health = HealthCheckService(check_interval_s=30.0)
health.register_component("csi_processor")
health.update_component("csi_processor", HealthStatus.HEALTHY, "Processing")
health.record_error("csi_processor", "Timeout")
summary = health.get_summary()
```

### `MetricsService`
Time-series metric collection.

```python
metrics = MetricsService(retention=1000)
metrics.record("processing_latency_ms", 12.5, {"module": "doppler"})
latest = metrics.get_latest("processing_latency_ms")
avg = metrics.get_average("processing_latency_ms", timedelta(minutes=5))
```
