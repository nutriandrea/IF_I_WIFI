"""
stats.py — Streaming (online) statistics for real-time signal processing.

Welford's online algorithm for mean and variance in O(1) memory,
suitable for embedded and streaming contexts.  No numpy dependency.
"""

from __future__ import annotations

import math
from typing import Optional


class WelfordOnline:
    """
    Welford's online algorithm for streaming mean and variance.

    Single-pass, O(1) memory, numerically stable.
    Ported from RuView edge_processing.c Welford variance tracking.

    Parameters
    ----------
    ddof : int
        Delta degrees of freedom.  Use 0 for population variance,
        1 for sample variance (default).

    Example
    -------
    >>> w = WelfordOnline()
    >>> for x in stream:
    ...     w.update(x)
    ...     if w.count >= 2:
    ...         print(f"mean={w.mean():.2f}, std={w.std():.2f}")
    """

    def __init__(self, ddof: int = 1) -> None:
        self.ddof = ddof
        self.reset()

    def reset(self) -> None:
        self._count: int = 0
        self._mean: float = 0.0
        self._m2: float = 0.0

    def update(self, value: float) -> None:
        self._count += 1
        delta = value - self._mean
        self._mean += delta / self._count
        delta2 = value - self._mean
        self._m2 += delta * delta2

    def update_batch(self, values: list[float]) -> None:
        for v in values:
            self.update(v)

    @property
    def count(self) -> int:
        return self._count

    def mean(self) -> float:
        return self._mean

    def variance(self) -> float:
        if self._count <= self.ddof:
            return 0.0
        return self._m2 / (self._count - self.ddof)

    def std(self) -> float:
        return math.sqrt(self.variance())

    def z_score(self, value: float) -> float:
        s = self.std()
        if s < 1e-15:
            return 0.0
        return (value - self._mean) / s

    def merge(self, other: WelfordOnline) -> None:
        if other._count == 0:
            return
        if self._count == 0:
            self._count = other._count
            self._mean = other._mean
            self._m2 = other._m2
            return

        n1, n2 = self._count, other._count
        m1, m2 = self._mean, other._mean
        s1, s2 = self._m2, other._m2
        n = n1 + n2

        delta = m2 - m1
        self._count = n
        self._mean = (n1 * m1 + n2 * m2) / n
        self._m2 = s1 + s2 + delta * delta * n1 * n2 / n

    def to_dict(self) -> dict:
        return {
            "count": self._count,
            "mean": self._mean,
            "variance": self.variance(),
            "std": self.std(),
        }


class RunningMinMax:
    """Tracks rolling min, max, peak-to-peak range on a stream."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.min_val = float("inf")
        self.max_val = float("-inf")
        self._initialized = False

    def update(self, value: float) -> None:
        if not self._initialized:
            self.min_val = value
            self.max_val = value
            self._initialized = True
        else:
            if value < self.min_val:
                self.min_val = value
            if value > self.max_val:
                self.max_val = value

    @property
    def range(self) -> float:
        if not self._initialized:
            return 0.0
        return self.max_val - self.min_val

    @property
    def has_data(self) -> bool:
        return self._initialized


__all__ = [
    "WelfordOnline",
    "RunningMinMax",
]
