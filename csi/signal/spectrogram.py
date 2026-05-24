from enum import Enum
from dataclasses import dataclass
from typing import Optional
import numpy as np


class WindowFunction(Enum):
    Rectangular = "rectangular"
    Hann = "hann"
    Hamming = "hamming"
    Blackman = "blackman"


@dataclass
class SpectrogramConfig:
    window_size: int = 256
    hop_size: int = 64
    window_fn: WindowFunction = WindowFunction.Hann
    power: bool = True


class Spectrogram:
    def __init__(self, data: np.ndarray, n_freq: int, n_time: int,
                 freq_resolution: float, time_resolution: float):
        self.data = data
        self.n_freq = n_freq
        self.n_time = n_time
        self.freq_resolution = freq_resolution
        self.time_resolution = time_resolution

    def to_db(self) -> np.ndarray:
        """Convert to dB scale (10*log10)."""
        return 10 * np.log10(self.data + 1e-12)


def compute_spectrogram(signal: np.ndarray, sample_rate: float,
                        config: Optional[SpectrogramConfig] = None) -> Spectrogram:
    """
    Compute spectrogram of a 1D signal.
    Uses numpy FFT-based STFT with configurable window function.
    """
    if config is None:
        config = SpectrogramConfig()

    win = config.window_size
    hop = config.hop_size
    n = len(signal)

    if n < win:
        raise ValueError(f"Signal too short: {n} < window {win}")

    if config.window_fn == WindowFunction.Rectangular:
        window = np.ones(win)
    elif config.window_fn == WindowFunction.Hann:
        window = np.hanning(win)
    elif config.window_fn == WindowFunction.Hamming:
        window = np.hamming(win)
    elif config.window_fn == WindowFunction.Blackman:
        window = np.blackman(win)
    else:
        window = np.ones(win)

    n_frames = 1 + (n - win) // hop
    n_freq = win // 2 + 1
    stft = np.zeros((n_freq, n_frames), dtype=np.complex128)

    for t in range(n_frames):
        start = t * hop
        segment = signal[start:start + win] * window
        spectrum = np.fft.rfft(segment)
        stft[:, t] = spectrum

    if config.power:
        data = np.abs(stft) ** 2
    else:
        data = np.abs(stft)

    freq_res = sample_rate / win
    time_res = hop / sample_rate

    return Spectrogram(data, n_freq, n_frames, freq_res, time_res)
