from __future__ import annotations

import time
from typing import Optional

from .anomaly import AnomalyAlert, VitalAnomalyDetector
from .heartrate import HeartRateExtractor
from .preprocessor import CsiVitalPreprocessor
from .respiration import BreathingExtractor
from .store import VitalSignStore
from .types import CsiFrame, VitalEstimate, VitalReading


class VitalSignPipeline:
    def __init__(
        self,
        n_subcarriers: int = 56,
        sample_rate: float = 100.0,
    ) -> None:
        self.preprocessor = CsiVitalPreprocessor(n_subcarriers, 0.05)
        self.breathing = BreathingExtractor(n_subcarriers, sample_rate, 30.0)
        self.heartrate = HeartRateExtractor(n_subcarriers, sample_rate, 15.0)
        self.anomaly = VitalAnomalyDetector()
        self.store = VitalSignStore(3600)

    def process_frame(
        self,
        amplitudes: list[float],
        phases: list[float],
        sample_rate: float,
        sample_index: int,
    ) -> tuple[Optional[VitalReading], list[AnomalyAlert]]:
        frame = CsiFrame(
            amplitudes=amplitudes,
            phases=phases,
            n_subcarriers=len(amplitudes),
            sample_index=sample_index,
            sample_rate_hz=sample_rate,
        )

        residuals = self.preprocessor.process(frame)
        if residuals is None:
            return None, []

        n = len(amplitudes)
        weights = [1.0 / n] * n if n > 0 else []

        rr = self.breathing.extract(residuals, weights)
        hr = self.heartrate.extract(residuals, phases)

        reading = VitalReading(
            respiratory_rate=rr if rr is not None else VitalEstimate.unavailable(),
            heart_rate=hr if hr is not None else VitalEstimate.unavailable(),
            subcarrier_count=n,
            signal_quality=0.9,
            timestamp_secs=time.time(),
        )

        alerts = self.anomaly.check(reading)
        self.store.push(reading)

        return reading, alerts

    def reset(self) -> None:
        self.preprocessor.reset()
        self.breathing.reset()
        self.heartrate.reset()
        self.anomaly.reset()
        self.store.clear()
