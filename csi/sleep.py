"""Sleep quality analysis from CSI."""
import math
import time
from collections import deque
from statistics import mean, stdev

from .rssi_features import RSSIFeatureExtractor

_NUMPY_AVAILABLE = False

SLEEP_FEATURE_NAMES = [
    "breathing_rate_bpm",
    "breathing_regularity",
    "sleep_stage",
    "sleep_confidence",
    "apnea_detected",
    "breathing_band_power",
    "change_points",
]
try:
    import numpy as _np
    _NUMPY_AVAILABLE = True
except ImportError:
    _np = None

class SleepQualityAnalyzer:
    """
    Analizza qualita' del sonno e respirazione da segnale RSSI.

    Usa la banda 0.1-0.5 Hz per estrarre:
      - Frequenza respiratoria (BPM)
      - Regolarita' del respiro
      - Stima stadio sonno: AWAKE / REM / LIGHT / DEEP
      - Rilevazione apnea (calo prolungato energia respiratoria)

    Parameters
    ----------
    window_seconds : float
        Finestra di analisi (default 60 s per respirazione).
    sample_rate_hz : float
        Frequenza campionamento RSSI (default 10 Hz).
    """

    # Bande per stima stadio sonno (BPM)
    _DEEP_RANGE = (6, 16)
    _LIGHT_RANGE = (12, 20)
    _REM_RANGE = (16, 24)

    # Soglia apnea: energia banda respiratoria sotto questa frazione
    # del massimo storico viene marcata come possibile apnea
    _APNEA_THRESHOLD = 0.02

    def __init__(
        self,
        window_seconds: float = 60.0,
        sample_rate_hz: float = 10.0,
    ):
        self._window_seconds = window_seconds
        self._sample_rate = sample_rate_hz
        self._extractor = RSSIFeatureExtractor(
            window_seconds=window_seconds,
            cusum_threshold=2.0,
            cusum_drift=0.3,
        )
        # Storico per rilevamento trend apnea
        self._breathing_history: deque = deque(maxlen=10)

    def analyze(
        self,
        rssi_values: list,
        timestamps: list | None = None,
        sample_rate: float | None = None,
    ) -> dict:
        """
        Analizza finestra di valori RSSI.

        Parameters
        ----------
        rssi_values : list
            Valori RSSI in dBm.
        timestamps : list, optional
            Timestamp Unix per ogni campione.
        sample_rate : float, optional
            Frequenza campionamento (override default).

        Returns
        -------
        dict con: breathing_rate_bpm, breathing_regularity,
        sleep_stage, sleep_confidence, apnea_detected,
        breathing_band_power, change_points.
        """
        sr = sample_rate or self._sample_rate
        feats = self._extractor.extract(rssi_values, timestamps, sr)

        # Frequenza respiratoria (Hz → BPM)
        breathing_rate_bpm = (
            feats.dominant_freq_hz * 60.0 if feats.dominant_freq_hz > 0 else 0.0
        )

        # Regolarita': 1.0 = perfettamente regolare, 0.0 = caotico
        # Usa il picco spettrale come proxy: piu' il picco e' stretto,
        # piu' la respirazione e' regolare
        if feats.breathing_band_power > 0:
            # std relativo alla media come misura di dispersione
            rel_std = feats.std / max(abs(feats.mean), 1e-6)
            breathing_regularity = max(0.0, 1.0 - min(rel_std, 1.0))
        else:
            breathing_regularity = 0.0

        # Stima stadio sonno
        stage, stage_conf = self._estimate_sleep_stage(
            breathing_rate_bpm, breathing_regularity, feats.breathing_band_power
        )

        # Apnea: energia respiratoria sotto soglia
        self._breathing_history.append(feats.breathing_band_power)
        apnea_detected = self._detect_apnea(feats.breathing_band_power)

        return {
            "breathing_rate_bpm": round(breathing_rate_bpm, 1),
            "breathing_regularity": round(breathing_regularity, 3),
            "sleep_stage": stage,
            "sleep_confidence": round(stage_conf, 3),
            "apnea_detected": apnea_detected,
            "breathing_band_power": round(feats.breathing_band_power, 6),
            "change_points": feats.n_change_points,
        }

    def _estimate_sleep_stage(
        self, bpm: float, regularity: float, band_power: float
    ) -> tuple:
        """Stima stadio sonno basato su BPM e regolarita'."""
        if band_power < self._APNEA_THRESHOLD * 0.5:
            return ("AWAKE", 0.0)

        deep_lo, deep_hi = self._DEEP_RANGE
        light_lo, light_hi = self._LIGHT_RANGE
        rem_lo, rem_hi = self._REM_RANGE

        if regularity > 0.6 and deep_lo <= bpm <= deep_hi:
            conf = min(1.0, regularity)
            return ("DEEP", conf)
        elif regularity > 0.3 and light_lo <= bpm <= light_hi:
            conf = min(1.0, regularity * 0.8 + 0.2)
            return ("LIGHT", conf)
        elif rem_lo <= bpm <= rem_hi:
            conf = 0.5 + 0.3 * regularity
            return ("REM", conf)
        else:
            conf = max(0.0, 0.5 - abs(bpm - 15) / 30)
            return ("AWAKE", conf)

    def _detect_apnea(self, current_power: float) -> bool:
        """Rileva possibile apnea: potenza attuale bassa rispetto allo storico recente."""
        if len(self._breathing_history) < 3:
            return False
        recent_max = max(self._breathing_history)
        if recent_max > 0:
            ratio = current_power / recent_max
            return ratio < 0.2 and recent_max > 0.01
        return False

    def reset(self):
        """Resetta lo storico (tra sessioni di monitoraggio)."""
        self._breathing_history.clear()


