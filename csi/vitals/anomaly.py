from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .types import VitalReading


class WelfordStats:
    def __init__(self) -> None:
        self.count: int = 0
        self.mean: float = 0.0
        self.m2: float = 0.0

    def update(self, value: float) -> None:
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        delta2 = value - self.mean
        self.m2 += delta * delta2

    def variance(self) -> float:
        if self.count < 2:
            return 0.0
        return self.m2 / (self.count - 1)

    def std_dev(self) -> float:
        return math.sqrt(self.variance())

    def z_score(self, value: float) -> float:
        sd = self.std_dev()
        if sd < 1e-10:
            return 0.0
        return (value - self.mean) / sd


@dataclass
class AnomalyAlert:
    vital_type: str = ""
    alert_type: str = ""
    severity: float = 0.0
    message: str = ""


class VitalAnomalyDetector:
    def __init__(self, window: int = 60, z_threshold: float = 2.5) -> None:
        self.rr_stats = WelfordStats()
        self.hr_stats = WelfordStats()
        self.rr_history: list[float] = []
        self.hr_history: list[float] = []
        self.window = window
        self.z_threshold = z_threshold

    @classmethod
    def default_config(cls) -> VitalAnomalyDetector:
        return cls(60, 2.5)

    def check(self, reading: VitalReading) -> list[AnomalyAlert]:
        alerts: list[AnomalyAlert] = []

        rr = reading.respiratory_rate.value_bpm
        hr = reading.heart_rate.value_bpm

        self.rr_history.append(rr)
        if len(self.rr_history) > self.window:
            self.rr_history.pop(0)

        self.hr_history.append(hr)
        if len(self.hr_history) > self.window:
            self.hr_history.pop(0)

        self.rr_stats.update(rr)
        self.hr_stats.update(hr)

        if self.rr_stats.count < 5:
            return alerts

        rr_z = self.rr_stats.z_score(rr)

        if rr < 4.0 and reading.respiratory_rate.confidence > 0.3:
            alerts.append(
                AnomalyAlert(
                    vital_type="respiratory",
                    alert_type="apnea",
                    severity=0.9,
                    message=f"Possible apnea detected: RR = {rr:.1f} BPM",
                )
            )
        elif rr > 30.0 and reading.respiratory_rate.confidence > 0.3:
            severity = max(0.3, min(1.0, (rr - 30.0) / 20.0))
            alerts.append(
                AnomalyAlert(
                    vital_type="respiratory",
                    alert_type="tachypnea",
                    severity=severity,
                    message=f"Elevated respiratory rate: RR = {rr:.1f} BPM",
                )
            )
        elif rr < 8.0 and reading.respiratory_rate.confidence > 0.3:
            severity = max(0.3, min(0.8, (8.0 - rr) / 8.0))
            alerts.append(
                AnomalyAlert(
                    vital_type="respiratory",
                    alert_type="bradypnea",
                    severity=severity,
                    message=f"Low respiratory rate: RR = {rr:.1f} BPM",
                )
            )

        if abs(rr_z) > self.z_threshold:
            severity = max(0.2, min(1.0, abs(rr_z) / (self.z_threshold * 2.0)))
            alerts.append(
                AnomalyAlert(
                    vital_type="respiratory",
                    alert_type="sudden_change",
                    severity=severity,
                    message=f"Sudden respiratory rate change: z-score = {rr_z:.2f} (RR = {rr:.1f} BPM)",
                )
            )

        hr_z = self.hr_stats.z_score(hr)

        if hr > 100.0 and reading.heart_rate.confidence > 0.3:
            severity = max(0.3, min(1.0, (hr - 100.0) / 80.0))
            alerts.append(
                AnomalyAlert(
                    vital_type="cardiac",
                    alert_type="tachycardia",
                    severity=severity,
                    message=f"Elevated heart rate: HR = {hr:.1f} BPM",
                )
            )
        elif hr < 50.0 and reading.heart_rate.confidence > 0.3:
            severity = max(0.3, min(1.0, (50.0 - hr) / 30.0))
            alerts.append(
                AnomalyAlert(
                    vital_type="cardiac",
                    alert_type="bradycardia",
                    severity=severity,
                    message=f"Low heart rate: HR = {hr:.1f} BPM",
                )
            )

        if abs(hr_z) > self.z_threshold:
            severity = max(0.2, min(1.0, abs(hr_z) / (self.z_threshold * 2.0)))
            alerts.append(
                AnomalyAlert(
                    vital_type="cardiac",
                    alert_type="sudden_change",
                    severity=severity,
                    message=f"Sudden heart rate change: z-score = {hr_z:.2f} (HR = {hr:.1f} BPM)",
                )
            )

        return alerts

    def reset(self) -> None:
        self.rr_stats = WelfordStats()
        self.hr_stats = WelfordStats()
        self.rr_history.clear()
        self.hr_history.clear()

    def reading_count(self) -> int:
        return self.rr_stats.count

    def rr_mean(self) -> float:
        return self.rr_stats.mean

    def hr_mean(self) -> float:
        return self.hr_stats.mean
