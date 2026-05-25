# 0002 — Merge con il repo team `nutriandrea/IF_I_WIFI`

**Date:** 2026-05-24
**Status:** Accepted

## Contesto

IF I WIFI è nato come estrazione minimale embedded-first da RuView (vedi
[ADR-0001](0001-extracted-from-ruview.md)). Pochi giorni dopo abbiamo
realizzato che un repo team parallelo, `nutriandrea/IF_I_WIFI`, esisteva
già con:

- Cross-ping firmware ESP32 (3 nodi, MAC stabili) — soluzione originale
  non presente in RuView, particolarmente utile per il nostro setup a 3
  ESP32.
- Pipeline Python end-to-end funzionante: presence detection,
  position tracking (Kalman 2D + RF regressor), vital signs (breathing +
  heart rate), Doppler, multi-AP triangulation, Home Assistant MQTT bridge.
- Frontend web Three.js + heatmap che parla WebSocket col backend.
- Test suite con 123 test, 9 file, ~3.2k LOC.
- 75 commit, 4 contributor (Pierluca incluso).

L'estrazione embedded-first dei pochi giorni precedenti aveva prodotto:

- Firmware ESP-IDF C più robusto del .ino team (rate gate hardware, ENOMEM
  backoff, anti-corruption NVS).
- Workspace Rust pulito (~1k LOC) per il path embedded futuro su Arduino UNO Q.
- Documentazione esplicita di cosa è stato deliberatamente lasciato fuori
  da RuView (claim biomedici non validati, ecc.).

I due repo non sono in conflitto, sono complementari.

## Decisione

Fare il merge nel repo `IF-I-WIFI` (questo), non viceversa, perché:

- Questo è il nuovo repo da cui partire come main del progetto.
- L'utente è l'unico autore qui, contributor del team là.
- I file team possono essere assorbiti senza perdita di funzionalità.

Struttura risultante:

```
IF-I-WIFI/
├── firmware/
│   ├── esp32-radar3d/        (da team — cross-ping 3-node, Arduino .ino)
│   └── esp32-csi-node/       (da RuView extraction — single-source S3, ESP-IDF C)
│
├── host-python/              (da team — pipeline operativa)
│   ├── csi/
│   ├── mapping/              (HTML asset legacy, in via di migrazione)
│   ├── experimental/
│   └── tests/
│
├── host-rust/                (da RuView extraction — embedded-future)
│   └── crates/{ifi-core, ifi-dsp, ifi-transport, ifi-cli}
│
├── visualization/web-minimal/
│   └── ui.html               (da team mapping/ui.html)
│
├── docs/
│   ├── architecture.md       (RuView extraction)
│   ├── csi-frame-format.md   (RuView extraction)
│   ├── hardware-support.md   (RuView extraction)
│   ├── what-was-cut.md       (RuView extraction)
│   ├── decisions/            (questo ADR + 0001)
│   └── from-team/            (tutta la documentazione team)
│
└── experiments/              (placeholder per lavori speculative)
```

## Cosa entra da dove (mapping esplicito)

### Da `nutriandrea/IF_I_WIFI` (team)

