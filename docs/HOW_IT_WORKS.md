# Come funziona il sistema — guida per chi parte da zero

> Questa è la doc da leggere **prima** del quickstart. Spiega:
> cosa fa il sistema, cosa usiamo *al posto* del machine learning,
> qual è la differenza tra **calibrazione** (sempre necessaria) e
> **training** (opzionale, probabilmente non ti serve), e come
> debuggare quando qualcosa non parte.

---

## § 1 · Cosa fa il sistema, in 30 secondi

Tre ESP32 si pingano a vicenda su WiFi canale 6 fisso. Ogni ESP32, quando *riceve* un ping, registra il Channel State Information (CSI) — un'impronta digitale del canale radio in quell'istante. Quando una persona entra nella stanza, l'impronta cambia. Il PC raccoglie i CSI via UDP e li passa a 3 pipeline parallele:

```
   3× ESP32        UDP :5005          WS :8765           Browser
┌───────────┐ ──▶ ┌──────────┐ ─────▶ ┌──────────┐ ────▶ ┌──────────┐
│ radar3d   │     │ presence │        │ ws_server │      │ ui.html  │
│  cross-   │     │ blob_live│        │ multiplex │      │  canvas+ │
│   ping    │     │ blob3d   │        │           │      │   3D     │
└───────────┘     └──────────┘        └──────────┘      └──────────┘
```

- **`presence`** → "stanza vuota / persona ferma / persona in movimento"
- **`blob_live`** → "in quale quadrante della stanza è la persona" (mappa 2D)
- **`blob3d`** → posizione (x, y, z) approssimativa con altezza macro-classe

Niente cloud, niente camere, niente wearable. Tutto gira sul tuo PC in locale.

---

## § 2 · Cosa usiamo al posto del Machine Learning

**Domanda diretta dell'utente: "cosa usiamo al posto di ML?"**

Risposta breve: **un centroide pesato per varianza**. Niente modelli, niente training, niente file `.joblib` da generare. Il default funziona "out of the box".

### Come funziona, in concreto

Implementato in [`csi/quadrants/blob_live.py`](../csi/quadrants/blob_live.py), classe `BlobEstimator`.

1. Per ogni ricevitore RX0, RX1, RX2 (e ogni percorso TX→RX), tengo in memoria gli ultimi ~100 frame di CSI (≈ 1 secondo a 100 Hz).
2. Per ogni RX, calcolo **quanto la sua "vista" del canale sta variando** in quella finestra → numero `var[RX_i]`.
   - Se la persona è vicina a RX0, RX0 "vede" molto più varianza degli altri.
3. La posizione stimata è il **baricentro delle posizioni RX, pesato dalle varianze**:
   ```
   posizione_persona = Σ(pos[RX_i] * var[RX_i]) / Σ(var[RX_i])
   ```
4. Da (x, y) → distribuzione Gaussiana sulla griglia → cella più probabile.

### Perché funziona senza training

Non c'è nessun modello da allenare: la matematica è la stessa per qualunque stanza, qualunque persona. Cambia solo il "dove sono i miei RX" (configurato via flag `--rx`).

### Limiti onesti

- **Accuratezza**: ~0.5–1.5 m con 3 RX in stanza 6×5 m. Sufficiente per "in quale quadrante", non per "in che cm preciso".
- **Ambiguo se persona al centro geometrico dei 3 RX**: massima incertezza, blob "indeciso".
- **Se varianza < soglia minima**: il sistema non emette stima ("stanza vuota o persona troppo lontana").

### Perché abbiamo scelto no-ML come default

Nel branch master usavamo un classificatore Random Forest (`CSIClassifier.train_custom`) che mappava CSI → cella della griglia (`r0c0`, `r0c1`, ...). Risultato: **overfitting strutturale** — il modello imparava a votare sempre la stessa cella. Il centroide pesato per varianza **non può overfittare** perché non c'è training.

### C'è anche un percorso ML opt-in

