//! Streaming DSP primitives for IF I WIFI.
//!
//! Two things live here today:
//!
//! - [`StreamingHampel`] — an online Hampel outlier detector that holds a
//!   ring buffer of the last `2*half_window + 1` samples and reports each
//!   incoming sample as either kept-as-is or clamped to the local median.
//!   This is a port of the batch implementation in
//!   `RuView/v2/crates/wifi-densepose-signal/src/hampel.rs`, reworked so
//!   the host doesn't need to buffer the whole signal first.
//!
//! - [`CoherenceGate`] — a simple "is the signal trustworthy right now"
//!   gate based on a z-score of the running variance vs a baseline. Lifted
//!   from the same upstream crate (`ruvsense/coherence_gate.rs`).
//!
//! No `f64`. All math is `f32` — adequate for CSI dynamic range and
//! cheaper if you ever cross-compile this to a Cortex-M.

use ifi_core::CsiFrame;

/// Multiplier that converts MAD (median absolute deviation) into the
/// equivalent Gaussian σ.
const MAD_TO_SIGMA: f32 = 1.4826;

/// Configuration for [`StreamingHampel`].
#[derive(Debug, Clone, Copy)]
pub struct HampelConfig {
    /// Half-window. Total window = `2 * half_window + 1`. Default 3 (window=7).
    pub half_window: usize,
    /// Outlier threshold expressed in units of estimated σ. Default 3.0.
    pub threshold: f32,
}

impl Default for HampelConfig {
    fn default() -> Self {
        Self {
            half_window: 3,
            threshold: 3.0,
        }
    }
}

/// One step of streaming Hampel filtering.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct HampelStep {
    /// Sample after filtering (== input if not an outlier).
    pub value: f32,
    /// Was the input flagged as an outlier?
    pub is_outlier: bool,
    /// Local median used as the replacement.
    pub median: f32,
    /// Estimated local σ.
    pub sigma: f32,
}

/// Streaming Hampel filter.
///
/// Push one sample, get one decision. The internal ring buffer holds at
/// most `2*half_window + 1` samples; before it fills, decisions use whatever
/// is available (you'll get less robust σ estimates for the first few samples).
pub struct StreamingHampel {
    cfg: HampelConfig,
    buf: Vec<f32>,
    head: usize, // index of the next slot to write
    filled: usize,
}

impl StreamingHampel {
    pub fn new(cfg: HampelConfig) -> Self {
        let cap = 2 * cfg.half_window + 1;
        Self {
            cfg,
            buf: vec![0.0; cap],
            head: 0,
            filled: 0,
        }
    }

    pub fn push(&mut self, x: f32) -> HampelStep {
        // Write into the ring.
        self.buf[self.head] = x;
        self.head = (self.head + 1) % self.buf.len();
        if self.filled < self.buf.len() {
            self.filled += 1;
        }

        // Copy active window into a scratch slice so we can sort.
        let mut window: Vec<f32> = self.buf[..self.filled].to_vec();
        let median = median(&mut window);
        let mut dev: Vec<f32> = window.iter().map(|v| (v - median).abs()).collect();
        let mad = median(&mut dev);
        let sigma = MAD_TO_SIGMA * mad;

        let deviation = (x - median).abs();
        let is_outlier = if sigma > 1e-9 {
            deviation > self.cfg.threshold * sigma
        } else {
            // Degenerate case: window is flat. Any non-zero deviation is an outlier.
            deviation > 1e-9
        };

        HampelStep {
            value: if is_outlier { median } else { x },
            is_outlier,
            median,
            sigma,
        }
    }

    /// Convenience: filter an `(i, q)` byte pair into a single amplitude
    /// `sqrt(i^2 + q^2)`, push that into the filter, return the filtered amplitude.
    pub fn push_iq(&mut self, i: i8, q: i8) -> HampelStep {
        let amp = ((i as f32).powi(2) + (q as f32).powi(2)).sqrt();
        self.push(amp)
    }
}

/// Sort in place and return the median. Uses partial_cmp so NaNs don't panic.
fn median(v: &mut [f32]) -> f32 {
    if v.is_empty() {
        return 0.0;
    }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(core::cmp::Ordering::Equal));
    let n = v.len();
    if n % 2 == 0 {
        0.5 * (v[n / 2 - 1] + v[n / 2])
    } else {
        v[n / 2]
    }
}

// ---------------------------------------------------------------------------
// Coherence gate
// ---------------------------------------------------------------------------

/// Coherence gate state. Tracks a running mean+variance of an "energy"
/// signal (typically mean amplitude across subcarriers) and reports whether
/// the current frame is coherent with the baseline.
///
/// Output states map directly to the upstream `CoherenceDecision`:
/// `Accept`, `PredictOnly`, `Reject`, `Recalibrate`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CoherenceDecision {
    Accept,
    PredictOnly,
    Reject,
    Recalibrate,
}

