"""Feature extraction functions for CSI data.

Extracted from csi_ml.py. No sklearn dependency.
"""
from collections import deque
from statistics import mean, stdev
import math

# ============================================================
# Shared constants
# ============================================================
GLOBAL_FEATURE_NAMES = [
    "variance_across_subcarriers",
    "max_var_subcarrier_index",
    "max_var_subcarrier_value",
    "temporal_variance",
    "temporal_std_variance",
    "sub_peak_mean",
    "sub_peak_std",
    "ampl_mean_min", "ampl_mean_max", "ampl_mean_range",
    "ampl_std_min", "ampl_std_max", "ampl_std_range",
    "rssi_mean", "rssi_std",
    "noise_floor_mean",
    "window_frames",
]

SUB_MEAN_PREFIX = "sub_mean_"
SUB_STD_PREFIX = "sub_std_"
SUB_PHASE_MEAN_PREFIX = "phase_mean_"
SUB_PHASE_STD_PREFIX = "phase_std_"
NUM_CSI_SUBCARRIERS = 64

CSI_FEATURE_SIZE = len(GLOBAL_FEATURE_NAMES) + 2 * NUM_CSI_SUBCARRIERS

CSI_FEATURE_NAMES = (
    GLOBAL_FEATURE_NAMES
    + [f"{SUB_MEAN_PREFIX}{i}" for i in range(NUM_CSI_SUBCARRIERS)]
    + [f"{SUB_STD_PREFIX}{i}" for i in range(NUM_CSI_SUBCARRIERS)]
)

MAX_SUB = 128

def extract_csi_profile(frames_window: list) -> dict:
    """Estrae feature da una finestra di frame CSI.

    Args:
        frames_window: Lista di dict CSI (da parse_csi_line) con chiave 'csi'.

    Returns:
        dict con feature, o {"_empty": True} se dati insufficienti.
    """
    n = len(frames_window)
    if n < 2:
        return {"_empty": True}

    # Colleziona ampiezze per-subcarrier e metriche globali per frame
    ampl_vectors = []
    ampl_means_per_frame = []
    ampl_stds_per_frame = []
    rssi_vals = []
    noise_vals = []

    for f in frames_window:
        csi = f.get("csi")
        if not csi or not isinstance(csi, list):
            continue

        amps = [c.get("ampl", 0) for c in csi if isinstance(c, dict)]
        if len(amps) < 2:
            continue

        ampl_vectors.append(amps)
        ampl_means_per_frame.append(mean(amps))
        ampl_stds_per_frame.append(stdev(amps))

        rssi = f.get("rssi")
        if rssi is not None:
            rssi_vals.append(float(rssi))

        noise = f.get("noise_floor")
        if noise is not None:
            noise_vals.append(float(noise))

    if not ampl_vectors:
        return {"_empty": True}

    # Allinea tutte le subcarrier alla stessa lunghezza (padding/trunc a MAX_SUB=128)
    global MAX_SUB
    aligned = []
    for v in ampl_vectors:
        if len(v) < MAX_SUB:
            v = list(v) + [0.0] * (MAX_SUB - len(v))
        aligned.append(v[:MAX_SUB])

    num_frames = len(aligned)
    num_sub = MAX_SUB

    # Profilo medio e std per subcarrier attraverso la finestra
    per_sub_mean = [
        mean(aligned[j][i] for j in range(num_frames)) for i in range(num_sub)
    ]
    per_sub_std = [
        stdev([aligned[j][i] for j in range(num_frames)]) if num_frames >= 2 else 0.0
        for i in range(num_sub)
    ]

    feats = {}
    feats["window_frames"] = num_frames

    # Feature globali
    feats["variance_across_subcarriers"] = round(
        stdev(per_sub_mean) if len(per_sub_mean) >= 2 else 0.0, 4
    )

    # Subcarrier con massima varianza temporale (è dove il multipath cambia di +)
    max_var_sub_idx = max(range(num_sub), key=lambda i: per_sub_std[i])
    feats["max_var_subcarrier_index"] = max_var_sub_idx
    feats["max_var_subcarrier_value"] = round(per_sub_std[max_var_sub_idx], 4)

    # Varianza temporale
    feats["temporal_variance"] = round(
        stdev(ampl_means_per_frame) if len(ampl_means_per_frame) >= 2 else 0.0, 4
    )
    feats["temporal_std_variance"] = round(
        stdev(ampl_stds_per_frame) if len(ampl_stds_per_frame) >= 2 else 0.0, 4
    )

    # Subcarrier con ampiezza maggiore (picchi spettrali)
    sorted_means = sorted(per_sub_mean, reverse=True)
    feats["sub_peak_mean"] = round(mean(sorted_means[:3]), 4) if len(sorted_means) >= 3 else round(mean(sorted_means), 4)
    feats["sub_peak_std"] = round(stdev(sorted_means[:3]), 4) if len(sorted_means) >= 3 else 0.0

    feats["ampl_mean_min"] = round(min(ampl_means_per_frame), 4)
    feats["ampl_mean_max"] = round(max(ampl_means_per_frame), 4)
    feats["ampl_mean_range"] = round(feats["ampl_mean_max"] - feats["ampl_mean_min"], 4)
    feats["ampl_std_min"] = round(min(ampl_stds_per_frame), 4)
    feats["ampl_std_max"] = round(max(ampl_stds_per_frame), 4)
    feats["ampl_std_range"] = round(feats["ampl_std_max"] - feats["ampl_std_min"], 4)

    feats["rssi_mean"] = round(mean(rssi_vals), 2) if rssi_vals else 0.0
    feats["rssi_std"] = round(stdev(rssi_vals), 2) if len(rssi_vals) >= 2 else 0.0
    feats["noise_floor_mean"] = round(mean(noise_vals), 2) if noise_vals else 0.0

    # Per-subcarrier feature (prime 32)
    for i in range(NUM_CSI_SUBCARRIERS):
        feats[f"{SUB_MEAN_PREFIX}{i}"] = round(per_sub_mean[i], 4)
        feats[f"{SUB_STD_PREFIX}{i}"] = round(per_sub_std[i], 4)

    return feats


