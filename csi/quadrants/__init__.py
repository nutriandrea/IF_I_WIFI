"""
csi.quadrants — 2D quadrant / cell localization.

Funzionalità 2 di feat/wifi-sensing-final.

Due strategie disponibili:
    blob_live    — DEFAULT, NO ML. Centroid pesato per varianza + Gaussian 2D.
                   Funziona da subito, zero training, zero overfitting.
    regressor    — OPT-IN. RandomForestRegressor (x, y) continuo + Kalman 2D.
                   Solo dopo calibrazione validata leave-one-cell-out.

Output via WebSocket (`ws_server`) → frontend `mapping/ui.html`.
"""
