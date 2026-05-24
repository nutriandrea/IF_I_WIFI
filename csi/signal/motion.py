from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import numpy as np


class MotionScore:
    def __init__(self, variance: float, correlation: float, phase: float,
                 doppler: Optional[float] = None):
        self.variance_component = variance
        self.correlation_component = correlation
        self.phase_component = phase
        self.doppler_component = doppler

        if doppler is not None:
            self.total = 0.3 * variance + 0.2 * correlation + 0.2 * phase + 0.3 * doppler
        else:
            self.total = 0.4 * variance + 0.3 * correlation + 0.3 * phase
        self.total = max(0.0, min(1.0, self.total))

    def is_motion_detected(self, threshold: float = 0.3) -> bool:
        return self.total >= threshold


@dataclass
class MotionAnalysis:
    score: MotionScore
    temporal_variance: float
    spatial_variance: float
    estimated_velocity: float
    motion_direction: Optional[float] = None
    confidence: float = 0.0


@dataclass
class MotionDetectorConfig:
    human_detection_threshold: float = 0.8
    motion_threshold: float = 0.3
    smoothing_factor: float = 0.9
    history_size: int = 100
    amplitude_threshold: float = 0.1
    phase_threshold: float = 0.05
    amplitude_weight: float = 0.4
    phase_weight: float = 0.3
    motion_weight: float = 0.3


@dataclass
class HumanDetectionResult:
    human_detected: bool
    confidence: float
    motion_score: float
    threshold: float
    timestamp: datetime
    raw_confidence: float = 0.0
    motion_analysis: Optional[MotionAnalysis] = None


@dataclass
class HumanDetectionConfig:
    """Alias for MotionDetectorConfig for convenient import."""
    human_detection_threshold: float = 0.8
    motion_threshold: float = 0.3
    smoothing_factor: float = 0.9
    history_size: int = 100


class MotionDetector:
    def __init__(self, config: Optional[MotionDetectorConfig] = None):
        self.config = config or MotionDetectorConfig()
        self.history: list[MotionScore] = []
        self.smoothed_score: float = 0.0
        self.previous_confidence: float = 0.0

    def _extract_components(self, features) -> tuple:
        amp_var = float(np.mean(features.amplitude.variance)) if hasattr(features, 'amplitude') else 0.0

        if hasattr(features, 'correlation'):
            corr_val = 1.0 - abs(getattr(features.correlation, 'mean_correlation', 0.0))
        else:
            corr_val = 0.0

        if hasattr(features, 'phase'):
            phase_var = float(np.mean(features.phase.variance)) if hasattr(features.phase, 'variance') and features.phase.variance.size > 0 else 0.0
        else:
            phase_var = 0.0

        doppler_val = None
        if hasattr(features, 'doppler') and features.doppler is not None:
            doppler_val = min(1.0, getattr(features.doppler, 'max_doppler_freq', 0.0) / 100.0)

        variance_score = min(1.0, amp_var / 0.5)
        correlation_score = corr_val
        phase_score = min(1.0, phase_var / 0.5)

        return variance_score, correlation_score, phase_score, doppler_val

    def update(self, features) -> MotionAnalysis:
        variance_score, correlation_score, phase_score, doppler_val = self._extract_components(features)

        score = MotionScore(variance_score, correlation_score, phase_score, doppler_val)

        self.smoothed_score = (
            self.config.smoothing_factor * self.smoothed_score
            + (1.0 - self.config.smoothing_factor) * score.total
        )

        self.history.append(score)
        if len(self.history) > self.config.history_size:
            self.history.pop(0)

        temporal_var = 0.0
        if len(self.history) >= 2:
            scores_arr = np.array([m.total for m in self.history])
            temporal_var = float(np.var(scores_arr))

        spatial_variance = 0.0
        if hasattr(features, 'amplitude') and hasattr(features.amplitude, 'variance') and features.amplitude.variance.size > 0:
            spatial_variance = float(np.mean(features.amplitude.variance))

        direction = None
        if hasattr(features, 'phase') and hasattr(features.phase, 'gradient') and features.phase.gradient.size > 0:
            direction = float(np.arctan(np.mean(features.phase.gradient)))

        vel = 0.0
        if doppler_val is not None:
            vel = doppler_val * 100.0

        score.total = self.smoothed_score
        confidence = self.smoothed_score

        return MotionAnalysis(
            score=score,
            temporal_variance=temporal_var,
            spatial_variance=spatial_variance,
            estimated_velocity=vel,
            motion_direction=direction,
            confidence=confidence,
        )

    def detect_human(self, features) -> HumanDetectionResult:
        analysis = self.update(features)
        raw_conf = analysis.confidence

        smoothed = (
            self.config.smoothing_factor * self.previous_confidence
            + (1.0 - self.config.smoothing_factor) * raw_conf
        )
        self.previous_confidence = smoothed

        threshold = self.config.human_detection_threshold
        human_detected = smoothed >= threshold

        return HumanDetectionResult(
            human_detected=human_detected,
            confidence=smoothed,
            motion_score=analysis.score.total,
            threshold=threshold,
            timestamp=datetime.now(),
            raw_confidence=raw_conf,
            motion_analysis=analysis,
        )
