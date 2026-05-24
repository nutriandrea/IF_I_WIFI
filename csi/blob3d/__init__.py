"""
csi.blob3d — 3D blob tracking (best-effort).

Funzionalità 3 di feat/wifi-sensing-final.

Realisticamente con 3 ESP32 planari il Z è osservabile solo per macro-classi
(in piedi / seduto / a terra). Non promettiamo precisione cm.

Architettura:
    (x, y)  →  da csi.quadrants.regressor
    z       →  heuristic su energia inter-band (sub-banda alta vs bassa)
              + Kalman 3D constant-velocity con stato (x,y,z,vx,vy,vz)

Disabilitato se < 3 ESP32 attivi (lo dichiariamo esplicitamente nella UI).
"""
