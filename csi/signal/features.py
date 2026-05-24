from __future__ import annotations

import numpy as np


class AmplitudeFeatures:
    def __init__(self, mean: np.ndarray, variance: np.ndarray,
                 peak: float, rms: float, dynamic_range: float):
        self.mean = mean
        self.variance = variance
        self.peak = peak
        self.rms = rms
        self.dynamic_range = dynamic_range

    @staticmethod
    def from_csi_data(amplitude: np.ndarray) -> AmplitudeFeatures:
        """amplitude shape: (n_antennas, n_subcarriers) or (n_subcarriers,)"""
        if amplitude.ndim == 1:
            amplitude = amplitude[np.newaxis, :]
        n_ant, n_sc = amplitude.shape

        mean = np.mean(amplitude, axis=0)
        variance = np.var(amplitude, axis=0, ddof=0)
        peak = float(np.max(amplitude))
        min_val = float(np.min(amplitude))
        dynamic_range = peak - min_val
        rms = float(np.sqrt(np.mean(amplitude ** 2)))

        return AmplitudeFeatures(mean, variance, peak, rms, dynamic_range)


class PhaseFeatures:
    def __init__(self, difference: np.ndarray, variance: np.ndarray,
                 gradient: np.ndarray, coherence: float):
        self.difference = difference
        self.variance = variance
        self.gradient = gradient
        self.coherence = coherence

    @staticmethod
    def from_csi_data(phase: np.ndarray) -> PhaseFeatures:
        """phase shape: (n_antennas, n_subcarriers) or (n_subcarriers,)"""
        if phase.ndim == 1:
            phase = phase[np.newaxis, :]
        n_ant, n_sc = phase.shape

        if n_sc >= 2:
            diff_matrix = np.diff(phase, axis=1)
            difference = np.mean(diff_matrix, axis=0)
        else:
            difference = np.array([])

        variance = np.var(phase, axis=0, ddof=0)

        if len(difference) >= 2:
            gradient = np.diff(difference)
        else:
            gradient = np.zeros(1)

        coherence = PhaseFeatures._calculate_coherence(phase)

        return PhaseFeatures(difference, variance, gradient, coherence)

    @staticmethod
    def _calculate_coherence(phase: np.ndarray) -> float:
        n_ant, n_sc = phase.shape
        if n_ant < 2 or n_sc == 0:
            return 0.0

        coherence_sum = 0.0
        count = 0

        for i in range(n_ant):
            for k in range(i + 1, n_ant):
                row_i = phase[i, :]
                row_k = phase[k, :]
                mean_i = np.mean(row_i)
                mean_k = np.mean(row_k)

                cov = np.sum((row_i - mean_i) * (row_k - mean_k))
                var_i = np.sum((row_i - mean_i) ** 2)
                var_k = np.sum((row_k - mean_k) ** 2)
                std_prod = np.sqrt(var_i * var_k)

                if std_prod > 1e-10:
                    coherence_sum += cov / std_prod
                    count += 1

        return float(coherence_sum / count) if count > 0 else 0.0


class CorrelationFeatures:
    def __init__(self, matrix: np.ndarray, mean_correlation: float,
                 max_correlation: float, rank_deficiency: int):
        self.matrix = matrix
        self.mean_correlation = mean_correlation
        self.max_correlation = max_correlation
        self.rank_deficiency = rank_deficiency

    @staticmethod
    def from_csi_data(amplitude: np.ndarray) -> CorrelationFeatures:
        """amplitude shape: (n_antennas, n_subcarriers) or (n_subcarriers,)"""
        if amplitude.ndim == 1:
            amplitude = amplitude[np.newaxis, :]
        n_ant, n_sc = amplitude.shape

        if n_ant < 2:
            corr_matrix = np.array([[1.0]])
            return CorrelationFeatures(corr_matrix, 0.0, 1.0, 0)

        corr_matrix = np.corrcoef(amplitude)

        triu_inds = np.triu_indices(n_ant, k=1)
        if triu_inds[0].size > 0:
            off_diag = corr_matrix[triu_inds]
            mean_corr = float(np.mean(off_diag))
            max_corr = float(np.max(np.abs(off_diag)))
        else:
            mean_corr = 0.0
            max_corr = 0.0

        rank = np.linalg.matrix_rank(corr_matrix)
        rank_def = n_ant - rank

        return CorrelationFeatures(corr_matrix, mean_corr, max_corr, rank_def)


