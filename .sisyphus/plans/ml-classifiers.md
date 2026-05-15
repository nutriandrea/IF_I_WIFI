# Plan: ML Classifiers for RSSI + CSI

## TODOs
- [x] Create rssi_ml.py — RSSIClassifier (RandomForest, window_size=20, 9 features)
- [x] Create csi_ml.py — CSIClassifier (RandomForest, ~88 features, ternary EMPTY/STATIONARY/MOVEMENT)
- [x] Integrate RSSIClassifier into enhanced_presence.py (--use-ml, --train-ml, --ml-model flags)
- [x] Integrate CSIClassifier into csi_processor.py (same flags + --stationary-seconds)
- [x] Integrate CSIClassifier into csi_mac.py (same flags + --stationary-seconds)
- [x] Create test_ml_classifiers.py (6 tests: 3 RSSI + 3 CSI)
- [x] Update README.md with Pipeline 3 — Machine Learning Classifiers

## Final Verification Wave
- [x] F1 — Goal Verifier: All requirements met
- [x] F2 — Code Reviewer: Code quality approved
- [x] F3 — Security Auditor: No security issues
- [x] F4 — QA Executor: Tests pass, edge cases handled
- [x] F5 — Context Miner: Integration consistent, docs accurate
