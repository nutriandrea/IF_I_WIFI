"""
ESP32 binary frame parser for WiFi CSI data.

Parses binary UDP packets from ESP32 devices using magic-number
based protocol identification:
  - 0xC511_0001: Full CSI frame (I/Q pairs per subcarrier/antenna)
  - 0xC511_0002: Edge vitals packet (breathing, heartrate, presence)
  - 0xC511_0004: WASM module output events

Adapted from RuView wifi-densepose-sensing-server (csi.rs).
"""

import math
import struct
from dataclasses import dataclass, field
from typing import Optional


CSI_FRAME_MAGIC = 0xC511_0001
CSI_VITALS_MAGIC = 0xC511_0002
CSI_WASM_MAGIC = 0xC511_0004


@dataclass
class Esp32Frame:
    """Full CSI frame with per-antenna I/Q data."""
    magic: int
    node_id: int
    n_antennas: int
    n_subcarriers: int
    freq_mhz: int
    sequence: int
    rssi: int
    noise_floor: int
    amplitudes: list[float]
    phases: list[float]


@dataclass
class Esp32VitalsPacket:
    """Edge-computed vitals packet (32 bytes)."""
    node_id: int
    presence: bool
    fall_detected: bool
    motion: bool
    breathing_rate_bpm: float
    heartrate_bpm: float
    rssi: int
    n_persons: int
    motion_energy: float
    presence_score: float
    timestamp_ms: int


@dataclass
class WasmEvent:
    """Single WASM module output event."""
    event_type: int
    value: float


@dataclass
class WasmOutputPacket:
    """WASM module output packet."""
    node_id: int
    module_id: int
    events: list[WasmEvent]


@dataclass
class SignalField:
    """2D signal field visualization grid."""
    grid_size: int
    values: list[list[float]]


def parse_esp32_frame(buf: bytes) -> Optional[Esp32Frame]:
    """
    Parse a full CSI frame packet (magic 0xC511_0001).

    Header layout (20 bytes):
      Offset  Size  Type     Field
      0       4     u32      magic
      4       1     u8       node_id
      5       1     u8       n_antennas
      6       1     u8       n_subcarriers
      7       1     -        reserved
      8       2     u16      freq_mhz
      10      4     u32      sequence
      14      1     i8       rssi_raw
      15      1     i8       noise_floor
      16      4     -        reserved

    After header: for each of (n_antennas * n_subcarriers), 2 signed bytes (I, Q).

    Returns None if the buffer is too short or magic number mismatches.
    """
    if len(buf) < 20:
        return None

    magic = struct.unpack_from('<I', buf, 0)[0]
    if magic != CSI_FRAME_MAGIC:
        return None

    node_id = struct.unpack_from('<B', buf, 4)[0]
    n_antennas = struct.unpack_from('<B', buf, 5)[0]
    n_subcarriers = struct.unpack_from('<B', buf, 6)[0]
    freq_mhz = struct.unpack_from('<H', buf, 8)[0]
    sequence = struct.unpack_from('<I', buf, 10)[0]
    rssi_raw = struct.unpack_from('<b', buf, 14)[0]
    noise_floor = struct.unpack_from('<b', buf, 15)[0]

    if rssi_raw > 0:
        rssi_raw = -rssi_raw

    n_pairs = n_antennas * n_subcarriers
    iq_start = 20
    expected_len = iq_start + n_pairs * 2
    if len(buf) < expected_len:
        return None

    amplitudes = []
    phases = []
    for k in range(n_pairs):
        offset = iq_start + k * 2
        i_val = struct.unpack_from('<b', buf, offset)[0]
        q_val = struct.unpack_from('<b', buf, offset + 1)[0]
        amplitudes.append(math.sqrt(i_val * i_val + q_val * q_val))
        phases.append(math.atan2(q_val, i_val))

    return Esp32Frame(
        magic=magic,
        node_id=node_id,
        n_antennas=n_antennas,
        n_subcarriers=n_subcarriers,
        freq_mhz=freq_mhz,
        sequence=sequence,
        rssi=rssi_raw,
        noise_floor=noise_floor,
        amplitudes=amplitudes,
        phases=phases,
    )