Per chi vuole più precisione, esiste [`csi/quadrants/regressor.py`](../csi/quadrants/regressor.py) — `PositionRegressor` (Random Forest che regredisce coordinate continue + Kalman smoothing). **Ma è opzionale e probabilmente non ti serve.** Vedi § 4 quando potrebbe servirti.

---

## § 3 · Calibrazione vs Training — sono cose diverse

Il punto di confusione più comune. **Sono due cose distinte, con scopi diversi.**

| | **Baseline calibration** | **ML training** |
|---|---|---|
| A cosa serve | Imparare il "rumore di fondo" della stanza vuota | Imparare a mappare CSI → cella della griglia |
| Quando | **Sempre, automatica** (primi 30 s dopo avvio) | **Solo se** vuoi `--quadrants-mode regressor` (opt-in) |
| Cosa devi fare tu | Stare fuori dalla stanza i primi 30 s dopo aver lanciato il server | Raccogliere 30–60 s di CSI etichettato per ogni cella, una alla volta |
| Cosa impara | 1 numero: la soglia oltre cui c'è "movimento" | RandomForestRegressor + Kalman 2D |
| Dove vive | `PresenceDetector._finalize_calibration` in `csi/presence/detector.py` | `PositionRegressor.train` + `cross_validate_loo_cell` in `csi/quadrants/regressor.py` |

### Quindi: per il tuo quickstart cosa devi fare?

> **SOLO la baseline calibration. Niente training.**
>
> Il server la fa **automaticamente** nei primi 30 s di esecuzione. Tu devi solo:
> 1. Lanciare il server
> 2. Stare fuori dalla stanza per 30 s
> 3. Nella UI vedi il badge "calibrazione: 30 %, 60 %, 100 %" → quando arriva a 100 %, il sistema è pronto.

Nessun file da generare. Nessun dataset da raccogliere. Nessun `.joblib`.

---

## § 4 · Quando *potrebbe* servire il training ML (probabilmente mai)

Se in futuro volessi quadranti più precisi del centroide no-ML, potresti allenare il `PositionRegressor`. La procedura sarebbe:

1. Stai fermo in cella `r0c0` per 30–60 s mentre logghi i frame CSI.
2. Ripeti per ogni cella della griglia (es. 16 celle per una 4×4).
3. Chiama `PositionRegressor.train(labeled_frames)`.
4. Chiama `cross_validate_loo_cell(labeled_frames)` — questo è il **gate anti-overfitting**: tira fuori una cella alla volta, addestra sulle altre, e verifica se sa interpolare la cella esclusa.
5. **Se il LOO-cell rifiuta il modello** (MAE > 0.20 normalizzato, oppure R² < 0): il modello sta overfittando. **Non lo usi.** Torni al baseline no-ML (`blob_live`).

Tutto questo è il "gate anti-problema-3" descritto in [`plan.md`](../plan.md).

**Per la fase attuale — capire se il sistema funziona, fare la demo:** salta tutto. Il no-ML è sufficiente.

---

## § 5 · Quickstart verificato (con il fix che mancava al README)

