from __future__ import annotations

from typing import Optional

from .types import CsiFrame


class CsiVitalPreprocessor:
    def __init__(self, n_subcarriers: int = 56, alpha: float = 0.05) -> None:
        self.predictions = [0.0] * n_subcarriers
        self.initialized = [False] * n_subcarriers
        self._alpha = max(0.001, min(0.999, alpha))
        self._n_subcarriers = n_subcarriers

    @classmethod
    def esp32_default(cls) -> CsiVitalPreprocessor:
        return cls(56, 0.05)

    def process(self, frame: CsiFrame) -> Optional[list[float]]:
        n = min(len(frame.amplitudes), self._n_subcarriers)
        if n == 0:
            return None

        residuals = [0.0] * n

        for i in range(n):
            if self.initialized[i]:
                residuals[i] = frame.amplitudes[i] - self.predictions[i]
                self.predictions[i] = (
                    self._alpha * frame.amplitudes[i]
                    + (1.0 - self._alpha) * self.predictions[i]
                )
            else:
                self.predictions[i] = frame.amplitudes[i]
                self.initialized[i] = True
                residuals[i] = frame.amplitudes[i]

        return residuals

    def reset(self) -> None:
        for i in range(self._n_subcarriers):
            self.predictions[i] = 0.0
            self.initialized[i] = False

    def alpha(self) -> float:
        return self._alpha

    def set_alpha(self, alpha: float) -> None:
        self._alpha = max(0.001, min(0.999, alpha))

    def n_subcarriers(self) -> int:
        return self._n_subcarriers
