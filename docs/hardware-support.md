# Hardware support

## Sensor side (CSI capture)

| Board | Firmware target | Status | Notes |
| --- | --- | --- | --- |
| ESP32 (originale) | `esp32-radar3d` (Arduino) | Primario, testato | Setup attuale del team. CPU a 240 MHz, brownout abilitato (vedi sotto). |
| ESP32-S3 dev board (8 MB) | `esp32-csi-node` (ESP-IDF) | Primario | Default `sdkconfig.defaults`. Firmware ~250 KB. |
| ESP32-S3 SuperMini (4 MB) | `esp32-csi-node` (ESP-IDF) | Funziona | Edit `partitions.csv` per ridurre la factory partition. |
| ESP32-S3 con `esp32-radar3d` | `esp32-radar3d` (Arduino) | Non testato | Dovrebbe funzionare ma il rate gate va riverificato (cache race wDev_ProcessFiq). |
| ESP32-C6 | nessuno dei due | Sperimentale | RuView ha modulistica C6 (TWT, 802.15.4) che abbiamo tagliato in `esp32-csi-node`. |
| ESP32-C3 | n/a | Non supportato | Single-core, RAM insufficiente per WiFi + CSI cb + lwIP. |
| Original ESP32-S2 | n/a | Non testato | Promiscuous CSI esiste in IDF ma timing budget non verificato. |

### Note sull'alimentazione

Lo sketch `esp32-radar3d` originale del team disabilitava il brownout
detector. In questo repo lo abbiamo riabilitato. Se vedi reboot continui
sotto CSI:

- **Non** ri-disabilitare il brownout — è un workaround sintomatico.
- Verifica che il cavo USB sia **corto** (<1.5 m, AWG24 o meglio).
- Usa una porta USB-3 (900 mA) o un hub alimentato, non una USB-2 (500 mA).
- Se persiste, alimenta dal pin 5V con un alimentatore dedicato.

## Host side (aggregator)

| Host | Stack | Stato |
| --- | --- | --- |
| macOS Apple Silicon | `host-python` (.venv + Python 3.11) | Primario, usato dal team |
| macOS Apple Silicon | `host-rust` (cargo) | Compilazione non riverificata post-merge |
| Linux x86_64 | `host-python` | Funziona |
| Linux ARM64 | entrambi | Non testato qui, niente blockers |
| Windows | `host-python` | `pyserial` su COM funziona, `websockets` ok, ma il team non testa su Windows |

Il PC è il target operativo oggi. Almeno **4 GB RAM** servono per dare
respiro a numpy/scipy + sklearn + il modello blob in RSS. CPU: un core
recente è sufficiente.

## Future host: Arduino UNO Q

L'UNO Q ha NXP i.MX RT1062 (Cortex-M7 @ 600 MHz, 1 MB SRAM interna, DDR
esterna). Linux/RTOS, non un classico Arduino.

| Cosa è realistico portarci | Verdetto |
| --- | --- |
| Ricevere UDP da 3 ESP32 @ 100 Hz | Sì, throughput sovrabbondante |
| Eseguire `host-rust` (con porting no-std del transport) | Sì, target a medio termine |
| Eseguire `host-python` come è | No: numpy+scipy+sklearn+joblib non realistici |
| Esporre il WebSocket → UI web | Sì se gira un piccolo HTTP/WS server (qualche centinaio di LOC C/Rust) |
| Sostituire completamente il PC | Solo per cattura + DSP base; il dev resterà su PC |

Strategia: sviluppo contro PC con `host-python` finché la pipeline è
operativa. Quando l'algoritmica si stabilizza, porting del subset
necessario in Rust (`host-rust`) e da lì cross-compile a `armv7em-none-eabihf`.

## Arduino UNO R4 WiFi

| Cosa | Verdetto |
| --- | --- |
| Eseguire l'host stack | **No**. 32 KB SRAM (RA4M1) sono ~1 ordine di grandezza sotto il working set. |
| Ricevere CSI direttamente | **No**. Niente promiscuous CSI sul co-processor WiFi (ESP32-S3 firmware è chiuso). |
| Mostrare risultati su display | Sì, come peripheral. UART/SPI dall'UNO Q (o ESP32-S3) → OLED/TFT su UNO R4. |

Documentato per chiarezza: non perdere tempo a portarci niente di pesante.

## Hardware periferico opzionale (non incluso oggi)

Il repo team aveva strutture per integrare:

- Seeed MR60BHA2 (60 GHz FMCW, heart/breath/presence diretto).
- HLK-LD2410 (24 GHz FMCW presence + distance).
- Pannelli AMOLED per visualizzazione board-mounted.

Nessuno di questi è cablato nelle pipeline attuali. Se servono, i driver
upstream (`RuView/v2/crates/wifi-densepose-hardware` e
`firmware/esp32-csi-node/main/mmwave_sensor.c`) sono il punto di partenza.
Tutti deliberatamente non inclusi: vedi [`what-was-cut.md`](what-was-cut.md).