def csi_window_to_vector(frames_window: list) -> list | None:
    """Converte finestra frame CSI in vettore feature flat per sklearn."""
    f = extract_csi_profile(frames_window)
    if f.get("_empty"):
        return None
    return [f[n] for n in CSI_FEATURE_NAMES]


# ============================================================
# Feature extraction per-MAC (per modello posizioni)
# ============================================================

def _extract_source_profile(frames: list, source_key: str = "mac",
                             source_value: str | None = None) -> dict | None:
    """Estrae ampl_mean, ampl_std, phase_mean, phase_std per una singola sorgente.

    La sorgente è identificata da ``source_key`` nel dict frame (es. "mac", "source_id").
    Se ``source_value`` è None, usa tutti i frame.

    Args:
        frames: lista di frame CSI.
        source_key: chiave nel dict frame per il raggruppamento (default "mac").
        source_value: valore di source_key da filtrare (None = tutti i frame).

    Returns:
        dict con 'ampl_mean', 'ampl_std', 'phase_mean', 'phase_std',
        ciascuna lista MAX_SUB valori. None se dati insufficienti.
    """
    if source_value is not None:
        filtered = [f for f in frames if f.get(source_key) == source_value]
    else:
        filtered = frames

    if len(filtered) < 2:
        return None

    ampl_vectors = []
    phase_vectors = []
    for f in filtered:
        csi = f.get("csi")
        if not csi or not isinstance(csi, list):
            continue
        amps = [c.get("ampl", 0) for c in csi if isinstance(c, dict)]
        phases = [c.get("phase", 0) for c in csi if isinstance(c, dict)]
        if len(amps) < 2:
            continue
        ampl_vectors.append(amps)
        phase_vectors.append(phases)

    if not ampl_vectors:
        return None

    aligned_amp = []
    for v in ampl_vectors:
        if len(v) < MAX_SUB:
            v = list(v) + [0.0] * (MAX_SUB - len(v))
        aligned_amp.append(v[:MAX_SUB])

    aligned_phase = []
    for v in phase_vectors:
        if len(v) < MAX_SUB:
            v = list(v) + [0.0] * (MAX_SUB - len(v))
        aligned_phase.append(v[:MAX_SUB])

    nf = len(aligned_amp)

    def _mean_col(idx):
        return mean(aligned_amp[j][idx] for j in range(nf))

    def _std_col(idx):
        return stdev([aligned_amp[j][idx] for j in range(nf)]) if nf >= 2 else 0.0

    def _phase_mean_col(idx):
        return mean(aligned_phase[j][idx] for j in range(nf))

    def _phase_std_col(idx):
        return stdev([aligned_phase[j][idx] for j in range(nf)]) if nf >= 2 else 0.0

    return {
        "ampl_mean": [_mean_col(i) for i in range(MAX_SUB)],
        "ampl_std": [_std_col(i) for i in range(MAX_SUB)],
        "phase_mean": [_phase_mean_col(i) for i in range(MAX_SUB)],
        "phase_std": [_phase_std_col(i) for i in range(MAX_SUB)],
    }


# Backward compat alias
_extract_mac_profile = lambda *a, **kw: _extract_source_profile(*a, source_key="mac", **kw)


def _prefix_from_value(value: str, source_key: str) -> str:
    """Genera prefisso leggibile per feature name da un valore sorgente."""
    if source_key == "mac" and len(value) > 8:
        return value[-8:]  # ultimi 8 hex del MAC
    return value  # source_id o MAC corto


