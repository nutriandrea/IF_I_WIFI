# CSI Frame Format (ADR-018)

The wire contract between the ESP32-S3 firmware and the host receiver.
A frame is a single UDP datagram with the structure below. Everything
is little-endian.

## Header (20 bytes)

| Offset | Size | Field              | Type | Notes                                                  |
| -----: | ---: | ------------------ | ---- | ------------------------------------------------------ |
|  0     | 4    | `magic`            | u32  | `0xC5110001`. Decoder MUST reject any other value.      |
|  4     | 1    | `node_id`          | u8   | Provisioned per-device (1..255).                       |
|  5     | 1    | `n_antennas`       | u8   | Usually 1 on ESP32-S3.                                 |
|  6     | 2    | `n_subcarriers`    | u16  | Derived from payload length / (2 × `n_antennas`).      |
|  8     | 4    | `freq_mhz`         | u32  | Center frequency derived from WiFi channel.            |
| 12     | 4    | `sequence`         | u32  | Monotonic per-node counter.                            |
| 16     | 1    | `rssi`             | i8   | dBm as reported by `rx_ctrl.rssi`.                     |
| 17     | 1    | `noise_floor`      | i8   | dBm as reported by `rx_ctrl.noise_floor`.              |
| 18     | 1    | `ppdu_type`        | u8   | Unused in this build (always 0). Reserved.             |
| 19     | 1    | `flags`            | u8   | Unused in this build (always 0). Reserved.             |

Total: 20 bytes.

## Payload (variable)

After the header: `n_antennas × n_subcarriers × 2` bytes of raw I/Q.
Each subcarrier contributes two `i8` values, in this order:

```
[ I_0, Q_0, I_1, Q_1, ..., I_{n_sub-1}, Q_{n_sub-1} ]   (per antenna)
```

For a single-antenna ESP32-S3 with 64 HT subcarriers, payload size is
`1 × 64 × 2 = 128` bytes, so the full UDP datagram is 148 bytes.

## Channel → frequency mapping (firmware-side)

```
ch  1..13   → 2412 + (ch - 1) * 5 MHz
ch  14      → 2484 MHz
ch  36..177 → 5000 + ch * 5 MHz
otherwise   → 0
```

## Sync packets (distinct magic)

The firmware does **not** emit sync packets in this build, but the host
decoder still recognizes the legacy upstream sync magic `0xC511A110`
and silently drops it (counted in `RxStats.sync_packets`). This is so a
mixed deployment with one upstream-firmware node doesn't spam the host
with "bad magic" errors.

## Reference C encoder

See [`firmware/.../csi_collector.c::csi_serialize_frame`](../firmware/esp32-csi-node/main/csi_collector.c).

## Reference Rust decoder

See [`host/crates/ifi-core/src/lib.rs::decode`](../host/crates/ifi-core/src/lib.rs)
— pure function, no allocator beyond the output `Vec<u8>` for the payload.

## Versioning

If you ever need to extend the header, do not change `magic`. Pick a new
magic for the v2 format (e.g. `0xC5110002`) and have both decoders
recognize both. This is what the upstream project did with ADR-110 sync
packets, and it works.
