from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .types import VitalReading, VitalStatus


@dataclass
class VitalStats:
    count: int = 0
    rr_mean: float = 0.0
    hr_mean: float = 0.0
    rr_min: float = 0.0
    rr_max: float = 0.0
    hr_min: float = 0.0
    hr_max: float = 0.0
    valid_fraction: float = 0.0


class VitalSignStore:
    def __init__(self, max_readings: int = 3600) -> None:
        self.readings: list[VitalReading] = []
        self.max_readings = max(1, max_readings)

    @classmethod
    def default_capacity(cls) -> VitalSignStore:
        return cls(3600)

    def push(self, reading: VitalReading) -> None:
        if len(self.readings) >= self.max_readings:
            self.readings.pop(0)
        self.readings.append(reading)

    def latest(self) -> Optional[VitalReading]:
        if self.readings:
            return self.readings[-1]
        return None

    def history(self, n: int) -> list[VitalReading]:
        start = max(0, len(self.readings) - n)
        return self.readings[start:]

    def stats(self) -> Optional[VitalStats]:
        if not self.readings:
            return None

        n = float(len(self.readings))
        rr_sum = 0.0
        hr_sum = 0.0
        rr_min = float("inf")
        rr_max = float("-inf")
        hr_min = float("inf")
        hr_max = float("-inf")
        valid_count = 0

        for r in self.readings:
            rr = r.respiratory_rate.value_bpm
            hr = r.heart_rate.value_bpm

            rr_sum += rr
            hr_sum += hr
            rr_min = min(rr_min, rr)
            rr_max = max(rr_max, rr)
            hr_min = min(hr_min, hr)
            hr_max = max(hr_max, hr)

            if (
                r.respiratory_rate.status == VitalStatus.Valid
                and r.heart_rate.status == VitalStatus.Valid
            ):
                valid_count += 1

        return VitalStats(
            count=len(self.readings),
            rr_mean=rr_sum / n,
            hr_mean=hr_sum / n,
            rr_min=rr_min,
            rr_max=rr_max,
            hr_min=hr_min,
            hr_max=hr_max,
            valid_fraction=valid_count / n,
        )

    def __len__(self) -> int:
        return len(self.readings)

    def is_empty(self) -> bool:
        return len(self.readings) == 0

    def capacity(self) -> int:
        return self.max_readings

    def clear(self) -> None:
        self.readings.clear()