def _generate_source_feature_names(values: list[str], source_key: str = "mac") -> list[str]:
    """Genera feature names per modello con per-source + fase + RSSI.

    Include feature globali + per ogni sorgente:
      rssi_mean, rssi_weight,
      sub_mean_0..N-1, sub_std_0..N-1,
      phase_mean_0..N-1, phase_std_0..N-1.

    RSSI_weight = 1/(|rssi|+1): peso alto = nodo vicino = più affidabile.
    Il Random Forest impara a fidarsi di più delle sorgenti con RSSI alto.

    Args:
        values: lista di valori sorgente (MAC address o source_id).
        source_key: "mac" → usa ultimi 8 hex, altro → usa valore intero.

    Returns:
        list[str]: nomi feature ordinati.
    """
    names = list(GLOBAL_FEATURE_NAMES)
    for val in values:
        prefix = _prefix_from_value(val, source_key)
        # RSSI features (ispirato da RuView: RSSI-weighted fusion)
        names.append(f"{prefix}_rssi_mean")
        names.append(f"{prefix}_rssi_weight")
        for i in range(NUM_CSI_SUBCARRIERS):
            names.append(f"{prefix}_{SUB_MEAN_PREFIX}{i}")
            names.append(f"{prefix}_{SUB_STD_PREFIX}{i}")
            names.append(f"{prefix}_{SUB_PHASE_MEAN_PREFIX}{i}")
            names.append(f"{prefix}_{SUB_PHASE_STD_PREFIX}{i}")
    return names


# Backward compat alias
_generate_position_feature_names = lambda macs: _generate_source_feature_names(macs, "mac")


def extract_csi_profile_per_source(frames_window: list, known_sources: list[str],
                                    source_key: str = "mac") -> dict:
    """Estrae feature per-sorgente con fase + RSSI + feature globali.

    Per ogni known_source, estrae profilo ampl_mean/ampl_std/phase_mean/phase_std
    e RSSI_mean/RSSI_weight dai soli frame di quella sorgente nella finestra.

    RSSI_weight = 1/(|RSSI_mean|+1): peso alto = nodo vicino.
    Il modello impara a fidarsi più delle sorgenti con RSSI alto
    (ispirato da RuView: cross-node RSSI-weighted feature fusion).

    Args:
        frames_window: Lista di dict CSI.
        known_sources: Lista di valori sorgente (MAC o source_id).
        source_key: Chiave nel dict frame per filtrare (default "mac").

    Returns:
        dict con feature, o {"_empty": True}.
    """
    profile = extract_csi_profile(frames_window)
    if profile.get("_empty"):
        return {"_empty": True}

    for val in known_sources:
        sp = _extract_source_profile(frames_window, source_key, val)
        prefix = _prefix_from_value(val, source_key)

        # Calcola RSSI medio per questa sorgente
        src_frames = [f for f in frames_window
                      if isinstance(f, dict) and f.get(source_key) == val]
        rssi_vals = [f.get("rssi", -90) for f in src_frames if isinstance(f, dict)]
        rssi_mean_v = mean(rssi_vals) if rssi_vals else -90.0
        rssi_weight = round(1.0 / (abs(rssi_mean_v) + 1.0), 4)

        profile[f"{prefix}_rssi_mean"] = round(rssi_mean_v, 2)
        profile[f"{prefix}_rssi_weight"] = rssi_weight

        if sp is None:
            for i in range(NUM_CSI_SUBCARRIERS):
                profile[f"{prefix}_{SUB_MEAN_PREFIX}{i}"] = 0.0
                profile[f"{prefix}_{SUB_STD_PREFIX}{i}"] = 0.0
                profile[f"{prefix}_{SUB_PHASE_MEAN_PREFIX}{i}"] = 0.0
                profile[f"{prefix}_{SUB_PHASE_STD_PREFIX}{i}"] = 0.0
        else:
            for i in range(NUM_CSI_SUBCARRIERS):
                profile[f"{prefix}_{SUB_MEAN_PREFIX}{i}"] = round(sp["ampl_mean"][i], 4)
                profile[f"{prefix}_{SUB_STD_PREFIX}{i}"] = round(sp["ampl_std"][i], 4)
                profile[f"{prefix}_{SUB_PHASE_MEAN_PREFIX}{i}"] = round(sp["phase_mean"][i], 4)
                profile[f"{prefix}_{SUB_PHASE_STD_PREFIX}{i}"] = round(sp["phase_std"][i], 4)

    profile["_sources"] = known_sources
    profile["_source_key"] = source_key
    return profile


# Backward compat alias
extract_csi_profile_per_mac = lambda f, m: extract_csi_profile_per_source(f, m, "mac")


def csi_window_to_vector_per_source(frames_window: list, known_sources: list[str],
                                     feature_names: list[str],
                                     source_key: str = "mac") -> list | None:
    """Converte finestra in vettore feature flat."""
    f = extract_csi_profile_per_source(frames_window, known_sources, source_key)
    if f.get("_empty"):
        return None
    return [f[n] for n in feature_names]


# Backward compat alias
csi_window_to_vector_per_mac = lambda f, m, fn: csi_window_to_vector_per_source(f, m, fn, "mac")

