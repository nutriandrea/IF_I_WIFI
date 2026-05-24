# Flashing Guide — ESP32 Radar 3D

## Prerequisiti

- 3x ESP32 (qualsiasi modello)
- Arduino IDE o PlatformIO
- Cavi USB
- PC con python3 + scikit-learn + joblib

## Arduino IDE Setup

1. **Aggiungi board ESP32**:
   - File → Preferenze → URL: `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`
   - Strumenti → Board → Boards Manager → cerca "ESP32" → installa

2. **Apri il firmware**: `firmware/esp32_radar3d/esp32_radar3d.ino`

## Step 1 — Scoprire i MAC

Collega UN ESP32 alla volta:

1. Lascia `NODE_ID 0` in `network_config.h`
2. Carica il firmware
3. Apri Serial Monitor (115200 baud)
4. Leggi il MAC stampato all'avvio
5. Annotalo in `NODE_MACS[0]` in `network_config.h`
6. Cambia `NODE_ID` a 1, ripeti → MAC in `NODE_MACS[1]`
7. Cambia `NODE_ID` a 2, ripeti → MAC in `NODE_MACS[2]`

## Step 2 — Configurare

In `network_config.h`:

```c
#define NODE_ID  0    // CAMBIA per ogni ESP32 prima di caricare

static const uint8_t NODE_MACS[3][6] = {
    {0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x00},  // MAC reale NODE 0
    {0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01},  // MAC reale NODE 1
    {0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x02},  // MAC reale NODE 2
};

#define UDP_TARGET_IP   "192.168.1.100"      // IP del PC che esegue run.py
#define UDP_TARGET_PORT 5005                 // deve matchare --udp-port
```

In `secrets.h` (NON committare):

```c
#define WIFI_SSID   "IlTuoWiFi"
#define WIFI_PASS   "LaPassword"
```

## Step 3 — Caricare su tutti e 3

Carica lo SKETCH su ogni ESP32 **dopo aver cambiato NODE_ID**.
Usa Settings upload speed: 921600 (più veloce).

## Step 4 — Posizionare i 3 ESP32

Disponi i 3 ESP32 in TRIANGOLO nella stanza da monitorare.
Posizionali a circa 1m da terra, con vista libera sulla stanza.

```
        ESP32-0
        /    \
       /      \
  ESP32-1 ——— ESP32-2
```

La distanza tra i nodi dovrebbe essere 2-5m per avere buona diversità spaziale.

Accendi tutti e 3 contemporaneamente.

## Step 5 — Verificare ricezione

Sul PC:

```bash
cd /path/to/project

# Verifica che i pacchetti arrivino
python3 -m csi.csi_mac --udp-port 5005 --capture --seconds 15
```

Se tutto funziona, vedrai frame con 9 percorsi TX→RX.

## Step 6 — Collezione dati (griglia)

Con 3 ESP32 accesi e funzionanti:

```bash
# Crea una griglia 3x3 in stanza (9 posizioni, 30s ciascuna)
python3 -m csi.csi_mac --udp-port 5005 --positions --grid 3x3 --seconds 30
```

Segui le istruzioni a schermo:
1. **FASE 1**: tutti fuori dalla stanza (baseline "vuoto", 30s)
2. Per ogni cella `rXcY`: mettiti in quella posizione premi INVIO (30s)

**Layout griglia**: la cella `r0c0` è l'angolo in basso a sinistra.
Se la stanza è 6×4m con griglia 3×3, ogni cella è ~2×1.3m.

Dopo la raccolta, il modello viene addestrato automaticamente
e salvato in `csi/csi_positions_regressor_model.joblib`.

## Step 7 — Avviare il monitor 3D

Dopo l'addestramento:

```bash
python3 run.py --udp-port 5005 --3d
```

Il browser mostrerà la stanza con blob 3D in tempo reale.
