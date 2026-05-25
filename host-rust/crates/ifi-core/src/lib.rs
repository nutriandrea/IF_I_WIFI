//! Core types and ADR-018 binary CSI frame codec.
//!
//! A `CsiFrame` is the host-side representation of one CSI callback fired
//! by the ESP-IDF WiFi driver on the sensor. The wire format is a fixed
//! 20-byte header followed by raw I/Q bytes. See `docs/csi-frame-format.md`
//! for the byte layout.
//!
//! This crate has no dependencies other than (optionally) `thiserror`. It
//! is `no_std`-compatible when the `std` feature is disabled, which makes
//! it usable on Cortex-M class receivers if you ever want to do the
//! decoding on an MCU.

#![cfg_attr(not(feature = "std"), no_std)]

#[cfg(not(feature = "std"))]
extern crate alloc;

#[cfg(not(feature = "std"))]
use alloc::vec::Vec;

/// ADR-018 magic number, little-endian.
pub const MAGIC: u32 = 0xC511_0001;

/// Sync-packet magic (see `firmware/.../csi_collector.c`).
/// We decode and ignore sync packets in the receiver — they're an upstream
/// mesh-time-sync hook we don't use here, but skipping them cleanly avoids
/// spurious "bad frame" warnings.
pub const SYNC_MAGIC: u32 = 0xC511_A110;

/// Fixed header size, in bytes.
pub const HEADER_SIZE: usize = 20;

/// Decoded CSI frame.
#[derive(Debug, Clone, PartialEq)]
pub struct CsiFrame {
    pub node_id: u8,
    pub n_antennas: u8,
    pub n_subcarriers: u16,
    pub freq_mhz: u32,
    pub sequence: u32,
    pub rssi: i8,
    pub noise_floor: i8,
    /// Byte 18 — PPDU type if the firmware was built with HE tagging,
    /// otherwise 0. We pass it through without interpreting it.
    pub ppdu_type: u8,
    /// Byte 19 — flag bits; bit 0 = 40 MHz, bit 2 = STBC, bit 4 = sync valid.
    pub flags: u8,
    /// Raw I/Q payload. Length is `n_antennas * n_subcarriers * 2`.
    /// We don't unpack into complex numbers here — the DSP crate does
    /// that on demand because not every caller needs it.
    pub iq: Vec<u8>,
}

impl CsiFrame {
    /// Returns the expected I/Q length in bytes given the header.
    pub fn expected_iq_len(&self) -> usize {
        usize::from(self.n_antennas)
            .saturating_mul(usize::from(self.n_subcarriers))
            .saturating_mul(2)
    }

    /// Number of complex samples in the payload (`n_antennas * n_subcarriers`).
    pub fn n_samples(&self) -> usize {
        usize::from(self.n_antennas).saturating_mul(usize::from(self.n_subcarriers))
    }
}

/// Decode an ADR-018 binary frame from a byte slice.
///
/// Returns `Ok(None)` for sync packets (magic = SYNC_MAGIC) — caller should
/// treat those as "ignore". Returns `Err` for unrecognized magic or
/// truncated frames.
pub fn decode(buf: &[u8]) -> Result<Option<CsiFrame>, DecodeError> {
    if buf.len() < 4 {
        return Err(DecodeError::Truncated {
            need: 4,
            got: buf.len(),
        });
    }
    let magic = u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]]);

    if magic == SYNC_MAGIC {
        return Ok(None);
    }
    if magic != MAGIC {
        return Err(DecodeError::BadMagic(magic));
    }
    if buf.len() < HEADER_SIZE {
        return Err(DecodeError::Truncated {
            need: HEADER_SIZE,
            got: buf.len(),
        });
    }

    let node_id = buf[4];
    let n_antennas = buf[5];
    let n_subcarriers = u16::from_le_bytes([buf[6], buf[7]]);
    let freq_mhz = u32::from_le_bytes([buf[8], buf[9], buf[10], buf[11]]);
    let sequence = u32::from_le_bytes([buf[12], buf[13], buf[14], buf[15]]);
    let rssi = buf[16] as i8;
    let noise_floor = buf[17] as i8;
    let ppdu_type = buf[18];
    let flags = buf[19];

    let expected_iq = usize::from(n_antennas)
        .saturating_mul(usize::from(n_subcarriers))
        .saturating_mul(2);
    let actual_iq = buf.len() - HEADER_SIZE;

    if actual_iq != expected_iq {
        return Err(DecodeError::IqLenMismatch {
            header: expected_iq,
            payload: actual_iq,
        });
    }

    let iq = buf[HEADER_SIZE..].to_vec();
    Ok(Some(CsiFrame {
        node_id,
        n_antennas,
        n_subcarriers,
        freq_mhz,
        sequence,
        rssi,
        noise_floor,
        ppdu_type,
        flags,
        iq,
    }))
}

