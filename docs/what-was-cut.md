# What was cut from RuView, and why

This document is the audit trail for the IF I WIFI extraction. It
intentionally errs on the side of "explain too much" because the
upstream codebase carries a lot of inertia and we want our reasons on
record.

## Two categories

1. **Cut because it's host-heavy and doesn't belong on an embedded
   target.** These are legitimate features in the upstream project, but
   they don't have a path to a constrained device. Keeping them here
   would either bloat the binary or force us to maintain a stub.
2. **Cut because we don't believe the claim.** Some upstream modules
   make biomedical / cognitive claims that aren't supported by the code
   or the cited references. We list them here so it's clear what is
   *not* in IF I WIFI by design.

## Cut: host-heavy components

| Upstream module                                         | Why removed |
| ------------------------------------------------------- | ----------- |
| `v2/crates/wifi-densepose-nn`                           | Inference backends (ONNX / Tch / Candle). Models are 50–200 MB. Not embedded-tractable. |
| `v2/crates/wifi-densepose-train`                        | Training pipeline. Training is host-only by definition. |
| `v2/crates/wifi-densepose-mat`                          | Mass-casualty assessment + survivor tracking. 19k LOC, depends on the NN pipeline. |
| `v2/crates/wifi-densepose-ruvector`                     | Pulls in an external `ruvector` repo (sublinear solvers, mincut, attention). Out of scope. |
| `v2/crates/wifi-densepose-sensing-server`               | 31k LOC Axum server with REST + WebSocket + multistatic bridges. We keep a much smaller UDP receiver in `ifi-transport` and a stub web visualizer. |
| `v2/crates/wifi-densepose-desktop`                      | Tauri/desktop app. Visualization in IF I WIFI is a single HTML file. |
| `v2/crates/wifi-densepose-wasm` / `-wasm-edge`          | WASM runtime + 30 "exo_*/qnt_*/med_*" modules (see next section). |
| `v2/crates/nvsim` / `nvsim-server`                      | NV-diamond magnetometer simulator. Unrelated to WiFi CSI. |
| `v2/crates/cog-*` (3 crates)                            | Packaging for a separate "Cognitum" appliance — not a runtime component. |
| `v2/crates/wifi-densepose-bfld`                         | Beamforming-feedback privacy layer. Early scaffold (366 LOC at extraction time). |
| `v2/crates/wifi-densepose-pointcloud` / `-geo`          | Point cloud + geo pipelines. Useful but not core CSI. |
| `v2/crates/ruv-neural/` (12 nested crates)              | Independent sub-workspace (own Cargo.toml, own keywords) that happened to live in the same repo. Not integrated into the CSI pipeline. |
| `vendor/midstream`, `vendor/ruvector`, `vendor/rvcsi`, `vendor/sublinear-time-solver` | Four git submodules from related projects. None are required for CSI capture + DSP. |
| `archive/v1/` (Python, ~40k LOC)                        | Legacy Python prototype. Dropped wholesale. |
| Firmware: OTA update server                             | `ota_update.c` + HTTP server. Use `idf.py flash` over USB instead. |
| Firmware: WASM runtime + upload endpoint                | `wasm_runtime.c` + `wasm_upload.c`. Programmable sensing modules on-device — not in our scope. |
| Firmware: mmWave sensor (`mmwave_sensor.c`)             | UART driver for HLK-LD2410 / Seeed MR60BHA2. Useful but separate concern. |
| Firmware: swarm bridge (`swarm_bridge.c`)               | HTTP heartbeat / vector ingest into a Cognitum Seed. |
| Firmware: AMOLED display (`display_*.c`)                | LVGL UI for board-mounted display. |
| Firmware: ADR-110 stack (`c6_twt`, `c6_timesync`, `c6_sync_espnow`, `c6_softap_he`, `c6_lp_core`) | ESP32-C6 TWT / 802.15.4 mesh / LP-core gating. C6 is not in scope for this minimal build. |
| Firmware: adaptive controller (`adaptive_controller.c`, `rv_radio_ops_*`, `rv_mesh.c`, `rv_feature_state.c`) | Multi-node mesh adaptation. Out of scope for a single-node firmware. |
| Firmware: mock CSI generator (`mock_csi.c`)             | Useful for QEMU testing — we develop against real hardware here. |
| Firmware: edge processing pipeline (`edge_processing.c`, 1078 LOC) | Includes vitals extraction, presence detection, fall detection, RVF parser. The DSP is real, but it lives on-device coupled to NVS-tunable thresholds and a Kalman tracker. We keep CSI capture pure and do DSP host-side. |
| ADR documents 1..121 in `docs/adr/`                     | The upstream project has 121 ADRs at extraction time. We do not carry that backlog. New ADRs (if any) start at 001 here. |