```bash
# 0) DIPENDENZE — il README ometteva questo step, ed era la causa
#    del "non funziona": il server crashava perché websockets
#    non era installato.
pip install websockets numpy scikit-learn joblib

# 1) Lancia il server (no hardware, default no-ML, 3D abilitato)
PYTHONPATH=. python3 -m csi.quadrants.ws_server \
    --udp-port 5005 --ws-port 8765 \
    --room 6x5 --grid 4x4 \
    --rx "0.5,0.5;5.5,0.5;3.0,4.5" \
    --enable-3d --room-height 3.0

# Cosa devi vedere a terminale (entro 1 secondo):
#   [ws] UDP in ascolto su :5005
#   [ws] WebSocket server su ws://0.0.0.0:8765
#   [ws] Blob3DTracker abilitato (z best-effort, macro-classi)
#
# Se invece vedi:
#   ERROR: 'websockets' non installato
# → torna al passo 0 e installa le dipendenze.

# 2) In un SECONDO terminale, lancia il simulatore
#    (finge che ci siano 3 ESP32 reali con una persona in movimento)
PYTHONPATH=. python3 experimental/inject_radar3d_frames.py --port 5005 --moving

# Adesso nel terminale del server (quello del punto 1) dovresti vedere:
#   [ws]  98.3 fps  paths= 9  state=EMPTY  blob=(3.0,2.5)
#                            ↑              ↑
#               tutti e 9 i percorsi      stato (in EMPTY per i primi 30s
#               TX→RX attivi              di calibrazione, poi MOVEMENT)

# 3) Apri la UI nel browser
open mapping/ui.html      # macOS
xdg-open mapping/ui.html  # Linux
# oppure: doppio click sul file dal Finder/Esplora Risorse

# Cosa devi vedere nella UI:
#   - In alto a destra: pallino VERDE + "connesso → ws://localhost:8765"
#   - Sidebar: badge "calibrazione" che sale 0% → 100% in 30 secondi
#   - Dopo i 30s: badge presence diventa "MOVEMENT" (rosso pulsante)
#   - Canvas centrale: pallino GIALLO (il blob) che si muove
#   - Pannello destro 3D: stanza wireframe + ellissoide giallo
```

### Versione "all-in-one" con script

Esiste anche uno script che fa tutto in automatico:

```bash
./experimental/run_local.sh
```

Lancia server + simulatore + apre il browser. Premi Ctrl-C per fermare tutto.

---

## § 5b · Quando hai hardware reale: diagnostica prima di tutto

Se hai i 3 ESP32 fisici, **prima** di lanciare ws_server fai un check di sanità:

```bash
PYTHONPATH=. python3 experimental/diag_paths.py --seconds 10
```

Questo tool ascolta i frame UDP per 10 secondi e stampa una **tabella 3×3** di chi sta parlando con chi:

```
┌─────┬────────────────┬────────────────┬────────────────┐
│     │   TX0          │   TX1          │   TX2          │
├─────┼────────────────┼────────────────┼────────────────┤
│ RX0 │  98.2 fps      │  ---           │  97.8 fps      │  ← TX1 invisibile!
│     │ var=12.4       │                │ var=10.1       │
│ RX1 │  99.5 fps      │  ---           │  98.9 fps      │
│     │ var=9.2        │                │ var=8.5        │
│ RX2 │  98.7 fps      │  ---           │  99.2 fps      │
│     │ var=7.6        │                │ var=12.9       │
└─────┴────────────────┴────────────────┴────────────────┘
6 paths attivi · ⚠ TX1 NON VISIBILE
```

Cosa significa:
- **9/9 cells piene** → setup hardware sano, vai al ws_server.
- **Manca una colonna TXn** → l'ESP32 con `NODE_ID=n` non viene riconosciuto come trasmettitore. **Causa più probabile**: il MAC reale di quell'ESP32 è diverso da `NODE_MACS[n]` in `network_config.h`. **Fix**: apri il Serial Monitor di quell'ESP32, leggi il MAC stampato a boot, aggiornalo nel firmware, ri-flasha.
- **Manca una riga RXm** → l'ESP32 con `NODE_ID=m` non sta inviando UDP al PC. **Cause**: non si è connesso al WiFi, oppure ha `UDP_TARGET_IP` sbagliato.
- **Tutte le varianze ~0** → CSI bloccato. Verifica che il Serial Monitor stampi `CSI:enabled` e che il contatore `CSI:` salga.
- **Tutte le varianze ~uguali** → diversità spaziale bassa. Soluzione nel ws_server: `--variance-power 2.5` per amplificare le differenze (vedi § 6).

---

## § 6 · Diagnostica — i sintomi più comuni

