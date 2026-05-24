"""
filter.py — Biquad IIR filter cascade for real-time signal processing.

Provides a standalone BiquadFilter class implementing 2nd-order IIR
biquad sections (Direct Form I) with design methods for Butterworth
lowpass, highpass, and bandpass filters.  Multiple sections can be
cascaded for higher-order filtering.

Ported from RuView edge_processing.c biquad_bandpass_design /
biquad_process (ADR-039 Edge Intelligence).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class BiquadCoeffs:
    """Second-order IIR biquad coefficients (Direct Form I)."""
    b0: float = 0.0
    b1: float = 0.0
    b2: float = 0.0
    a1: float = 0.0
    a2: float = 0.0


class BiquadState:
    """Filter state (delay line) for one biquad section."""
    __slots__ = ("x1", "x2", "y1", "y2")

    def __init__(self) -> None:
        self.x1 = 0.0
        self.x2 = 0.0
        self.y1 = 0.0
        self.y2 = 0.0

    def reset(self) -> None:
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0


class BiquadSection:
    """
    One second-order biquad section (Direct Form I).

    Can be used standalone or cascaded via :class:`BiquadFilter`.
    """

    def __init__(self, coeffs: BiquadCoeffs) -> None:
        self.coeffs = coeffs
        self.state = BiquadState()

    def process(self, x: float) -> float:
        """Process one sample through this biquad section."""
        b = self.coeffs
        s = self.state

        y = (b.b0 * x + b.b1 * s.x1 + b.b2 * s.x2
             - b.a1 * s.y1 - b.a2 * s.y2)

        s.x2 = s.x1
        s.x1 = x
        s.y2 = s.y1
        s.y1 = y

        return y

    def reset(self) -> None:
        self.state.reset()


class BiquadFilter:
    """
    Multi-section biquad IIR filter.

    Designed for real-time per-sample processing (streaming).
    Supports Butterworth lowpass, highpass, and bandpass designs.

    Parameters
    ----------
    sample_rate : float
        Sampling frequency in Hz.
    cutoff : float
        Cutoff frequency in Hz (for lowpass/highpass).
    cutoff_low : float
        Low cutoff for bandpass (Hz).  Used together with *cutoff_high*.
    cutoff_high : float
        High cutoff for bandpass (Hz).
    filter_type : str
        ``"lowpass"``, ``"highpass"``, or ``"bandpass"``.
    order : int
        Filter order (number of biquad sections = ``order // 2``).
        Must be even.  Default 2 (one section).

    Examples
    --------
    >>> bf = BiquadFilter(sample_rate=100.0, cutoff_low=0.1, cutoff_high=0.5)
    >>> for sample in raw_signal:
    ...     filtered = bf.process(sample)
    """

    def __init__(
        self,
        sample_rate: float,
        cutoff: float = 0.0,
        cutoff_low: float = 0.0,
        cutoff_high: float = 0.0,
        filter_type: str = "bandpass",
        order: int = 2,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be > 0")
        if order < 2 or order % 2 != 0:
            raise ValueError("order must be even and >= 2")

        self.sample_rate = sample_rate
        self.order = order
        self.n_sections = order // 2

        if filter_type == "bandpass":
            if cutoff_low <= 0 or cutoff_high <= 0:
                raise ValueError("bandpass requires cutoff_low and cutoff_high > 0")
        elif filter_type in ("lowpass", "highpass"):
            if cutoff <= 0:
                raise ValueError(f"{filter_type} requires cutoff > 0")

        self.filter_type = filter_type
        self.sections: List[BiquadSection] = []
        if filter_type == "bandpass":
            self._design_bandpass(cutoff_low, cutoff_high)
        elif filter_type == "lowpass":
            self._design_lowpass(cutoff)
        elif filter_type == "highpass":
            self._design_highpass(cutoff)
        else:
            raise ValueError(f"Unknown filter_type: {filter_type}")

    # ── Design methods ─────────────────────────────────────────────

    def _design_bandpass(self, f_lo: float, f_hi: float) -> None:
        """Butterworth bandpass biquad design per RuView edge_processing.c."""
        fs = self.sample_rate
        for _ in range(self.n_sections):
            w0 = 2.0 * math.pi * (f_lo + f_hi) / 2.0 / fs
            bw = 2.0 * math.pi * (f_hi - f_lo) / fs
            alpha = math.sin(w0) * math.sinh(math.log(2.0) / 2.0 * bw / math.sin(w0))

            a0_inv = 1.0 / (1.0 + alpha)
            coeffs = BiquadCoeffs(
                b0=alpha * a0_inv,
                b1=0.0,
                b2=-alpha * a0_inv,
                a1=-2.0 * math.cos(w0) * a0_inv,
                a2=(1.0 - alpha) * a0_inv,
            )
            self.sections.append(BiquadSection(coeffs))

    def _design_lowpass(self, f_c: float) -> None:
        """Butterworth lowpass biquad design."""
        fs = self.sample_rate
        for k in range(1, self.n_sections + 1):
            theta = math.pi * (2.0 * k - 1.0) / (2.0 * self.order)
            w0 = 2.0 * math.pi * f_c / fs
            alpha = math.sin(w0) * math.sinh(math.log(2.0) / 2.0
                                             * (w0 / math.sin(w0)))

            a0_inv = 1.0 / (1.0 + alpha)
            cos_w0 = math.cos(w0)
            coeffs = BiquadCoeffs(
                b0=(1.0 - cos_w0) / 2.0 * a0_inv,
                b1=(1.0 - cos_w0) * a0_inv,
                b2=(1.0 - cos_w0) / 2.0 * a0_inv,
                a1=-2.0 * cos_w0 * a0_inv,
                a2=(1.0 - alpha) * a0_inv,
            )
            self.sections.append(BiquadSection(coeffs))

    def _design_highpass(self, f_c: float) -> None:
        """Butterworth highpass biquad design."""
        fs = self.sample_rate
        for _ in range(self.n_sections):
            w0 = 2.0 * math.pi * f_c / fs
            alpha = math.sin(w0) * math.sinh(math.log(2.0) / 2.0
                                             * (math.pi / math.sin(w0)))

            a0_inv = 1.0 / (1.0 + alpha)
            cos_w0 = math.cos(w0)
            coeffs = BiquadCoeffs(
                b0=(1.0 + cos_w0) / 2.0 * a0_inv,
                b1=-(1.0 + cos_w0) * a0_inv,
                b2=(1.0 + cos_w0) / 2.0 * a0_inv,
                a1=-2.0 * cos_w0 * a0_inv,
                a2=(1.0 - alpha) * a0_inv,
            )
            self.sections.append(BiquadSection(coeffs))

    # ── Processing ─────────────────────────────────────────────────

    def process(self, x: float) -> float:
        """Process one sample through the cascaded biquad sections."""
        for section in self.sections:
            x = section.process(x)
        return x

    def filter(self, samples: List[float]) -> List[float]:
        """Apply filter to a complete signal buffer.

        For real-time streaming, prefer calling ``process(x)``
        per-sample to maintain state continuity.
        """
        return [self.process(x) for x in samples]

    def reset(self) -> None:
        """Reset all section states to zero."""
        for section in self.sections:
            section.reset()

    @property
    def is_settled(self) -> bool:
        """Check if all section states are zero (filter is idle)."""
        return all(
            s.state.x1 == 0.0 and s.state.x2 == 0.0
            and s.state.y1 == 0.0 and s.state.y2 == 0.0
            for s in self.sections
        )


__all__ = [
    "BiquadCoeffs",
    "BiquadState",
    "BiquadSection",
    "BiquadFilter",
]
