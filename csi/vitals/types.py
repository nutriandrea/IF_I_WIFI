from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


class VitalStatus(Enum):
    Valid = auto()
    Degraded = auto()
    Unreliable = auto()
    Unavailable = auto()


@dataclass
class VitalEstimate:
    value_bpm: float = 0.0
    confidence: float = 0.0
    status: VitalStatus = VitalStatus.Unavailable

    @classmethod
    def unavailable(cls) -> VitalEstimate:
        return cls(value_bpm=0.0, confidence=0.0, status=VitalStatus.Unavailable)


@dataclass
class VitalReading:
    respiratory_rate: VitalEstimate = field(default_factory=VitalEstimate.unavailable)
    heart_rate: VitalEstimate = field(default_factory=VitalEstimate.unavailable)
    subcarrier_count: int = 0
    signal_quality: float = 0.0
    timestamp_secs: float = 0.0


@dataclass
class CsiFrame:
    amplitudes: list[float]
    phases: list[float]
    n_subcarriers: int
    sample_index: int
    sample_rate_hz: float

    @classmethod
    def new(
        cls,
        amplitudes: list[float],
        phases: list[float],
        n_subcarriers: int,
        sample_index: int,
        sample_rate_hz: float,
    ) -> Optional[CsiFrame]:
        if len(amplitudes) != n_subcarriers or len(phases) != n_subcarriers:
            return None
        return cls(
            amplitudes=amplitudes,
            phases=phases,
            n_subcarriers=n_subcarriers,
            sample_index=sample_index,
            sample_rate_hz=sample_rate_hz,
        )
