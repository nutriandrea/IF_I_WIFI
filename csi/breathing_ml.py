"""Phase-based breathing rate estimation from CSI."""
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

class PhaseBreathingEstimator:
    """Stima BPM (respirazione) dalla fase CSI.

    Accumula una finestra di fase per ogni subcarrier e stima
    il BPM dominante tramite zero-crossing dopo bandpass 0.1-0.5 Hz.

    Ispirato da RuView edge_processing.c (Tier 2: Vitals pipeline).
    """

    def __init__(self, window_seconds: float = 10.0, sample_rate: float = 30.0):
        self.window_samples = int(window_seconds * sample_rate)
        self.sample_rate = sample_rate
        # phase_history[subcarrier_idx] = deque di fasi unwrapped
        self._phase_hist: dict[int, deque] = {}
        self._last_phase: dict[int, float] = {}
        self._bpm_ema = 0.0
        self._ema_alpha = 0.3
        self._min_frames = max(4, int(sample_rate * 2))  # almeno 2 secondi
        # Tenta import scipy per filtro Butterworth
        self._have_scipy = False
        try:
            from scipy import signal as _sig
            self._sos = _sig.butter(4, [0.1, 0.5], btype='band', fs=sample_rate, output='sos')
            self._sos_filter = _sig.sosfilt
            self._have_scipy = True
        except ImportError:
            pass

    def add_frame(self, frame: dict) -> None:
        """Aggiunge frame, accumula fase unwrapped per ogni subcarrier."""
        csi = frame.get("csi")
        if not csi:
            return
        for c in csi:
            if not isinstance(c, dict):
                continue
            idx = c.get("subcarrier", 0)
            raw_phase = c.get("phase", 0.0)

            # Unwrap phase: correggi salti 2π
            if idx in self._last_phase:
                delta = raw_phase - self._last_phase[idx]
                if delta > _np.pi:
                    raw_phase -= 2 * _np.pi
                elif delta < -_np.pi:
                    raw_phase += 2 * _np.pi
            self._last_phase[idx] = raw_phase

            if idx not in self._phase_hist:
                self._phase_hist[idx] = deque(maxlen=self.window_samples)
            self._phase_hist[idx].append(raw_phase)

    @property
    def ready(self) -> bool:
        """Pronto se ogni subcarrier ha almeno min_frames campioni."""
        if not self._phase_hist:
            return False
        lens = [len(h) for h in self._phase_hist.values()]
        return min(lens) >= self._min_frames if lens else False

    @property
    def bpm(self) -> float:
        """BPM corrente (smooth con EMA)."""
        return round(self._bpm_ema, 1)

    def estimate(self) -> dict:
        """Calcola BPM dalla fase migliore subcarrier.

        Returns:
            dict con 'bpm', 'confidence', 'n_subcarriers', o dict vuoto.
        """
        if not self.ready:
            return {"bpm": 0.0, "confidence": 0.0}

        best_bpm = 0.0
        best_energy = -1.0
        n_sc = 0

        for sc_idx, hist in self._phase_hist.items():
            vals = list(hist)
            if len(vals) < self._min_frames:
                continue
            n_sc += 1

            arr = _np.array(vals, dtype=_np.float64)
            arr = arr - _np.mean(arr)

            # Filtra banda respiratoria
            if self._have_scipy:
                # Butterworth bandpass 0.1-0.5 Hz (4° ordine, SOS)
                filtered = self._sos_filter(self._sos, arr)
                signal = filtered
            else:
                # Fallback: moving average smoothing (0.3 sec) + diff
                window = max(1, int(self.sample_rate * 0.3))
                kernel = _np.ones(window) / window
                smoothed = _np.convolve(arr, kernel, mode='same')
                signal = _np.diff(smoothed)

            # Zero-crossing count sul segnale filtrato
            zc = _np.sum(_np.diff(_np.signbit(signal).astype(int)) != 0)
            bpm_val = zc * self.sample_rate / (2 * len(signal)) * 60

            if not (2.0 < bpm_val < 40.0):
                continue

            # Energia nella banda respiratoria (proxy SNR)
            energy = float(_np.sum(signal ** 2)) / len(signal)

            if energy > best_energy:
                best_energy = energy
                best_bpm = bpm_val

        if best_energy < 0 or best_bpm == 0.0:
            return {"bpm": 0.0, "confidence": 0.0, "n_subcarriers": n_sc}

        # EMA smoothing
        if self._bpm_ema == 0.0:
            self._bpm_ema = best_bpm
        else:
            self._bpm_ema = self._ema_alpha * best_bpm + (1 - self._ema_alpha) * self._bpm_ema

        # Confidence: confronta energia banda respiratoria con energia out-of-band
        # Calcola energia media per subcarrier su RAW e su bandpass
        raw_var_sum = 0.0
        bp_var_sum = 0.0
        bp_samples = 0
        for sc_idx, hist in self._phase_hist.items():
            vals = list(hist)
            if len(vals) < 10:
                continue
            arr = _np.array(vals, dtype=_np.float64)
            arr = arr - _np.mean(arr)
            raw_var = float(_np.var(arr))
            raw_var_sum += raw_var
            if self._have_scipy:
                bp = self._sos_filter(self._sos, arr)
                bp_var_sum += float(_np.var(bp))
                bp_samples += 1
        n_sc_used = max(1, bp_samples)
        avg_raw_var = raw_var_sum / n_sc_used
        avg_bp_var = bp_var_sum / n_sc_used

        # SNR-like: rapporto energia banda-resp / (totale - banda-resp)
        if avg_bp_var > 0 and avg_raw_var > avg_bp_var:
            snr = avg_bp_var / (avg_raw_var - avg_bp_var + 1e-10)
        elif avg_bp_var > 0:
            snr = 10.0  # tutta l'energia è nella banda
        else:
            snr = 0.0
        # range factor: penalizza BPM fuori range fisiologico
        range_factor = 1.0 if 6.0 <= self._bpm_ema <= 30.0 else 0.2
        confidence = min(1.0, snr * 3.0 * range_factor)

        return {
            "bpm": round(self._bpm_ema, 1),
            "confidence": round(confidence, 3),
            "n_subcarriers": n_sc,
        }

    def reset(self) -> None:
        """Resetta lo storico fasi."""
        self._phase_hist.clear()
        self._last_phase.clear()
        self._bpm_ema = 0.0


if __name__ == "__main__":
    main()