class DopplerFeatures:
    def __init__(self, doppler_shift: np.ndarray, doppler_spread: float,
                 max_doppler_freq: float, spectral_entropy: float):
        self.doppler_shift = doppler_shift
        self.doppler_spread = doppler_spread
        self.max_doppler_freq = max_doppler_freq
        self.spectral_entropy = spectral_entropy

    @staticmethod
    def from_csi_data(amplitude_history: np.ndarray, sample_rate: float) -> DopplerFeatures:
        """amplitude_history shape: (n_samples, n_subcarriers) or (n_samples,)"""
        if amplitude_history.ndim == 1:
            amplitude_history = amplitude_history[:, np.newaxis]
        n_samples, n_sc = amplitude_history.shape

        if n_samples < 2:
            return DopplerFeatures(np.zeros(n_sc), 0.0, 0.0, 0.0)

        doppler_shift = np.zeros(n_sc)

        for j in range(n_sc):
            sig = amplitude_history[:, j]
            sig = sig - np.mean(sig)
            fft_vals = np.fft.fft(sig)
            magnitudes = np.abs(fft_vals)

            magnitudes[0] = 0
            max_idx = int(np.argmax(magnitudes))
            freq_res = sample_rate / n_samples

            if max_idx <= n_samples // 2:
                doppler_shift[j] = max_idx * freq_res
            else:
                doppler_shift[j] = (max_idx - n_samples) * freq_res

        magnitudes_all = np.abs(doppler_shift)
        max_doppler = float(np.max(magnitudes_all))
        mean_mag = float(np.mean(magnitudes_all))
        spread = float(np.std(magnitudes_all))

        psd = np.abs(np.fft.fft(np.mean(amplitude_history, axis=1))) ** 2
        psd_norm = psd / (np.sum(psd) + 1e-12)
        entropy = -float(np.sum(psd_norm * np.log2(psd_norm + 1e-12)))

        return DopplerFeatures(doppler_shift, spread, max_doppler, entropy)


class PowerSpectralDensity:
    def __init__(self, frequencies: np.ndarray, psd: np.ndarray, dominant_freq: float):
        self.frequencies = frequencies
        self.psd = psd
        self.dominant_freq = dominant_freq

    @staticmethod
    def from_signal(signal: np.ndarray, sample_rate: float) -> PowerSpectralDensity:
        """Compute PSD from a 1D signal using FFT (Welch-like periodogram)."""
        signal = signal - np.mean(signal)
        n = len(signal)
        fft_vals = np.fft.fft(signal)
        psd = np.abs(fft_vals[:n // 2]) ** 2 / (n * sample_rate)
        frequencies = np.fft.fftfreq(n, 1.0 / sample_rate)[:n // 2]

        if len(psd) > 0:
            dom_idx = int(np.argmax(psd))
            dominant_freq = float(frequencies[dom_idx])
        else:
            dominant_freq = 0.0

        return PowerSpectralDensity(frequencies, psd, dominant_freq)

    def band_power(self, low_hz: float, high_hz: float) -> float:
        mask = (self.frequencies >= low_hz) & (self.frequencies <= high_hz)
        return float(np.sum(self.psd[mask]))

    def spectral_centroid(self) -> float:
        total = np.sum(self.psd)
        if total > 1e-12:
            return float(np.sum(self.frequencies * self.psd) / total)
        return 0.0
