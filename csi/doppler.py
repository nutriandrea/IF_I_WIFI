"""Doppler shift extraction from CSI phase."""
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

_SCIPY_AVAILABLE = False
try:
    from scipy import fft as _scipy_fft
    _SCIPY_AVAILABLE = True
except ImportError:
    _scipy_fft = None

DOPPLER_FEATURE_NAMES = [
    "mean_doppler", "std_doppler",
    "max_positive_doppler", "max_negative_doppler",
    "doppler_abs_max", "doppler_band_power",
]
try:
    import numpy as _np
    _NUMPY_AVAILABLE = True
except ImportError:
    _np = None

class DopplerShiftExtractor:
    """
    Estrae lo spostamento Doppler dal segnale CSI.

    Il Doppler shift e' calcolato dalla differenza di fase tra frame
    consecutivi:
        f_Doppler = (dφ/dt) / (2π)

    Feature:
      - mean_doppler:         frequenza Doppler media (Hz)
      - std_doppler:          deviazione standard
      - max_positive_doppler: massimo shift positivo (avvicinamento)
      - max_negative_doppler: minimo shift negativo (allontanamento)
      - doppler_abs_max:      massimo |Doppler|
      - doppler_band_power:   energia spettrale banda 0.5-10 Hz

    Parameters
    ----------
    window_frames : int
        Numero frame nella finestra scorrevole (default 30).
    sample_rate_hz : float
        Frequenza di campionamento CSI (default 50 Hz).
    phase_sanitizer : PhaseSanitizer, optional
        PhaseSanitizer per preprocessing fase (default: auto).
    """

    def __init__(
        self,
        window_frames: int = 30,
        sample_rate_hz: float = 50.0,
        phase_sanitizer=None,
    ):
        self.window_size = window_frames
        self.sample_rate = sample_rate_hz
        self._buffer: deque = deque(maxlen=window_frames)
        if phase_sanitizer is not None:
            self._phase_sanitizer = phase_sanitizer
        else:
            from .phase_sanitizer import PhaseSanitizer
            self._phase_sanitizer = PhaseSanitizer()

    def add_frame(self, frame: dict):
        """Aggiunge un frame CSI al buffer."""
        self._buffer.append(frame)

    @property
    def ready(self) -> bool:
        """Vero se ci sono abbastanza frame per calcolare il Doppler."""
        return len(self._buffer) >= 3

    @property
    def n_frames(self) -> int:
        return len(self._buffer)

    def compute(self) -> dict:
        """
        Calcola le feature Doppler sul buffer corrente.

        Returns
        -------
        dict con feature Doppler, o dict vuoto se non pronto.
        """
        if not self.ready or not _NUMPY_AVAILABLE:
            return {}

        frames = list(self._buffer)
        phase_frames = _extract_phase_vectors(frames)
        if phase_frames is None or len(phase_frames) < 3:
            return {}

        # Sanitize phase
        phase_clean = self._phase_sanitizer.sanitize(phase_frames)

        # Phase difference lungo il tempo (axis=0)
        phase_diff = _np.diff(phase_clean, axis=0)
        # Normalizza in [-π, π]
        phase_diff = (phase_diff + _np.pi) % (2 * _np.pi) - _np.pi

        # Doppler: f = Δφ / (2π * Δt)
        dt = 1.0 / self.sample_rate
        doppler = phase_diff / (2 * _np.pi * dt)  # Hz

        # Media tra subcarrier
        doppler_mean = _np.mean(doppler, axis=1)

        if len(doppler_mean) == 0:
            return {}

        result: dict = {
            "mean_doppler": float(_np.mean(doppler_mean)),
            "std_doppler": float(_np.std(doppler_mean)),
            "max_positive_doppler": float(_np.max(doppler_mean)),
            "max_negative_doppler": float(_np.min(doppler_mean)),
            "doppler_abs_max": float(_np.max(_np.abs(doppler_mean))),
            "doppler_band_power": 0.0,
        }

        # Banda spettrale Doppler (0.5-10 Hz) se scipy disponibile
        if _SCIPY_AVAILABLE and len(doppler_mean) >= 8:
            signal = doppler_mean - _np.mean(doppler_mean)
            window = _np.hanning(len(signal))
            windowed = signal * window
            fft_vals = _scipy_fft.rfft(windowed)
            freqs = _scipy_fft.rfftfreq(len(signal), d=dt)
            psd = (_np.abs(fft_vals) ** 2) / len(signal)
            if len(freqs) > 1:
                mask = (freqs >= 0.5) & (freqs <= 10.0)
                result["doppler_band_power"] = float(_np.sum(psd[mask]))

        return result


def _extract_phase_vectors(frames: list) -> "_np.ndarray | None":
    """Estrae matrice fase (n_frames, n_sub) da lista frame CSI."""
    if not _NUMPY_AVAILABLE:
        return None
    phase_frames = []
    for f in frames:
        csi = f.get("csi")
        if not csi or not isinstance(csi, list):
            continue
        phases = [c.get("phase", 0.0) for c in csi if isinstance(c, dict)]
        if len(phases) >= 4:
            phase_frames.append(phases)
    if len(phase_frames) < 3:
        return None
    return _np.array(phase_frames, dtype=_np.float64)


