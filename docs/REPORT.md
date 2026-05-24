# Technical Report — Arduino Wi-Fi Sensing

## Summary

Codebase cleanup + RuView feature port across 3 phases. **49 Python source files (11,817 lines), 11 test files (123 tests), 4 firmware files.**

| Metric | Before | After | Delta |
|--------|--------|-------|-------|
| Python files | 57 (est.) | 49 | -8 |
| Source lines | ~13,920 (est.) | 11,817 | -2,103 |
| Test count | 123 | 123 | 0 (all pass) |
| Modified files | — | 8 | — |
| Deleted files | — | 1 (blob_regressor.py) | -482 lines |
| Moved (tools→experimental) | — | 4 | — |
| New files | — | 16 | +2,950 lines |

---

## Phase 1 — Cleanup (Cat D, E, B1, C)

### Cat D: Experimental tools → `/experimental/`

Moved 4 standalone utilities with git history preserved:

| File | Destination | Reason |
|------|-------------|--------|
| `tools/diag_paths.py` | `experimental/diag_paths.py` | Diagnostic, not part of main pipeline |
| `tools/discover_macs.py` | `experimental/discover_macs.py` | One-shot MAC discovery |
| `tools/inject_radar3d_frames.py` | `experimental/inject_radar3d_frames.py` | Test injection tool |
| `tools/run_local.sh` | `experimental/run_local.sh` | Demo script |

### Cat E: Empty exception handlers → logging

Fixed 8 empty `except: pass` blocks across 4 files:

| File | Fixes |
|------|-------|
| `csi/blob_cli.py` | 1 (WebSocket send) |
| `csi/csi_mac.py` | 1 (UDP send) |
| `csi/csi_processor.py` | 4 (UDP recv, WebSocket send/recv) |
| `csi/quadrants/ws_server.py` | 2 (WebSocket broadcast) |

Each replaced with `logger.debug("...")` preserving existing behavior while adding debug visibility.

### Cat B1: Regressor consolidation

**Problem**: Two independent position regressors — `csi/quadrants/regressor.py` (`PositionRegressor`, LOO-cell CV, numpy Kalman, used by ws_server + blob3d) and `csi/blob_regressor.py` (`BlobRegressor`, sklearn RF, used by blob_cli).

**Decision**: Keep `quadrants/regressor.py` (strictly better architecture). Port motion detection + `train_continuous()` from blob_regressor.

**Changes**:
- Added to `PositionRegressor`: `motion_threshold_mps`/`motion_sustain_n` params, `speed`/`motion` fields in `PositionEstimate` dataclass, motion hysteresis logic in `predict()`, `train_continuous()` method
- Updated `csi/blob_cli.py`: import `PositionRegressor` instead of `BlobRegressor`, adapted training call, adapted monitor loop (uses `PositionEstimate` fields)
- Deleted `csi/blob_regressor.py` via `git rm` (-482 lines)

### Cat C: Split `csi/csi_ml.py` (1,932 → 0 lines)

Original monolith contained 7 unrelated feature extraction + classification subsystems. Split into focused modules:

| New module | Lines | Content |
|------------|-------|---------|
| `csi/features.py` | 356 | Feature extraction functions, no sklearn dep |
| `csi/classifier.py` | 628 | `CSIClassifier` + constants + CLI |
| `csi/multi_ap.py` | 221 | `MultiAPCSIClassifier` |
| `csi/rssi_features.py` | 255 | `RSSIFeatures` + `RSSIFeatureExtractor` |
| `csi/doppler.py` | 160 | `DopplerShiftExtractor` |
| `csi/sleep.py` | 167 | `SleepQualityAnalyzer` |
| `csi/breathing_ml.py` | 182 | `PhaseBreathingEstimator` |

**`csi/csi_ml.py`**: Rewritten as 72-line backward-compat shim — re-exports all symbols from new modules. Zero import path changes required across 13 dependent sites.

---

## Phase 2 — RuView Audit & Port

Source: `ruvnet/RuView` (64K ⭐, 2,126 files, Rust core + Python v1 legacy).

