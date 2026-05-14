# Shopping List — Smart Environment Hub

## Priorita di acquisto

| Priorita | Cosa | Perche |
|----------|------|--------|
| **P1 - Obbligatorio** | DHT22, MQ135, LDR, breadboard, jumper, resistenze | Necessari per demo base |
| **P2 - Consigliato** | Relay module, LED | Per attuatori e feedback |
| **P3 - Opzionale** | PIR HC-SR501 | Solo se avanza budget |

---

## Sensori base (per Arduino UNO Q)

| Componente | Qty | Descrizione | Prezzo (~) | Dove comprare |
|------------|-----|-------------|------------|---------------|
| DHT22 (AM2302) | 1 | Temp + umidita (-40~80°C, 0-100% RH) | 5-8 € | Amazon / AliExpress |
| MQ135 | 1 | Qualita aria (CO2, VOC, NH3, NOx) | 5-10 € | Amazon / AliExpress |
| LDR (fotoresistenza) | 2 | Luminosita ambientale | 1-3 € (5 pz) | Amazon / AliExpress |
| Resistenza 10kΩ | 2 | Pull-down per LDR | Inclusa kit | Amazon |
| PIR HC-SR501 | 1 | Movimento (opzionale, backup presenza) | 3-5 € | Amazon / AliExpress |

**Totale sensori: ~15-20 €**

---

## Cablaggio & prototyping

| Componente | Qty | Descrizione | Prezzo (~) |
|------------|-----|-------------|------------|
| Breadboard 400 punti | 1 | Prototipazione sensori | 3-5 € |
| Jumper wires M-M | 20 | Connessioni breadboard | 4-6 € (kit) |
| Jumper wires M-F | 10 | Sensori -> breadboard | Incluso kit |
| Jumper wires F-F | 10 | Connessioni speciali | Incluso kit |
| Resistenze kit (220Ω, 10kΩ) | 1 | LED + LDR | 3-5 € |

**Totale cablaggio: ~10-15 €**

---

## Attuatori

| Componente | Qty | Descrizione | Prezzo (~) |
|------------|-----|-------------|------------|
| Relay module 1-canale 5V | 1 | Commutazione lampadina/ventilatore | 3-6 € |
| LED (rossi + verdi) | 5 + 5 | Stato sistema / feedback | 1-2 € |
| Resistenza 220Ω | 2 | Protezione LED | Inclusa kit |

**Totale attuatori: ~5-8 €**

---

## Strumenti (se non li hai gia)

| Strumento | Necessario per |
|-----------|---------------|
| Cavetto USB-C -> USB-A | Collegare UNO Q al PC |
| Cacciavite piccolo | Morsetti relay |
| Pinza spelafili (opzionale) | Tagliare jumper su misura |

---

## Kit consigliato (tutto incluso)

| Kit | Include | Prezzo (~) |
|-----|---------|------------|
| **Elegoo Starter Kit** | DHT22, relay, LED, breadboard, jumper, resistenze | 35-45 € |
| **ELEGOO UNO Project Super Starter Kit** | Identifica sensori compatibili + manuale | 45-55 € |

---

## Arduino UNO Q

- **Fornito dalla prof** (consegna via email)
- **Nessun acquisto necessario**
- **WiFi integrato** (WiFiNINA / WiFi101)
- Documentazione: [Arduino UNO Q docs](https://docs.arduino.cc/hardware/uno-q)

---

## Note importanti

- **Saldatura**: se vuoi saldare, contatta **Susanna Bardini** (susanna.bardini@polimi.it)
- **Sensori conformi** al corso (senza materiali pericolosi)
- **Connettivita**: UNO Q ha WiFi e BLE integrati

---

## Budget totale stimato

| Categoria | Costo minimo | Costo massimo |
|-----------|-------------|--------------|
| Sensori base | 15 € | 20 € |
| Cablaggio + breadboard | 10 € | 15 € |
| Attuatori | 5 € | 8 € |
| **Totale** | **30 €** | **43 €** |

Obiettivo rispettato (sotto 50 €) con margine per imprevisti.
