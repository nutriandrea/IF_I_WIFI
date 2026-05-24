# Hardware Setup — 3-node ESP32 radar

Questa guida copre il setup fisico e di rete per fare cross-ping CSI con 3 ESP32.

---

## 1. Cosa serve

| Voce | Quantità | Note |
|---|---|---|
| ESP32-S3 DevKitC (consigliato) | 3 | Anche ESP32 classico WROOM-32 funziona, ma con CSI più rumoroso |
| Cavi USB-C dati (non solo charge) | 3 | Per flash + alimentazione |
| Alimentazione USB | 3 prese | Powerbank, hub alimentato, o porte PC |
| PC (Mac/Linux/Windows) | 1 | Riceve UDP, gira la pipeline Python |
| AP WiFi 2.4 GHz | 1 | Solo per portare i frame UDP al PC (non per il sensing) |

> **Importante**: gli ESP32 e il PC devono essere sulla **stessa rete WiFi 2.4 GHz**. Il PC e gli ESP32 si parlano via UDP. Se l'hotspot è in 5 GHz, gli ESP32 classici non lo vedono.

---

## 2. Layout fisico

Posiziona i 3 ESP32 in **triangolo** nella stanza:

```
        NODE 0
         /  \
        /    \
       /      \
   NODE 1 ── NODE 2
```

- Distanza tra nodi: **2–5 metri** (per avere buona diversità spaziale).
- Altezza dal pavimento: **~1 m** (a "altezza torso", riduce riflessi multipli).
- Idealmente nessun ostacolo metallico tra i 3 nodi.
- Posizione nota e misurata: ti servirà per il flag `--rx x,y;x,y;x,y` del server.

**Esempio stanza 6×5 m:**
- NODE 0 a (0.5, 0.5) m — angolo basso-sinistra
- NODE 1 a (5.5, 0.5) m — angolo basso-destra
- NODE 2 a (3.0, 4.5) m — centro alto

---

## 3. Discovery dei MAC

Ogni ESP32 ha un MAC unico stampato all'avvio. Per scoprirli senza fare 3 flash manuali:

```bash
# Collega UN ESP32 alla volta via USB
# Apri il seriale a 115200 e premi reset
# Vedi nel log: "ESP32 MAC Address: AA:BB:CC:DD:EE:FF"
```

Oppure usa il tool helper:
```bash
PYTHONPATH=. python3 tools/discover_macs.py --port /dev/cu.usbserial-XXX
```

Questo:
1. Apre il seriale, fa reset, legge il MAC dell'ESP32 collegato.
2. Ti chiede in quale posizione (NODE 0/1/2) salvarlo.
3. Aggiorna `firmware/esp32_radar3d/network_config.h` automaticamente.
4. Ripeti per ogni ESP32.

---

## 4. Configurazione `network_config.h`

```c
#define NODE_ID 0    // <-- CAMBIA per ogni ESP32 prima del flash!

static const uint8_t NODE_MACS[3][6] = {
  { 0xE4, 0xB0, 0x63, 0xAE, 0xEE, 0x30 },  // NODE 0
  { 0xE4, 0xB0, 0x63, 0xAF, 0x00, 0x50 },  // NODE 1
  { 0x20, 0xE7, 0xC8, 0xB4, 0xBF, 0x08 },  // NODE 2
};

#define UDP_TARGET_IP   "192.168.1.100"    // IP del PC (ifconfig en0 / ip addr)
#define UDP_TARGET_PORT 5005
```

E `secrets.h`:
```c
#define WIFI_SSID  "TuoSSID2.4GHz"
#define WIFI_PASS  "TuaPassword"
```

---

## 5. Flash su Arduino IDE

1. **Tools → Board → ESP32 Dev Module** (o ESP32-S3 Dev Module se hai S3).
2. **Tools → Upload Speed → 115200** (più lento = più affidabile su cavi non perfetti).
3. **Tools → Port** → seleziona la porta del primo ESP32.
4. **NODE_ID = 0** in `network_config.h` → carica.
5. **NODE_ID = 1** → ricollega secondo ESP32 → carica.
6. **NODE_ID = 2** → ricollega terzo ESP32 → carica.

Verifica nel Serial Monitor di ognuno:
```
=== ESP32 Radar 3D — NODE 0 ===
ESP32 MAC Address: E4:B0:63:AE:EE:30
WiFi connecting....
Canale WiFi: 6
CSI attivo. Cross-ping 100 Hz...
[0] TX:500 CSI:1487
```

---

## 6. Lato PC

1. **Trova il tuo IP** (deve coincidere con `UDP_TARGET_IP` nel firmware):
   ```bash
   # Mac
   ipconfig getifaddr en0
   # Linux
   ip -4 -o addr show | awk '{print $2, $4}'
   ```

2. **Lancia il server**:
   ```bash
   cd ArduinoWifiSensing
   PYTHONPATH=. python3 -m csi.quadrants.ws_server \
       --udp-port 5005 --ws-port 8765 \
       --room 6x5 --grid 4x4 \
       --rx "0.5,0.5;5.5,0.5;3.0,4.5" \
       --enable-3d
   ```

3. **Apri la UI**:
   ```
   file:///$(pwd)/mapping/ui.html?ws=ws://localhost:8765&room=6x5&grid=4x4&3d=1
   ```
   (Cmd-click sul percorso da terminale su Mac.)

---

## 7. Verifica end-to-end

In console del server vedrai:

```
[ws] UDP in ascolto su :5005
[ws] WebSocket server su ws://0.0.0.0:8765
[ws] Blob3DTracker abilitato (z best-effort, macro-classi)
[ws] Modello regressor non disponibile/non validato: uso blob_live
[ws]  98.3 fps  paths= 9  state=EMPTY      blob=(3.0,2.5)
[ws] 100.1 fps  paths= 9  state=MOVEMENT   blob=(1.2,3.8)
```

- `paths=9` significa che tutti i 9 percorsi TX→RX (3×3) stanno arrivando. Se vedi `paths=3` o `paths=6`, uno o due ESP32 non stanno trasmettendo o ricevendo correttamente.
- `state=EMPTY` durante i primi 30 s mentre fai la calibrazione (stai fuori stanza).

---

## 8. Dimensioni stanza e griglia consigliate

| Stanza | Grid consigliata | Cella |
|---|---|---|
| 3×3 m (camera piccola) | 3×3 | 1×1 m |
| 4×5 m (ufficio) | 4×4 | 1×1.25 m |
| 6×5 m (soggiorno) | 4×4 | 1.5×1.25 m |
| 8×6 m (open space) | 6×5 | ~1.3×1.2 m |

Regola pratica: **dimensione cella ≥ 1 m**. Sotto 1 m la CSI non discrimina celle adiacenti in modo affidabile (e il LOO-cell lo smaschererà).