### Audit findings

Already ported (pre-existing):
- `csi/signal/` (7 modules) → maps to `wifi-densepose-signal` (Rust)
- `csi/vitals/` (7 modules) → maps to `wifi-densepose-vitals` (Rust)
- `csi/esp32_parser.py` → maps to `csi.rs` (Rust, 0xC511 magic numbers)
- `csi/phase_sanitizer.py` → maps to RuView PhaseSanitizer (Python legacy)

### 4 new features ported

| File | Lines | Description | RuView origin |
|------|-------|-------------|---------------|
| `csi/signal/filter.py` | 219 | `BiquadFilter` — Butterworth biquad IIR (lowpass/bandpass/highpass), multi-section cascade, per-sample streaming | `edge_processing.c` `biquad_bandpass_design`/`biquad_process` |
| `csi/signal/stats.py` | 141 | `WelfordOnline` — streaming mean/variance (O(1), numerically stable). `RunningMinMax`. Mergeable across streams | `edge_processing.c` Welford variance tracking |
| `csi/ha_bridge.py` | 243 | `HaBridge` — MQTT discovery + state publishing. 6 sensors + 3 binary sensors per node | ADR-115/ADR-117 P4 |
| `csi/ws_client.py` | 191 | `SensingWsClient` — asyncio WebSocket client yielding typed `EdgeVitals`/`PoseData`. Auto-reconnect | ADR-117 P4 |

### Remaining stealable features (gap analysis)

| Feature | Effort | Value | Status |
|---------|--------|-------|--------|
| CFO/SFO cancellation in CSI ratio | ~60 lines | Medium | Not ported |
| RVF binary container format | ~200 lines | Medium | Not ported |
| Home Assistant automation blueprint | ~100 lines | Medium | Not ported |
| Multi-BSSID WiFi scanning (ADR-022) | ~500+ lines | High | Needs firmware |
| NN-based pose estimation | Heavy dep | Low | Skip |
| Point cloud / Geo / MAT | Scope creep | Low | Skip |

---

## Phase 3 — Documentation

| File | Content |
|------|---------|
| `docs/ARCHITECTURE.md` | System architecture with ASCII data flow, full module map (34 modules, 10 categories), dependency graph, resource budget table |
| `docs/API.md` | Public API reference for 20+ modules — class signatures, method tables, code examples |
| `README.md` (updated) | Capability table expanded 9→16 rows (added heart rate, sleep, Doppler, RSSI features, signal processing, HA bridge, WS client) |

---

## Architecture decisions

| Decision | Rationale |
|----------|-----------|
| **Keep PositionRegressor, drop BlobRegressor** | PositionRegressor has LOO-cell CV, numpy Kalman, integrated with ws_server + blob3d. BlobRegressor was a near-duplicate with less capability |
| **Backward-compat shim for csi_ml.py** | 13 import sites across the codebase depend on `from csi.csi_ml import ...`. Shim avoids touching any of them |
| **Lazy imports for optional deps** | `paho-mqtt` and `websockets` are optional. Modules import guard at runtime, fail with clear error message |
| **BiquadFilter as standalone class** | Previously embedded as `_bandpass_filter` in `BreathingExtractor`. Now reusable by any module (vitals, signal, presence) |
| **No ML for presence/basics** | Variance-based state machine (EMPTY/STILL/MOTION) and RX-weighted centroid for position. Zero training required |
| **Experimental/ for incomplete tools** | Tools that work but aren't part of the main pipeline go to experimental/ with git history preserved |

---

## Current state

- **49 Python files**, **11,817 lines** (source) + **11 test files**
- **123 tests**, all passing (3 pre-existing pytest warnings, non-functional)
- **4 firmware files** (ESP32 cross-ping)
- **1 browser UI** (Three.js radar 3D + heatmap grid via WebSocket)
- **0 new runtime dependencies** — all optional imports guarded
- **8 docs files** covering architecture, API reference, hardware setup, capabilities, troubleshooting, testing
