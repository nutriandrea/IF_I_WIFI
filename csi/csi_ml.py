#!/usr/bin/env python3
"""
CSI ML Classifier — Backward-compat shim.

All functionality has been split into focused modules:
  - features.py         Feature extraction functions
  - classifier.py       CSIClassifier + constants + CLI
  - multi_ap.py         MultiAPCSIClassifier
  - rssi_features.py    RSSI feature extraction
  - doppler.py          Doppler shift extraction
  - sleep.py            Sleep quality analysis
  - breathing_ml.py     Phase-based breathing estimation

This file re-exports everything for backward compatibility.
"""
from .features import (
    GLOBAL_FEATURE_NAMES,
    SUB_MEAN_PREFIX,
    SUB_STD_PREFIX,
    SUB_PHASE_MEAN_PREFIX,
    SUB_PHASE_STD_PREFIX,
    NUM_CSI_SUBCARRIERS,
    CSI_FEATURE_SIZE,
    CSI_FEATURE_NAMES,
    MAX_SUB,
    extract_csi_profile,
    csi_window_to_vector,
    _extract_source_profile,
    _prefix_from_value,
    _generate_source_feature_names,
    _generate_position_feature_names,
    extract_csi_profile_per_source,
    csi_window_to_vector_per_source,
)

from .classifier import (
    MODEL_DIR,
    CSI_MODEL_PATH,
    POSITIONS_MODEL_PATH,
    POSITIONS_LABELS_PATH,
    EMPTY,
    STILL,
    MOTION,
    CSI_CLASSES,
    CSI_LABELS,
    CSIClassifier,
    main,
)

from .multi_ap import MultiAPCSIClassifier
from .rssi_features import RSSIFeatures, RSSIFeatureExtractor, RSSI_FEATURE_NAMES
from .doppler import DopplerShiftExtractor, DOPPLER_FEATURE_NAMES
from .sleep import SleepQualityAnalyzer, SLEEP_FEATURE_NAMES
from .breathing_ml import PhaseBreathingEstimator

__all__ = [
    "GLOBAL_FEATURE_NAMES", "SUB_MEAN_PREFIX", "SUB_STD_PREFIX",
    "SUB_PHASE_MEAN_PREFIX", "SUB_PHASE_STD_PREFIX",
    "NUM_CSI_SUBCARRIERS", "CSI_FEATURE_SIZE", "CSI_FEATURE_NAMES", "MAX_SUB",
    "extract_csi_profile", "csi_window_to_vector",
    "_extract_source_profile", "_prefix_from_value",
    "_generate_source_feature_names", "_generate_position_feature_names",
    "extract_csi_profile_per_source", "csi_window_to_vector_per_source",
    "MODEL_DIR", "CSI_MODEL_PATH", "POSITIONS_MODEL_PATH", "POSITIONS_LABELS_PATH",
    "EMPTY", "STILL", "MOTION", "CSI_CLASSES", "CSI_LABELS",
    "CSIClassifier", "main",
    "MultiAPCSIClassifier",
    "RSSIFeatures", "RSSIFeatureExtractor", "RSSI_FEATURE_NAMES",
    "DopplerShiftExtractor", "DOPPLER_FEATURE_NAMES",
    "SleepQualityAnalyzer", "SLEEP_FEATURE_NAMES",
    "PhaseBreathingEstimator",
]
