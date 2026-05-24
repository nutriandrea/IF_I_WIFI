# Troubleshooting — WiFi Sensing pipeline

I 10 errori più comuni e cosa fare. Se sei bloccato, parti da qui.

---

## 1. ESP32 non si connette al WiFi (`WiFi FAIL — reboot`)

**Sintomi**: ESP32 stampa `WiFi connecting....` in loop, poi `WiFi FAIL — reboot` ogni 20 secondi.

**Cause + fix**:
- **Hotspot in 5 GHz**: l'ESP32 classico/S3 è solo 2.4 GHz. Forza la banda 2.4 GHz dell'hotspot (iPhone: "Massimizza compatibilità"; Android: scegli "banda 2.4 GHz" nelle impostazioni hotspot).
- **SSID/PASS sbagliata** in `secrets.h`: ricontrolla maiuscole/minuscole/spazi finali.
- **Caratteri speciali** nel pass (es. `$`): mettila in `\"...\"` o usa una pass solo alfanumerica per test.
- **TX power troppo bassa** + AP lontano: ridotta a 8.5 dBm nel firmware base (`WiFi.setTxPower(WIFI_POWER_8_5dBm)`). Rimuovi per portarla al default 19.5 dBm.

---

## 2. ESP32 si resetta in loop (brownout)

**Sintomi**: messaggio `Brownout detector was triggered` o reset ogni pochi secondi.

**Fix**:
- Hub USB non alimentato → alimenta direttamente dal PC o da un hub powered.
- Cavo USB scarso (charge-only) → usa un cavo dati.
- Disabilitato già nel firmware (`WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0)`), ma se il VCC è davvero sotto 2.7V non basta — serve alimentazione migliore.

---

## 3. PC non riceve frame UDP (`paths=0`)

**Sintomi**: il server stampa `paths_active=0` e niente blob.

**Diagnostica**:
1. Verifica che gli ESP32 e il PC siano sulla **stessa rete WiFi**. Routing inter-VLAN non funziona by-default.
2. Sul PC: `nc -lu -p 5005` (Linux/Mac) per vedere se arriva qualcosa raw.
3. Sull'ESP32: nel Serial Monitor controlla `WiFi:OK,<ip>` e `CSI attivo`. L'IP dell'ESP32 deve essere nella stessa subnet del PC.
4. Firewall PC: temporaneamente disabilita per testare. Mac: `Settings → Network → Firewall → Off`.

---

## 4. Server vede solo `paths=3` invece di 9

**Sintomi**: il server dice `paths_active=3` o `=6` invece di 9.

**Causa**: uno o due ESP32 non sono attivi, o stanno trasmettendo ma su un canale diverso.

**Fix**:
- Verifica che **tutti e 3** stiano stampando `[N] TX:xxxx CSI:yyyy` ogni 5 secondi nel loro Serial.
- Verifica che siano **sullo stesso canale** (default 6, hardcoded nel firmware). Tutti devono associarsi allo **stesso AP** (lo SSID in `secrets.h`).
- Aspetta 10 secondi dopo l'avvio — il primo ESP32 a partire potrebbe aver bisogno che gli altri siano già online.

---

## 5. `state=EMPTY` anche quando la stanza è occupata

**Sintomi**: PresenceDetector resta su EMPTY anche se ti muovi.

**Cause + fix**:
- **Soglie sbagliate**: il default `--empty-mult 1.5 --move-mult 4.0` può essere troppo conservativo. Prova `--empty-mult 1.2 --move-mult 2.5`.
- **Calibrazione contaminata**: se ti sei mosso durante i primi 30 s, il baseline è gonfio. Premi `c` nel monitor CLI (o riavvia il server) e calibra in stanza VUOTA.
- **Tutti i percorsi hanno varianza nulla**: probabile che il CSI sia bloccato — verifica con `python3 -m csi.csi_record --info` su una cattura.

---

## 6. `state=MOVEMENT` perpetuo

**Sintomi**: il sistema dichiara MOVEMENT anche a stanza vuota.

