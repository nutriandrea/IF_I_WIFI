from dataclasses import dataclass
from typing import Optional
import numpy as np
from .fresnel import SPEED_OF_LIGHT
from . import spectrogram


class BvpError(Exception):
    """BVP extraction errors."""
    pass


@dataclass
class BvpConfig:
    window_size: int = 128
    hop_size: int = 32
    carrier_frequency: float = 5.0e9
    n_velocity_bins: int = 64
    max_velocity: float = 2.0


class BodyVelocityProfile:
    def __init__(self, data, velocity_bins, n_time, time_resolution, velocity_resolution):
        self.data = data
        self.velocity_bins = velocity_bins
        self.n_time = n_time
        self.time_resolution = time_resolution
        self.velocity_resolution = velocity_resolution


def extract_bvp(csi_temporal: np.ndarray, sample_rate: float,
                config: Optional[BvpConfig] = None) -> BodyVelocityProfile:
    """
    csi_temporal: (n_samples x n_subcarriers) amplitude matrix
    sample_rate: Hz

    Returns BodyVelocityProfile with shape (n_velocity_bins x n_time_frames)
    """
    if config is None:
        config = BvpConfig()

    n_samples, n_sc = csi_temporal.shape

    if n_samples < config.window_size:
        raise BvpError(f"Insufficient samples: need {config.window_size}, got {n_samples}")
    if n_sc == 0:
        raise BvpError("No subcarriers in input")
    if config.hop_size == 0 or config.window_size == 0:
        raise BvpError("window_size and hop_size must be > 0")

    wavelength = SPEED_OF_LIGHT / config.carrier_frequency
    n_frames = (n_samples - config.window_size) // config.hop_size + 1
    n_fft_bins = config.window_size // 2 + 1

    freq_resolution = sample_rate / config.window_size
    velocity_resolution = config.max_velocity * 2.0 / config.n_velocity_bins

    velocity_bins = np.linspace(-config.max_velocity, config.max_velocity, config.n_velocity_bins)

    aggregated = np.zeros((n_fft_bins, n_frames))

    for sc in range(n_sc):
        col = csi_temporal[:, sc]
        col_mean = np.mean(col)

        if col.ndim == 0:
            col = np.array([col])

        spec_config = spectrogram.SpectrogramConfig(
            window_size=config.window_size,
            hop_size=config.hop_size,
            window_fn=spectrogram.WindowFunction.Hann,
            power=False,
        )

        sig = col - col_mean
        spec = spectrogram.compute_spectrogram(sig, sample_rate, spec_config)
        aggregated += spec.data

    aggregated /= n_sc

    bvp = np.zeros((config.n_velocity_bins, n_frames))

    for v_idx, velocity in enumerate(velocity_bins):
        doppler_freq = 2.0 * velocity / wavelength
        fft_bin = int(round(abs(doppler_freq) / freq_resolution))
        if fft_bin < n_fft_bins:
            bvp[v_idx, :] = aggregated[fft_bin, :]

    return BodyVelocityProfile(
        data=bvp,
        velocity_bins=velocity_bins,
        n_time=n_frames,
        time_resolution=config.hop_size / sample_rate,
        velocity_resolution=velocity_resolution,
    )
