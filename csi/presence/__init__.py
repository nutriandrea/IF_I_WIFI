"""
csi.presence — Motion / EMPTY / STILL / MOTION detection.

Funzionalità 1 di feat/wifi-sensing-final.

Pipeline:
    CSI frames → variance per (tx_node, rx_node) → EMA → state machine
                                                     → JSON status + sparkline

Funziona con 1, 2, o 3 ESP32 (degradazione graduale).

Sub-moduli:
    detector    — algoritmo core (no ML, no overfitting)
    monitor_cli — dashboard CLI con `rich` (o output JSON-line strutturato)
"""