**Cause + fix**:
- **Soglie troppo basse**: prova `--move-mult 6.0`.
- **Rumore di ambiente** (altri WiFi, motori, ventole): il CSI riflette anche perturbazioni non umane. Sposta uno dei nodi in un'altra posizione e ricalibra.
- **Calibrazione baseline troppo corta**: usa `--baseline-seconds 60` per un baseline più solido.
- **EMA troppo reattivo**: cambia `--window 200` (raddoppia la finestra a ~2 s).

---

## 7. Cella predetta è sempre la stessa (overfitting)

**Sintomi**: il quadrante predetto resta su `r0c0` (o un'altra cella fissa) qualunque cosa tu faccia.

**Diagnosi**:
1. Se stai usando `--quadrants-mode regressor`: vedi `Modello regressor non validato` in console? Il loader sta rifiutando un modello LOO-cell-failed (giusto così — risolve l'overfitting).
2. Se stai usando `blob_live` (default) e il blob è bloccato: è probabile che la varianza sia dominata da un solo RX (gli altri 2 trasmettono ma il loro CSI è scarso).

**Fix**:
- Verifica `by_rx` nei messaggi `diag`: tutti i RX dovrebbero avere ratio simile di frame ricevuti. Se RX1 ha 90% e RX0/RX2 hanno 5% a testa → quasi tutto il segnale viene da un solo nodo.
- Sposta gli ESP32 per migliorare la diversità.

---

## 8. Three.js non carica (scena 3D nera)

**Sintomi**: la sezione 3D nella UI è nera o non si vede.

**Cause + fix**:
- **Offline**: Three.js è caricato da CDN (`cdn.jsdelivr.net`). Senza internet, è disabilitato (vedi console warning). Per uso offline scarica `three.min.js` e modifica `ui.html` per puntare al locale.
- **CSP del browser**: alcuni browser bloccano CDN da file:// URLs. Servi la UI con un piccolo HTTP server: `python3 -m http.server 8080` poi apri `http://localhost:8080/mapping/ui.html?ws=ws://localhost:8765`.

---

## 9. `pyserial`/`websockets` non installati

**Sintomi**: `ModuleNotFoundError: No module named 'websockets'` o simile.

**Fix**:
```bash
pip install websockets numpy scikit-learn joblib msgpack pyserial
```

Pacchetti per funzionalità:
- `numpy` + `scikit-learn` + `joblib` → ML (regressor, Kalman)
- `websockets` → `ws_server.py`
- `pyserial` → `csi_mac.py`, `csi_record.py`, `tools/discover_macs.py`
- `msgpack` → solo se usi `csi_processor.py` con `RouterClient` (UNO Q bridge)

---

## 10. Test sintetici falliscono dopo modifiche

**Sintomi**: dopo aver toccato il codice, `tests/test_*.py` falliscono.

**Fix**:
```bash
# Esegui tutta la suite in sequenza
for t in tests/test_*.py; do
    [ "$t" = "tests/test_csi_tools.py" ] && continue   # richiede pyserial
    PYTHONPATH=. python3 "$t" || { echo "FAIL: $t"; break; }
done
```

I test usano dati sintetici deterministici (`random.seed(...)`), quindi se fallisce è perché hai cambiato la logica. Leggi il messaggio `[FAIL]` per capire dove.

I test rilevanti per ogni modulo:
- `csi/presence/` → `tests/test_presence.py`
- `csi/quadrants/` → `tests/test_quadrants.py`
- `csi/blob3d/` → `tests/test_blob3d.py`
- `csi/csi_processor.py` (parser) → `tests/test_csi_processor.py`

---

## Bonus: debug rapido con simulatore

Quando lo stack reale ha problemi e vuoi isolare se il problema è hardware o software:

```bash
# Terminale 1: server con --quiet
PYTHONPATH=. python3 -m csi.quadrants.ws_server --quiet

# Terminale 2: simula movimento da un punto fisso
PYTHONPATH=. python3 tools/inject_radar3d_frames.py --moving --port 5005

# Apri ui.html → se vedi il blob muoversi: la pipeline software è sana,
# il problema è nei dati reali (probabilmente hardware o configurazione).
```
