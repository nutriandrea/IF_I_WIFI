"""RSSI-based feature extraction from CSI."""
import math
import time
from collections import deque
from statistics import mean, stdev

_NUMPY_AVAILABLE = False
try:
    import numpy as _np
    _NUMPY_AVAILABLE = True
except ImportError:
    _np = None

RSSI_FEATURE_NAMES = (
    "mean", "variance", "std", "skewness", "kurtosis",
    "range", "iqr",
    "dominant_freq_hz", "breathing_band_power",
    "motion_band_power", "total_spectral_power",
    "n_change_points", "n_samples", "duration_seconds", "sample_rate_hz",
)

class RSSIFeatures:
    """Feature RSSI: time-domain, frequency-domain, CUSUM change-points."""

    __slots__ = (
        "mean", "variance", "std", "skewness", "kurtosis",
        "range", "iqr",
        "dominant_freq_hz", "breathing_band_power",
        "motion_band_power", "total_spectral_power",
        "n_change_points", "n_samples", "duration_seconds", "sample_rate_hz",
    )

    def __init__(self):
        for s in self.__slots__:
            setattr(self, s, 0.0)
        self.n_samples = 0
        self.n_change_points = 0
        self.duration_seconds = 0.0
        self.sample_rate_hz = 0.0

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}

    def to_vector(self) -> list:
        return [getattr(self, n) for n in RSSI_FEATURE_NAMES]


try:
    import numpy as _np
    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False

try:
    from scipy import fft as _scipy_fft
    from scipy import stats as _scipy_stats
    _SCIPY_AVAILABLE = True
except ImportError:
    _SCIPY_AVAILABLE = False


class RSSIFeatureExtractor:
    """
    Feature extraction avanzata da serie temporale RSSI.

    - Time-domain: mean, variance, std, skewness, kurtosis, range, IQR
    - Frequency-domain: FFT con finestra Hann, bande respiratorie (0.1-0.5 Hz)
      e movimento (0.5-3.0 Hz), frequenza dominante
    - Change-point: CUSUM change-point detection

    Parameters
    ----------
    window_seconds : float
        Finestra temporale per l'analisi (default 30).
    cusum_threshold : float
        Soglia CUSUM in deviazioni standard (default 3.0).
    cusum_drift : float
        Drift CUSUM in deviazioni standard (default 0.5).
    """

    def __init__(
        self,
        window_seconds: float = 30.0,
        cusum_threshold: float = 3.0,
        cusum_drift: float = 0.5,
    ):
        self._window_seconds = window_seconds
        self._cusum_threshold = cusum_threshold
        self._cusum_drift = cusum_drift

    def extract(self, rssi_values: list, timestamps: list | None = None,
                sample_rate: float | None = None) -> RSSIFeatures:
        """Estrae feature da una lista di valori RSSI."""
        feats = RSSIFeatures()
        feats.n_samples = len(rssi_values)

        if len(rssi_values) < 4:
            return feats

        # Stima sample rate
        if sample_rate is not None:
            sr = sample_rate
        elif timestamps and len(timestamps) > 1:
            sr = 1.0 / (sum(timestamps[i] - timestamps[i-1]
                           for i in range(1, len(timestamps))) / (len(timestamps) - 1))
        else:
            sr = 10.0  # fallback

        feats.sample_rate_hz = sr
        if timestamps and len(timestamps) > 1:
            feats.duration_seconds = timestamps[-1] - timestamps[0]

        if _NUMPY_AVAILABLE:
            rssi = _np.array(rssi_values, dtype=_np.float64)
            self._compute_time_domain(rssi, feats)
            if _SCIPY_AVAILABLE:
                self._compute_frequency_domain(rssi, sr, feats)
            self._compute_change_points(rssi, feats)
        else:
            # Fallback numpy-free minimale
            self._compute_time_domain_fallback(rssi_values, feats)

        return feats

    def _compute_time_domain(self, rssi: "_np.ndarray", feats: RSSIFeatures):
        feats.mean = float(_np.mean(rssi))
        feats.variance = float(_np.var(rssi, ddof=1)) if len(rssi) > 1 else 0.0
        feats.std = float(_np.std(rssi, ddof=1)) if len(rssi) > 1 else 0.0
        feats.range = float(_np.ptp(rssi))

        if feats.std < 1e-12:
            feats.skewness = 0.0
            feats.kurtosis = 0.0
        else:
            feats.skewness = float(_scipy_stats.skew(rssi, bias=False)) if len(rssi) > 2 else 0.0
            feats.kurtosis = float(_scipy_stats.kurtosis(rssi, bias=False)) if len(rssi) > 3 else 0.0

        q75, q25 = _np.percentile(rssi, [75, 25])
        feats.iqr = float(q75 - q25)

    @staticmethod
    def _compute_time_domain_fallback(rssi_values: list, feats: RSSIFeatures):
        """Fallback minimale senza numpy."""
        n = len(rssi_values)
        if n == 0:
            return
        feats.mean = sum(rssi_values) / n
        if n > 1:
            feats.variance = sum((v - feats.mean) ** 2 for v in rssi_values) / (n - 1)
            feats.std = feats.variance ** 0.5
        feats.range = max(rssi_values) - min(rssi_values)
        sorted_vals = sorted(rssi_values)
        feats.iqr = sorted_vals[int(n * 0.75)] - sorted_vals[int(n * 0.25)]

    def _compute_frequency_domain(
        self, rssi: "_np.ndarray", sample_rate: float, feats: RSSIFeatures
    ):
        """FFT con finestra Hann, band power."""
        n = len(rssi)
        if n < 4:
            return

        signal = rssi - _np.mean(rssi)
        window = _np.hanning(n)
        windowed = signal * window

        fft_vals = _scipy_fft.rfft(windowed)
        freqs = _scipy_fft.rfftfreq(n, d=1.0 / sample_rate)
        psd = (_np.abs(fft_vals) ** 2) / n

        if len(freqs) <= 1:
            return

        freqs_no_dc = freqs[1:]
        psd_no_dc = psd[1:]
        feats.total_spectral_power = float(_np.sum(psd_no_dc))

        peak_idx = int(_np.argmax(psd_no_dc))
        feats.dominant_freq_hz = float(freqs_no_dc[peak_idx])

        feats.breathing_band_power = float(
            _band_power(freqs_no_dc, psd_no_dc, 0.1, 0.5)
        )
        feats.motion_band_power = float(
            _band_power(freqs_no_dc, psd_no_dc, 0.5, 3.0)
        )

    def _compute_change_points(self, rssi: "_np.ndarray", feats: RSSIFeatures):
        """CUSUM change-point detection."""
        if len(rssi) < 4:
            return
        mean_val = _np.mean(rssi)
        std_val = _np.std(rssi, ddof=1)
        if std_val < 1e-12:
            feats.n_change_points = 0
            return
        threshold = self._cusum_threshold * std_val
        drift = self._cusum_drift * std_val
        feats.n_change_points = _cusum_detect(rssi, mean_val, threshold, drift)


