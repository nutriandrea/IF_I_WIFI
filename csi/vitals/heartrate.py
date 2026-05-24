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


def compute_phase_coherence_signal(
    residuals: list[float], phases: list[float], n: int
) -> float:
    if n <= 1:
        return residuals[0] if residuals else 0.0

    weighted_sum = 0.0
    weight_total = 0.0

    for i in range(n):
        if i + 1 < n:
            phase_diff = abs(phases[i + 1] - phases[i])
            coherence = math.exp(-phase_diff)
        elif i > 0:
            phase_diff = abs(phases[i] - phases[i - 1])
            coherence = math.exp(-phase_diff)
        else:
            coherence = 1.0

        weighted_sum += residuals[i] * coherence
        weight_total += coherence

    if weight_total > 1e-15:
        return weighted_sum / weight_total
    return 0.0


def autocorrelation_peak(
    signal: list[float], sample_rate: float, freq_low: float, freq_high: float
) -> tuple[int, float]:
    n = len(signal)
    if n < 4:
        return (0, 0.0)

    min_lag = int(math.floor(sample_rate / freq_high))
    max_lag = int(math.ceil(sample_rate / freq_low))
    max_lag = min(max_lag, n // 2)

    if min_lag >= max_lag or min_lag >= n:
        return (0, 0.0)

    mean = sum(signal) / n

    acf0 = sum((x - mean) * (x - mean) for x in signal)
    if acf0 < 1e-15:
        return (0, 0.0)

    best_lag = 0
    best_acf = -float("inf")

    for lag in range(min_lag, max_lag + 1):
        acf = 0.0
        for i in range(n - lag):
            acf += (signal[i] - mean) * (signal[i + lag] - mean)

        normalized = acf / acf0
        if normalized > best_acf:
            best_acf = normalized
            best_lag = lag

    if best_acf > 0.0:
        return (best_lag, best_acf)
    return (0, 0.0)


class HeartRateExtractor:
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
        self.freq_low = 0.8
        self.freq_high = 2.0
        self.filter_state = IirState()
        self.min_subcarriers = 4

    @classmethod
    def esp32_default(cls) -> HeartRateExtractor:
        return cls(56, 100.0, 15.0)

    def extract(
        self, residuals: list[float], phases: list[float]
    ) -> Optional[VitalEstimate]:
        n = min(len(residuals), self.n_subcarriers, len(phases))
        if n == 0:
            return None

        phase_signal = compute_phase_coherence_signal(residuals, phases, n)

        filtered = self._bandpass_filter(phase_signal)

        self.filtered_history.append(filtered)
        max_len = int(self.sample_rate * self.window_secs)
        if len(self.filtered_history) > max_len:
            self.filtered_history.pop(0)

        min_samples = int(self.sample_rate * 5.0)
        if len(self.filtered_history) < min_samples:
            return None

        period_samples, acf_peak = autocorrelation_peak(
            self.filtered_history,
            self.sample_rate,
            self.freq_low,
            self.freq_high,
        )

        if period_samples == 0:
            return None

        frequency_hz = self.sample_rate / period_samples
        bpm = frequency_hz * 60.0

        if not (40.0 <= bpm <= 180.0):
            return None

        subcarrier_factor = 1.0 if n >= self.min_subcarriers else n / self.min_subcarriers
        confidence = max(0.0, min(1.0, acf_peak * subcarrier_factor))

        if confidence >= 0.6 and n >= self.min_subcarriers:
            status = VitalStatus.Valid
        elif confidence >= 0.3:
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