| Sorgente | Destinazione | Modifiche |
| --- | --- | --- |
| `csi/` (intero pacchetto, ~16k LOC) | `host-python/csi/` | Verbatim |
| `mapping/` | `host-python/mapping/` | Verbatim (asset legacy, ui.html duplicato in visualization/) |
| `experimental/` | `host-python/experimental/` | Verbatim |
| `tests/` (9 file, 123 test) | `host-python/tests/` | Verbatim |
| `firmware/esp32_radar3d/esp32_radar3d.ino` | `firmware/esp32-radar3d/esp32-radar3d.ino` | + rate gate 100 Hz, + UDP ENOMEM backoff, + stats estese, brownout detector riabilitato |
| `firmware/esp32_radar3d/network_config.h` | `firmware/esp32-radar3d/network_config.h` | Verbatim (contiene MAC del setup attuale) |
| `firmware/esp32_radar3d/FLASHING.md` | `firmware/esp32-radar3d/FLASHING.md` | Verbatim |
| `mapping/ui.html` | `visualization/web-minimal/ui.html` | Verbatim (copia, l'originale resta in mapping/) |
| `docs/*.md` (9 file) | `docs/from-team/*.md` | Verbatim |

Aggiunte rispetto al sorgente team (non esistevano prima):

- `host-python/pyproject.toml` — deps versionate (numpy, scipy, sklearn,
  websockets, pyserial; extras mqtt/plot/unoq/dev).
- `host-python/README.md` — provenance + comandi di lancio.
- `firmware/esp32-radar3d/README.md` — descrizione + diff vs sorgente team.
- `firmware/esp32-radar3d/secrets.h.example` — template per il file ignored.

### Da RuView extraction (lavoro precedente, già in `IF-I-WIFI` pre-merge)

Tutto preservato:

- `firmware/esp32-csi-node/` (ESP-IDF, magic 0xC5110001).
- `host-rust/` (rinominato da `host/`).
- `docs/{architecture,csi-frame-format,hardware-support,what-was-cut}.md`.
- `docs/decisions/0001-extracted-from-ruview.md`.

## Conseguenze

### Bene

- C'è una pipeline operativa OGGI (Python + cross-ping firmware) — non
  serve aspettare il completamento del path Rust.
- Esiste un frontend web reale, non un placeholder.
- Il firmware più robusto (ESP-IDF) resta disponibile per chi vuole il path
  single-S3 con difese contro i bug di promiscuous-mode già visti upstream.
- Il path Rust embedded resta intatto come fondazione per quando porteremo
  l'host a Arduino UNO Q.
- La disciplina anti-fuffa di IF I WIFI (docs/what-was-cut.md, claim
  esplicitamente esclusi da RuView) si applica al merge.

### Costi

- Repo più grande: ~22k LOC totali (16k Python + 1k Rust + 750 C team + 750
  C ESP-IDF + docs). Resta gestibile, ma non è più "leggibile in un
  pomeriggio".
- Due firmware paralleli: vanno mantenuti entrambi e i loro formati binari
  divergono (0xC5110001 vs 0xC5110003). La pipeline Python già parsa
  entrambi; il CLI Rust parsa solo 0xC5110001 oggi (TODO se serve).
- Due host stack (Python operativo, Rust scheletro): bisogna documentare
  bene quale si usa quando, altrimenti chi arriva si confonde.
- Git history dei singoli file team viene persa con la copia (i 75 commit
  del repo team non vengono importati). Mitigato dal fatto che il repo
  team resta vivo sul suo GitHub come riferimento storico.

### Non vere conseguenze (cose che NON cambiano)

- Le licenze: entrambi i sorgenti sono MIT OR Apache-2.0, niente attriti.
- Le esclusioni RuView (claim biomedici, "ghost hunter", quantum
  coherence, ecc.) restano fuori — il team stesso non li aveva
  importati e non li importiamo neanche noi.

## Verifica post-merge

In ordine di priorità, prima di considerare il merge "ok":

1. **Python pipeline gira come prima**:
   ```bash
   cd host-python
   python3 -m venv .venv && source .venv/bin/activate
   pip install -e ".[mqtt,plot,dev]"
   pytest                          # devono passare i 123 test team
   ```
   Se qualche test fallisce, è quasi certo che dipende da un import
   relativo / path che era hardcoded nel repo team. Da fixare.

2. **Firmware esp32-radar3d compila con le difese aggiunte**:
   - Aprire il `.ino` in Arduino IDE 2.x.
   - Build per ESP32 Dev Module.
   - Cercare warnings nuovi su `s_last_process_us`, `s_udp_backoff_until_us`,
     `csi_dropped_rate`, `udp_send_fail`.
   - Se compila, flash su una scheda e verificare che il log seriale
     mostri ancora `[N] TX:... CSI:... rate_drop:... udp_fail:...`.

3. **Workspace Rust ancora compila** (dopo `git mv host host-rust`):
   ```bash
   cd host-rust
   cargo test --workspace
   ```
   Nessun motivo perché non lo faccia, ma vale la pena verificare.

4. **UI web carica con dati live**:
   ```bash
   cd host-python && source .venv/bin/activate
   python3 -m csi.quadrants.ws_server --udp-port 5005 --ws-port 8765
   # in altra shell:
   python3 -m http.server -d ../visualization/web-minimal 8000
   # browser: http://localhost:8000/ui.html?ws=ws://localhost:8765
   ```

## Path di sviluppo dopo il merge

Priorità ordinate:

1. **Stabilizzare il merge**: i 4 punti di verifica sopra.
2. **Test post-merge**: lanciare la pipeline team su un setup nuovo
   (re-flash 3 ESP32 con il firmware modificato, verificare che le
   difese aggiunte non rompano nulla, che le metriche `rate_drop` e
   `udp_fail` siano basse sul nostro setup).
3. **Splittare i file monolitici** in `host-python/`: `csi_mac.py` 1034
   LOC, `csi_processor.py` 1029 LOC, `quadrants/regressor.py` 827 LOC.
   Pattern da seguire: quello già usato dal team su `csi_ml.py` ("split
   monolith into 7 modules").
4. **Migrare** `mapping/` dentro `visualization/` per consolidare gli
   asset web e rimuovere la duplicazione `mapping/ui.html` ↔
   `visualization/web-minimal/ui.html`.
5. **Parità Rust 0xC5110003**: oggi `host-rust/ifi-core` decode solo
   ADR-018 puro (magic 0xC5110001). Estendere per supportare anche il
   formato esteso radar3d (header 24 byte) così l'`ifiwifi-capture` CLI
   funziona anche con il firmware del team.
6. **Solo dopo**: iniziare il porting Rust no-std verso UNO Q.

## Riferimenti

- ADR-0001 (estrazione da RuView): [`0001-extracted-from-ruview.md`](0001-extracted-from-ruview.md)
- Repo team upstream: https://github.com/nutriandrea/IF_I_WIFI
- Audit anti-fuffa RuView: [`../what-was-cut.md`](../what-was-cut.md)
