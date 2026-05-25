# 0001 — Extracted from RuView

**Date:** 2026-05-24
**Status:** Accepted

## Context

`ruvnet/wifi-densepose` (a.k.a. RuView) is a 180k+ LOC Rust project with
121 ADRs, four git submodules, a separate nested workspace
(`ruv-neural/`), and a 30+ module "wasm-edge" component that ships
features like "happiness score from WiFi" and "ghost hunter." About 1.0%
of its Rust surface is on the path from CSI capture to a useful signal.

We want a small, embedded-first project we can reason about end-to-end.

## Decision

Fork the parts of RuView that are (a) actually wired together
end-to-end and (b) realistic to run on a constrained host, into a new
repository named **IF I WIFI**. Strip everything else.

The kept components are:

1. The ESP32-S3 CSI capture path (firmware C, ~700 LOC after stripping).
2. The ADR-018 binary wire format.
3. The `CsiFrame` type and an associated decoder (clean rewrite, ~250 LOC Rust).
4. The Hampel filter (batch → streaming port, ~120 LOC Rust).
5. The coherence gate (simplified port, ~80 LOC Rust).
6. A blocking UDP receiver + bounded ring buffer (~170 LOC Rust).
7. A single CLI binary (`ifiwifi-capture`, ~150 LOC Rust).

Everything else from upstream is dropped. See
[`../what-was-cut.md`](../what-was-cut.md) for the line-by-line list and
the reasons.

## Consequences

**Good:**

- A new contributor can read the entire codebase in an afternoon.
- The firmware binary shrinks from 1.1 MB to ~250 KB, leaving room on
  4 MB ESP32-S3 boards.
- No external git submodules. No nested workspaces. One Cargo.toml at
  `host/`. One CMake project at `firmware/esp32-csi-node/`.
- Honest about hardware: we don't pretend Arduino UNO R4 WiFi can run
  the DSP.

**Bad:**

- We give up upstream's pose-estimation pipeline. If we need pose, we
  either bring it back as a host-only optional crate behind a feature
  flag, or we add an `experiments/` prototype.
- We give up upstream's multi-AP fusion (multistatic, cross-room,
  AETHER re-ID). These are real engineering, just out of scope for a
  single-node sensor + single-host receiver.
- We carry a maintenance debt: any future bug in `csi_collector.c` may
  also exist upstream, and we have to decide each time whether to port
  the fix or fork.

## Open questions

- Do we ever want to publish the host crates to crates.io as
  `ifi-core` / `ifi-dsp`? Probably not yet — too early.
- The Rust workspace isn't verified to compile because the dev
  environment used to extract this didn't have a Rust toolchain
  installed. First action after merging this ADR: install Rust and run
  `cargo test --workspace`. If anything fails, fix it before
  advertising the repo.
