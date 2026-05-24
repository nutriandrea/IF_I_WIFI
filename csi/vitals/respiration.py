from __future__ import annotations

import math
from typing import Optional

from .types import VitalEstimate, VitalStatus


class IirState:
    __slots__ = ("x1", "x2", "y1", "y2")

    def __init__(self) -> None:
        self.x1 = 0.0
        self.x2 = 0.0
        self.y1 = 0.0
        self.y2 = 0.0


def count_zero_crossings(signal: list[float]) -> int:
    crossings = 0
    for i in range(1, len(signal)):
        if signal[i - 1] * signal[i] < 0.0:
            crossings += 1
    return crossings


def compute_confidence(signal: list[float]) -> float:
    n = len(signal)
    if n < 4:
        return 0.0

    mean = sum(signal) / n
    variance = sum((x - mean) * (x - mean) for x in signal) / n

    if variance < 1e-15:
        return 0.0

    peak = max(abs(x) for x in signal)
    noise = math.sqrt(variance)
    snr = peak / noise if noise > 1e-15 else 0.0

    return min(snr / 5.0, 1.0)


class BreathingExtractor:
    def __init__(
        self,
        n_subcarriers: int,
        sample_rate: float,
        window_secs: float,
    ) -> None:
        capacity = int(sample_rate * window_secs)
        self.filtered_history: list[float] = []
        self.sample_rate = sample_rate
        self.window_secs = window_secs
        self.n_subcarriers = n_subcarriers
        self.freq_low = 0.1
        self.freq_high = 0.5
        self.filter_state = IirState()

    @classmethod
    def esp32_default(cls) -> BreathingExtractor:
        return cls(56, 100.0, 30.0)

    def extract(
        self, residuals: list[float], weights: list[float]
    ) -> Optional[VitalEstimate]:
        n = min(len(residuals), self.n_subcarriers)
        if n == 0:
            return None

        uniform_w = 1.0 / n
        weighted_signal = 0.0
        for i in range(n):
            w = weights[i] if i < len(weights) else uniform_w
            weighted_signal += residuals[i] * w

        filtered = self._bandpass_filter(weighted_signal)

        self.filtered_history.append(filtered)
        max_len = int(self.sample_rate * self.window_secs)
        if len(self.filtered_history) > max_len:
            self.filtered_history.pop(0)

        min_samples = int(self.sample_rate * 10.0)
        if len(self.filtered_history) < min_samples:
            return None

        crossings = count_zero_crossings(self.filtered_history)
        duration_s = len(self.filtered_history) / self.sample_rate
        frequency_hz = crossings / (2.0 * duration_s)

        if frequency_hz < self.freq_low or frequency_hz > self.freq_high:
            return None

        bpm = frequency_hz * 60.0
        confidence = compute_confidence(self.filtered_history)

        if confidence >= 0.7:
            status = VitalStatus.Valid
        elif confidence >= 0.4:
            status = VitalStatus.Degraded
        else:
            status = VitalStatus.Unreliable

        return VitalEstimate(value_bpm=bpm, confidence=confidence, status=status)

    def _bandpass_filter(self, input_val: float) -> float:
        state = self.filter_state

        omega_low = 2.0 * math.pi * self.freq_low / self.sample_rate
        omega_high = 2.0 * math.pi * self.freq_high / self.sample_rate
        bw = omega_high - omega_low
        center = (omega_low + omega_high) / 2.0

        r = 1.0 - bw / 2.0
        cos_w0 = math.cos(center)

        output = (
            (1.0 - r) * (input_val - state.x2)
            + 2.0 * r * cos_w0 * state.y1
            - r * r * state.y2
        )

        state.x2 = state.x1
        state.x1 = input_val
        state.y2 = state.y1
        state.y1 = output

        return output

    def reset(self) -> None:
        self.filtered_history.clear()
        self.filter_state = IirState()

    def history_len(self) -> int:
        return len(self.filtered_history)

    def band(self) -> tuple[float, float]:
        return (self.freq_low, self.freq_high)