## Cut: claims we don't believe

These are all in `v2/crates/wifi-densepose-wasm-edge/src/` upstream.
None of them have a peer-reviewed benchmark or a reproducible
evaluation in the upstream repo. We exclude them by default; if you want
to revive any of them, put them under [`experiments/`](../experiments)
with an actual evaluation harness.

| Upstream module                          | Claim                                                              | Why excluded |
| ---------------------------------------- | ------------------------------------------------------------------ | ------------ |
| `exo_happiness_score.rs`                 | "Happiness score" from CSI.                                        | Not a measurable physical quantity from CSI. |
| `exo_dream_stage.rs`                     | Dream-stage (REM/NREM) detection.                                  | Sleep staging needs EEG / accelerometer ground truth; no validation provided. |
| `exo_emotion_detect.rs`                  | Emotion detection from CSI.                                        | Same problem as happiness: no operational definition. |
| `exo_ghost_hunter.rs`                    | "Ghost hunter."                                                    | Not a scientific claim. |
| `exo_music_conductor.rs`                 | Conducting gestures.                                               | Plausible as a gesture-recognition demo, but framed as a serious feature. |
| `aut_psycho_symbolic.rs`                 | "Psycho-symbolic" processing.                                      | Pseudoscientific framing. |
| `qnt_quantum_coherence.rs`, `qnt_interference_search.rs` | "Quantum" applied to 2.4 GHz WiFi signals.                         | The word is being used as a synonym for "phase coherence." Misleading. |
| `lrn_ewc_lifelong.rs`                    | EWC lifelong learning at the WASM edge.                            | EWC needs Fisher information accumulation; doing this on an MCU at meaningful scale is not realistic. |
| `med_seizure_detect.rs`, `med_cardiac_arrhythmia.rs`, `med_sleep_apnea.rs`, `med_respiratory_distress.rs` | Medical diagnostic claims.                                         | These need regulatory validation (FDA / CE-MDR) before being shipped as features. The upstream code has no such validation. |
| `v2/crates/wifi-densepose-signal/src/ruvsense/intention.rs` | Detect "intention" 200–500 ms before movement, from CSI embeddings. | Cited reference (Massion 1992) is a generic neurobiology review that doesn't support CSI-based detection. The implementation is real (1st/2nd derivatives in embedding space), but the framing oversells what it can do. We keep the coherence gate from the same `ruvsense/` directory but drop this one. |

## Naming / branding inflation also removed

The upstream code uses internal codenames as if they were established
technologies: `AETHER` (re-ID embeddings), `MERIDIAN` (cross-environment
generalization), `RVDNA` (vitals pipeline), `SONA` (self-learning),
`RVF` (rapid sensing format). They appear in module names, ADRs, and
doc comments. We renamed or stripped them so the naming reflects what
the code does: `CoherenceGate` instead of `AETHER coherence`,
`CsiFrame` instead of `RVF frame`, etc.

If you ever need to grep back into the upstream repo, the codenames are
still there.

## Numbers, for posterity

| Repo            | Rust LOC | C LOC  | Python LOC | ADRs |
| --------------- | -------: | -----: | ---------: | ---: |
| RuView upstream | 184,632  | 14,125 | 39,981     | 121  |
| IF I WIFI       |   ~1,800 |  ~750  | 0          | 0    |

That's about **1.0% of the upstream Rust surface, 5% of the firmware C, and
zero Python**. The point of this fork is not "RuView is bad" — it's that
~99% of upstream's surface is not on the path from CSI to a useful
signal, and an embedded-first project shouldn't carry that weight.