| # | Cosa vedi | Cosa significa | Cosa fai |
|---|---|---|---|
| 1 | Server termina subito, terminale torna al prompt | `websockets` non installato (causa #1 di problemi) | `pip install websockets` |
| 2 | UI mostra "disconnesso. retry in 2s…" (rosso) | Il server non è in esecuzione, o WS porta sbagliata | Controlla che il terminale 1 (server) sia ancora vivo e mostri "WebSocket server su :8765" |
| 3 | UI connessa, ma stato resta "UNKNOWN" e la barra calibrazione non sale | Il simulatore non sta inviando frame UDP (o porta sbagliata) | Verifica che il simulatore usi `--port 5005` (uguale a `--udp-port` del server) |
| 4 | UI mostra "EMPTY" forever, blob non appare | Hai lanciato il simulatore senza `--moving` (sta inviando frame statici, varianza = 0) | Rilancia con `python3 experimental/inject_radar3d_frames.py --port 5005 --moving` |
| 5 | Nel campo `diag` vedi `paths_active=0` | Frame UDP non arrivano al server (firewall? porta sbagliata?) | Test rapido: `nc -lu -p 5005` → se nemmeno questo riceve nulla, è firewall o porta. Se riceve → problema nel parser dei frame |
| 6 | `paths_active=6` (su 9 attesi) o `7` o `8` | Uno dei 3 nodi non viene riconosciuto come TX (MAC sbagliato) o non sta ricevendo (UDP non parte) | Lancia `experimental/diag_paths.py` per vedere quale TX/RX manca. Cause + fix in § 5b. |
| 7 | Blob fermo al centro geometrico dei RX (es. (3.0, 1.7) con RX a (0.5,0.5)/(5.5,0.5)/(3.0,4.5)) | Le varianze dei 3 RX sono ~uguali → il centroide pesato collassa al centro. Spesso conseguenza del sintomo #6 OPPURE di bassa diversità spaziale | (a) Risolvi #6 prima. (b) Rilancia con `--variance-power 2.5` (amplifica le differenze). (c) Considera `--blob-baseline-seconds 20` per sottrarre il rumore di fondo per-RX. (d) Allontana fisicamente i 3 ESP32. |
| 8 | `state=EMPTY` anche se ti muovi nella stanza | La calibrazione baseline ha imparato una soglia troppo alta (es. perché ti sei mosso durante i primi 30 s, o perché il CSI ha rumore di fondo notevole). | Rilancia con `--move-mult 2.0` (default 3.0) per essere più sensibile. Se non basta: `--move-mult 1.5`. In alternativa, ricalibra con stanza davvero vuota. |
| 9 | `fps` esagerati (es. 3000+) nel log del server | (Risolto: era un bug nella misura, ora usa contatore 1s.) | Aggiorna alla versione corrente del branch. |

### Comandi di verifica rapida

```bash
# Verifica che websockets sia installato
python3 -c "import websockets; print(websockets.__version__)"

# Verifica che frame UDP arrivino sulla porta 5005
nc -lu -p 5005     # lascia in ascolto, poi lancia il simulatore in un altro terminale

# Verifica che il server WebSocket sia raggiungibile
python3 -c "import asyncio, websockets; \
  asyncio.run((lambda: websockets.connect('ws://localhost:8765').__aenter__())())"

# ⭐ Il tool più importante per debuggare hardware: tabella 3×3 dei percorsi
PYTHONPATH=. python3 experimental/diag_paths.py --seconds 10
```

### Tuning del ws_server per hardware reale

Se dopo `diag_paths.py` vedi 9/9 percorsi ma il blob fa cose strane:

```bash
# Più sensibile al movimento (default move_mult=3.0)
PYTHONPATH=. python3 -m csi.quadrants.ws_server --move-mult 2.0 ...

# Più sensibile alle differenze tra RX (anti "blob fermo al centro")
PYTHONPATH=. python3 -m csi.quadrants.ws_server --variance-power 2.5 ...

# Sottrai automaticamente il rumore di fondo per-RX (calibra 20s)
PYTHONPATH=. python3 -m csi.quadrants.ws_server --blob-baseline-seconds 20 ...

# Finestra più lunga = più smoothing, meno reattività
PYTHONPATH=. python3 -m csi.quadrants.ws_server --window 200 ...

# Tutti insieme (combo "robusto per hardware reale rumoroso"):
PYTHONPATH=. python3 -m csi.quadrants.ws_server \
    --move-mult 2.0 --variance-power 2.5 --blob-baseline-seconds 20 \
    --rx "0.5,0.5;5.5,0.5;3.0,4.5" --enable-3d
```

Per casi più rari, vedi [`docs/TROUBLESHOOTING.md`](TROUBLESHOOTING.md) (10 sintomi estesi).

---

## § 7 · "Voglio capire un componente specifico"

Mini-mappa dei file per chi vuole leggere il codice.

| Componente | File | Test |
|---|---|---|
| Presence detection (EMPTY/STAT/MOVE) | [`csi/presence/detector.py`](../csi/presence/detector.py) | [`tests/test_presence.py`](../tests/test_presence.py) |
| Dashboard CLI presence | [`csi/presence/monitor_cli.py`](../csi/presence/monitor_cli.py) | (smoke nel test sopra) |
| Quadranti no-ML (default) | [`csi/quadrants/blob_live.py`](../csi/quadrants/blob_live.py) | [`tests/test_quadrants.py`](../tests/test_quadrants.py) |
| Quadranti ML opt-in + LOO-cell gate | [`csi/quadrants/regressor.py`](../csi/quadrants/regressor.py) (cerca `cross_validate_loo_cell`) | [`tests/test_quadrants.py`](../tests/test_quadrants.py) |
| Server WebSocket multiplex | [`csi/quadrants/ws_server.py`](../csi/quadrants/ws_server.py) | smoke |
| Blob 3D + altezza | [`csi/blob3d/tracker.py`](../csi/blob3d/tracker.py) | [`tests/test_blob3d.py`](../tests/test_blob3d.py) |
| Parser CSI (radar3d + ADR-018 + testo) | [`csi/csi_processor.py`](../csi/csi_processor.py) | [`tests/test_csi_processor.py`](../tests/test_csi_processor.py) |
| Frontend canvas 2D + Three.js 3D | [`mapping/ui.html`](../mapping/ui.html) | manuale |
| Firmware ESP32 (cross-ping) | [`firmware/esp32_radar3d/esp32_radar3d.ino`](../firmware/esp32_radar3d/esp32_radar3d.ino) | hardware |
| Simulatore UDP per CI/dev | [`experimental/inject_radar3d_frames.py`](../experimental/inject_radar3d_frames.py) | usato dai test integration |

### Suite di test (194/194 ✓)

```bash
for t in tests/test_*.py; do
    [ "$t" = "tests/test_csi_tools.py" ] && continue  # serve pyserial
    PYTHONPATH=. python3 "$t" 2>&1 | tail -1
done
```

Tutti i test sono **standalone** (no pytest), girano con dati sintetici, **zero hardware** richiesto.

---

## § 8 · Riassunto delle 3 domande dell'utente

> **"Cosa usiamo al posto di ML?"**
> Un centroide pesato per varianza CSI per RX (file `csi/quadrants/blob_live.py`). Niente modelli, niente training. Spiegato in § 2.

> **"Dobbiamo trainare lo spazio vuoto?"**
> No: il sistema fa una **calibrazione automatica** di 30 s al lancio (durante i quali devi stare fuori stanza). Non è "training", è solo "impara il livello di rumore". Spiegato in § 3.

> **"Come si esegue il tutto?"**
> 3 step: (0) `pip install websockets numpy scikit-learn joblib`, (1) lancia il server, (2) lancia il simulatore, (3) apri la UI. Spiegato in § 5. Se non funziona → § 6.
