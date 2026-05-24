from dataclasses import dataclass
import numpy as np


@dataclass
class HampelResult:
    filtered: np.ndarray
    outlier_indices: list[int]
    medians: np.ndarray
    sigma_estimates: np.ndarray


def hampel_filter(signal: np.ndarray, window_size: int = 5, n_sigmas: float = 3.0) -> HampelResult:
    """
    Apply Hampel filter to a 1D signal.
    Replaces outliers with median of local window.
    """
    if len(signal) == 0:
        raise ValueError("Signal must not be empty")
    if window_size < 3:
        raise ValueError("window_size must be >= 3")

    n = len(signal)
    result = signal.copy()
    outlier_indices: list[int] = []
    medians = np.zeros(n)
    sigma_estimates = np.zeros(n)
    half = window_size // 2

    for i in range(n):
        start = max(0, i - half)
        end = min(n, i + half + 1)
        window = signal[start:end]

        med = float(np.median(window))
        mad = float(np.median(np.abs(window - med)))
        sigma = 1.4826 * mad

        medians[i] = med
        sigma_estimates[i] = sigma

        if sigma > 0 and abs(signal[i] - med) / sigma > n_sigmas:
            result[i] = med
            outlier_indices.append(i)

    return HampelResult(result, outlier_indices, medians, sigma_estimates)


class HampelFilter:
    def __init__(self, window_size: int = 5, n_sigmas: float = 3.0):
        if window_size < 3:
            raise ValueError("window_size must be >= 3")
        self.window_size = window_size
        self.n_sigmas = n_sigmas

    def filter(self, data: np.ndarray) -> np.ndarray:
        """Replace outliers with median of local window."""
        result = data.copy()
        half = self.window_size // 2
        for i in range(len(data)):
            start = max(0, i - half)
            end = min(len(data), i + half + 1)
            window = data[start:end]
            med = np.median(window)
            mad = np.median(np.abs(window - med))
            if mad > 0 and abs(data[i] - med) / (mad * 1.4826) > self.n_sigmas:
                result[i] = med
        return result