def _band_power(
    freqs: "_np.ndarray", psd: "_np.ndarray", low_hz: float, high_hz: float
) -> float:
    """Somma PSD in banda [low_hz, high_hz]."""
    mask = (freqs >= low_hz) & (freqs <= high_hz)
    return float(_np.sum(psd[mask]))


def _cusum_detect(
    signal: "_np.ndarray", target: float, threshold: float, drift: float
) -> int:
    """
    CUSUM change-point detection.
    Ritorna il numero di change-point trovati.
    """
    n = len(signal)
    s_pos = 0.0
    s_neg = 0.0
    count = 0
    for i in range(n):
        deviation = float(signal[i]) - target
        s_pos = max(0.0, s_pos + deviation - drift)
        s_neg = max(0.0, s_neg - deviation - drift)
        if s_pos > threshold or s_neg > threshold:
            count += 1
            s_pos = 0.0
            s_neg = 0.0
    return count

    def _compute_confidence(
        self, variance: float, motion_energy: float,
        breathing_energy: float, label: str,
    ) -> float:
        """Confidence score in [0, 1]."""
        # Base confidence (60%)
        if label == "EMPTY":
            base = max(0.0, 1.0 - variance / self._var_thresh) if self._var_thresh > 0 else 1.0
        else:
            ratio = variance / self._var_thresh if self._var_thresh > 0 else 10.0
            base = min(1.0, ratio)

        # Spectral confidence (20%)
        if label == "MOTION":
            spectral = min(1.0, motion_energy / max(self._motion_thresh, 1e-12))
        elif label == "STILL":
            spectral = min(1.0, breathing_energy / max(self._motion_thresh, 1e-12))
        else:
            spectral = 1.0

        # Agreement (20%) — default 1.0 per singolo ricevitore
        confidence = 0.6 * base + 0.2 * spectral + 0.2
        return max(0.0, min(1.0, confidence))