#[derive(Debug, Clone, Copy)]
pub struct CoherenceConfig {
    /// Exponential smoothing factor for the running mean (0,1]. 0.05 ≈ 20 frames.
    pub alpha_mean: f32,
    /// Exponential smoothing factor for the running variance.
    pub alpha_var: f32,
    /// z-score above which we go `PredictOnly`.
    pub warn_z: f32,
    /// z-score above which we `Reject`.
    pub reject_z: f32,
    /// Frames before the baseline is considered calibrated.
    pub warmup_frames: usize,
}

impl Default for CoherenceConfig {
    fn default() -> Self {
        Self {
            alpha_mean: 0.05,
            alpha_var: 0.02,
            warn_z: 2.5,
            reject_z: 4.0,
            warmup_frames: 30,
        }
    }
}

pub struct CoherenceGate {
    cfg: CoherenceConfig,
    mean: f32,
    var: f32,
    seen: usize,
}

impl CoherenceGate {
    pub fn new(cfg: CoherenceConfig) -> Self {
        Self {
            cfg,
            mean: 0.0,
            var: 1.0, // start non-zero so the first frames don't divide by 0
            seen: 0,
        }
    }

    /// Push one scalar "energy" sample and get the gate decision.
    pub fn step(&mut self, energy: f32) -> CoherenceDecision {
        let delta = energy - self.mean;
        // EWMA on mean and on squared deviation.
        self.mean += self.cfg.alpha_mean * delta;
        self.var = (1.0 - self.cfg.alpha_var) * self.var
            + self.cfg.alpha_var * delta * delta;
        self.seen += 1;

        if self.seen < self.cfg.warmup_frames {
            return CoherenceDecision::Recalibrate;
        }

        let sigma = self.var.sqrt().max(1e-6);
        let z = (energy - self.mean).abs() / sigma;
        if z > self.cfg.reject_z {
            CoherenceDecision::Reject
        } else if z > self.cfg.warn_z {
            CoherenceDecision::PredictOnly
        } else {
            CoherenceDecision::Accept
        }
    }
}

/// Compute the mean amplitude of a CSI frame's payload — handy as the
/// "energy" input to [`CoherenceGate`].
pub fn frame_mean_amplitude(frame: &CsiFrame) -> f32 {
    let n = frame.n_samples();
    if n == 0 {
        return 0.0;
    }
    let mut sum = 0.0f32;
    for c in frame.iq.chunks_exact(2) {
        let i = c[0] as i8 as f32;
        let q = c[1] as i8 as f32;
        sum += (i * i + q * q).sqrt();
    }
    sum / (n as f32)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hampel_passes_clean_signal() {
        let mut h = StreamingHampel::new(HampelConfig::default());
        let mut outliers = 0;
        for k in 0..200 {
            let x = ((k as f32) * 0.1).sin();
            let step = h.push(x);
            if step.is_outlier {
                outliers += 1;
            }
        }
        // First few samples can be flagged before the window fills — that's expected.
        assert!(outliers < 10, "too many false outliers: {}", outliers);
    }

    #[test]
    fn hampel_catches_spike() {
        let mut h = StreamingHampel::new(HampelConfig::default());
        // Seed with a flat baseline.
        for _ in 0..20 {
            h.push(1.0);
        }
        // Insert a 100x spike.
        let step = h.push(100.0);
        assert!(step.is_outlier);
        assert!((step.value - 1.0).abs() < 1e-3, "expected clamp to median");
    }

    #[test]
    fn coherence_recalibrates_then_accepts_flat() {
        let mut g = CoherenceGate::new(CoherenceConfig::default());
        for _ in 0..29 {
            assert_eq!(g.step(1.0), CoherenceDecision::Recalibrate);
        }
        let d = g.step(1.0);
        assert!(matches!(d, CoherenceDecision::Accept), "got {:?}", d);
    }

    #[test]
    fn coherence_rejects_huge_outlier() {
        let mut g = CoherenceGate::new(CoherenceConfig::default());
        for _ in 0..60 {
            g.step(1.0);
        }
        let d = g.step(50.0);
        assert!(
            matches!(d, CoherenceDecision::Reject | CoherenceDecision::PredictOnly),
            "got {:?}",
            d
        );
    }

    #[test]
    fn mean_amplitude_zero_for_empty() {
        let f = CsiFrame {
            node_id: 0,
            n_antennas: 0,
            n_subcarriers: 0,
            freq_mhz: 0,
            sequence: 0,
            rssi: 0,
            noise_floor: 0,
            ppdu_type: 0,
            flags: 0,
            iq: vec![],
        };
        assert_eq!(frame_mean_amplitude(&f), 0.0);
    }
}