/// Encode a frame back to bytes — mostly for round-trip tests and for
/// host-side simulators that want to emit the same wire format.
pub fn encode(frame: &CsiFrame, out: &mut Vec<u8>) {
    out.clear();
    out.reserve(HEADER_SIZE + frame.iq.len());
    out.extend_from_slice(&MAGIC.to_le_bytes());
    out.push(frame.node_id);
    out.push(frame.n_antennas);
    out.extend_from_slice(&frame.n_subcarriers.to_le_bytes());
    out.extend_from_slice(&frame.freq_mhz.to_le_bytes());
    out.extend_from_slice(&frame.sequence.to_le_bytes());
    out.push(frame.rssi as u8);
    out.push(frame.noise_floor as u8);
    out.push(frame.ppdu_type);
    out.push(frame.flags);
    out.extend_from_slice(&frame.iq);
}

/// Iterate over `(i_byte, q_byte)` pairs in the payload.
pub fn iq_pairs(frame: &CsiFrame) -> impl Iterator<Item = (i8, i8)> + '_ {
    frame.iq.chunks_exact(2).map(|c| (c[0] as i8, c[1] as i8))
}

/// Decode errors.
#[derive(Debug, PartialEq, Eq)]
pub enum DecodeError {
    Truncated { need: usize, got: usize },
    BadMagic(u32),
    IqLenMismatch { header: usize, payload: usize },
}

#[cfg(feature = "std")]
impl std::fmt::Display for DecodeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Truncated { need, got } => {
                write!(f, "truncated frame: need {} bytes, got {}", need, got)
            }
            Self::BadMagic(m) => write!(f, "bad magic: 0x{:08X}", m),
            Self::IqLenMismatch { header, payload } => write!(
                f,
                "I/Q length mismatch: header says {} bytes, payload has {}",
                header, payload
            ),
        }
    }
}

#[cfg(feature = "std")]
impl std::error::Error for DecodeError {}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_frame() -> CsiFrame {
        CsiFrame {
            node_id: 3,
            n_antennas: 1,
            n_subcarriers: 4,
            freq_mhz: 2437,
            sequence: 42,
            rssi: -52,
            noise_floor: -95,
            ppdu_type: 0,
            flags: 0,
            iq: vec![1, 2, 3, 4, 5, 6, 7, 8], // 4 subc * 2 bytes
        }
    }

    #[test]
    fn roundtrip() {
        let f = sample_frame();
        let mut buf = Vec::new();
        encode(&f, &mut buf);
        assert_eq!(buf.len(), HEADER_SIZE + f.iq.len());
        let decoded = decode(&buf).unwrap().expect("not a sync packet");
        assert_eq!(decoded, f);
    }

    #[test]
    fn truncated() {
        let buf = [0u8; 3];
        assert!(matches!(
            decode(&buf),
            Err(DecodeError::Truncated { need: 4, got: 3 })
        ));
    }

    #[test]
    fn bad_magic() {
        let buf = [0u8; HEADER_SIZE];
        assert!(matches!(decode(&buf), Err(DecodeError::BadMagic(0))));
    }

    #[test]
    fn sync_packet_returns_none() {
        let mut buf = Vec::from(SYNC_MAGIC.to_le_bytes());
        buf.extend_from_slice(&[0u8; 28]);
        assert_eq!(decode(&buf).unwrap(), None);
    }

    #[test]
    fn iq_len_mismatch() {
        let f = sample_frame();
        let mut buf = Vec::new();
        encode(&f, &mut buf);
        buf.truncate(buf.len() - 2); // chop two bytes off the payload
        assert!(matches!(
            decode(&buf),
            Err(DecodeError::IqLenMismatch { .. })
        ));
    }

    #[test]
    fn iq_pairs_iterates() {
        let f = sample_frame();
        let pairs: Vec<_> = iq_pairs(&f).collect();
        assert_eq!(pairs.len(), 4);
        assert_eq!(pairs[0], (1, 2));
    }
}
