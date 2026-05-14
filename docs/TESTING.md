# Testing — Feasibility Validation

## Come eseguire i test

### 1. Carica lo sketch MCU

1. Apri `feasibility_test.ino` nell'Arduino IDE
2. Seleziona board: **Arduino UNO Q**
3. Carica sulla board via USB-C
4. Tieni la board collegata al PC

### 2. Esegui il test Python

Sulla board UNO Q (o via SSH):

```bash
# Trasferisci lo script sulla board
scp feasibility_test.py arduino:/home/arduino/

# Esegui
python3 feasibility_test.py
```

Oppure su PC se la board e montata come dispositivo:

```bash
python3 feasibility_test.py
```

### 3. Leggi il report

Il test genera un file JSON:

```bash
cat feasibility_report_*.json | python3 -m json.tool
```

---

## Cosa verifica ogni test

| Test | Cosa misura | Soglia di fallimento |
|------|-------------|----------------------|
| **RSSI Sampling** | Frequenza e affidabilita del campionamento WiFi | <50% campioni attesi |
| **Feature Extraction** | Velocita di calcolo media/std/delta | >10ms per estrazione |
| **UART Communication** | Comunicazione seriale MCU <-> Linux | <3 righe in 10s |
| **System Load** | CPU e RAM durante sensing | Crash o overload |
| **Presence Detection** | Capacita di distinguere vuoto/movimento | Falso positivo > soglia |
| **Combined Pipeline** | Tutto insieme per 30s senza crash | >20% errori o <10 loop |

---

## Interpretazione dei risultati

### FULL PASS
Tutti i test passano -> il progetto e fattibile sulla UNO Q.

### Qualche FAIL

| Test fallito | Impatto | Cosa fare |
|-------------|---------|-----------|
| RSSI Sampling | Grave — presenza non rilevabile | Verifica driver WiFi, prova `iw dev wlan0 scan` |
| Feature Extraction | Medio — ridurre frequenza campionamento | Usa statistiche pure Python senza numpy |
| UART Communication | Grave — sensori non leggibili | Verifica cablaggio, baud rate, porta seriale |
| System Load | Medio — ottimizzare | Ridurre finestra temporale o frequenza |
| Presence Detection | Medio — regolare soglie | Calibra STD_THRESHOLD sull'ambiente |
| Combined Pipeline | Critico — sistema instabile | Debug singoli componenti prima di integrare |

---

## Test aggiuntivi manuali

### Durata batteria (se applicabile)
```bash
# Monitora consumo energia
watch -n 5 'cat /sys/class/power_supply/*/voltage_now'
```

### Stabilita WiFi a lungo termine
```bash
# Lascia il test eseguire per 1h
python3 -c "
from feasibility_test import test_rssi_sampling
for i in range(12):
    print(f'--- Round {i+1}/12 ---')
    test_rssi_sampling()
"
```

### Calibrazione presence detection
```bash
# Calibrazione rapida (5 minuti totali)
python3 calibrate_presence.py --mode quick

# Monitoraggio real-time
python3 calibrate_presence.py --mode monitor
```