def parse_esp32_vitals(buf: bytes) -> Optional[Esp32VitalsPacket]:
    """
    Parse a 32-byte edge vitals packet (magic 0xC511_0002).

    Layout:
      Offset  Size  Type     Field
      0       4     u32      magic
      4       1     u8       node_id
      5       1     u8       flags
      6       2     u16      breathing_raw (BPM * 100)
      8       4     u32      heartrate_raw (BPM * 10000)
      12      1     i8       rssi
      13      1     u8       n_persons
      14      2     -        reserved
      16      4     f32      motion_energy
      20      4     f32      presence_score
      24      4     u32      timestamp_ms

    Returns None if the buffer is too short or magic number mismatches.
    """
    if len(buf) < 32:
        return None

    magic = struct.unpack_from('<I', buf, 0)[0]
    if magic != CSI_VITALS_MAGIC:
        return None

    node_id = struct.unpack_from('<B', buf, 4)[0]
    flags = struct.unpack_from('<B', buf, 5)[0]
    breathing_raw = struct.unpack_from('<H', buf, 6)[0]
    heartrate_raw = struct.unpack_from('<I', buf, 8)[0]
    rssi = struct.unpack_from('<b', buf, 12)[0]
    n_persons = struct.unpack_from('<B', buf, 13)[0]
    motion_energy = struct.unpack_from('<f', buf, 16)[0]
    presence_score = struct.unpack_from('<f', buf, 20)[0]
    timestamp_ms = struct.unpack_from('<I', buf, 24)[0]

    return Esp32VitalsPacket(
        node_id=node_id,
        presence=bool(flags & 0x01),
        fall_detected=bool(flags & 0x02),
        motion=bool(flags & 0x04),
        breathing_rate_bpm=breathing_raw / 100.0,
        heartrate_bpm=heartrate_raw / 10000.0,
        rssi=rssi,
        n_persons=n_persons,
        motion_energy=motion_energy,
        presence_score=presence_score,
        timestamp_ms=timestamp_ms,
    )


def parse_wasm_output(buf: bytes) -> Optional[WasmOutputPacket]:
    """
    Parse a WASM module output packet (magic 0xC511_0004).

    Header (8 bytes):
      Offset  Size  Type     Field
      0       4     u32      magic
      4       1     u8       node_id
      5       1     u8       module_id
      6       2     u16      event_count

    Then for each event (5 bytes each):
      - event_type (B, 1 byte)
      - value (f, 4 bytes)

    Returns None if the buffer is too short or magic number mismatches.
    """
    if len(buf) < 8:
        return None

    magic = struct.unpack_from('<I', buf, 0)[0]
    if magic != CSI_WASM_MAGIC:
        return None

    node_id = struct.unpack_from('<B', buf, 4)[0]
    module_id = struct.unpack_from('<B', buf, 5)[0]
    event_count = struct.unpack_from('<H', buf, 6)[0]

    events = []
    offset = 8
    for _ in range(event_count):
        if offset + 5 > len(buf):
            break
        event_type = struct.unpack_from('<B', buf, offset)[0]
        value = struct.unpack_from('<f', buf, offset + 1)[0]
        events.append(WasmEvent(event_type=event_type, value=value))
        offset += 5

    return WasmOutputPacket(
        node_id=node_id,
        module_id=module_id,
        events=events,
    )


def generate_signal_field(
    mean_rssi: float,
    motion_score: float,
    breathing_rate_hz: float,
    signal_quality: float,
    subcarrier_variances: list[float],
) -> SignalField:
    """
    Generate a 20x20 signal field visualization grid from sensing features.

    Places Gaussian blobs around a center point for each subcarrier variance
    component, adds a signal-quality falloff, and optionally adds a breathing
    ring when breathing_rate_hz > 0.05.

    Args:
        mean_rssi: Mean RSSI value (currently unused; reserved).
        motion_score: Scalar motion intensity [0..1].
        breathing_rate_hz: Estimated breathing rate in Hz.
        signal_quality: Overall signal quality score [0..1].
        subcarrier_variances: Per-subcarrier variance values.

    Returns:
        SignalField with a 20x20 grid of values normalized to [0, 1].
    """
    grid = 20
    values = [[0.0] * grid for _ in range(grid)]
    center = (grid - 1) / 2.0

    max_var = max(subcarrier_variances) if subcarrier_variances else 0.0
    norm_factor = max_var if max_var > 1e-9 else 1.0
    n_sub = max(len(subcarrier_variances), 1)

    for k, var in enumerate(subcarrier_variances):
        weight = (var / norm_factor) * motion_score
        if weight < 1e-6:
            continue
        angle = (k / n_sub) * 2.0 * math.pi
        radius = center * 0.8 * math.sqrt(weight)
        hx = center + radius * math.cos(angle)
        hz = center + radius * math.sin(angle)
        spread = max(0.5 + weight * 2.0, 0.5)
        for z in range(grid):
            for x in range(grid):
                dx = x - hx
                dz = z - hz
                dist2 = dx * dx + dz * dz
                values[z][x] += weight * math.exp(-dist2 / (2.0 * spread * spread))

    for z in range(grid):
        for x in range(grid):
            dx = x - center
            dz = z - center
            dist = math.sqrt(dx * dx + dz * dz)
            base = signal_quality * math.exp(-dist * 0.12)
            values[z][x] += base * 0.3

    if breathing_rate_hz > 0.05:
        ring_r = center * 0.55
        ring_width = 1.8
        for z in range(grid):
            for x in range(grid):
                dx = x - center
                dz = z - center
                dist = math.sqrt(dx * dx + dz * dz)
                ring_val = 0.08 * math.exp(-((dist - ring_r) ** 2) / (2.0 * ring_width * ring_width))
                values[z][x] += ring_val

    field_max = max(max(row) for row in values)
    scale = 1.0 / field_max if field_max > 1e-9 else 1.0
    for z in range(grid):
        for x in range(grid):
            values[z][x] = max(0.0, min(values[z][x] * scale, 1.0))

    return SignalField(grid_size=grid, values=values)
