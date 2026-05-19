#!/usr/bin/env python3
"""
csi_ble.py — Lettore CSI via Bluetooth BLE (Nordic UART Service).

Si connette all'ESP32 via BLE (firmware esp32_csi_ble.ino) e
legge il flusso CSI. Rimpiazza il cavo UART.

Uso (da csi_mac.py, seat_mapper.py, ecc.):
    from csi.csi_ble import BleReader

    reader = BleReader()
    reader.connect()                  # connetti a "ESP32_CSI"
    for line in reader.iter_lines():  # blocca, yield linee CSI
        parsed = parse_csi_line(line)
        ...

    reader.send("ping")               # comandi

Dipendenze:
    pip install bleak                  # cross-platform BLE (Mac/Linux/Windows)

Su UNO Q Linux MPU:
    pip3 install bleak
    # Serve BlueZ + D-Bus (già presente su Yocto/Buildroot con BT)
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from typing import Callable, Optional

# bleak — BLE library cross-platform
_BLEAK_AVAILABLE = False
try:
    import bleak
    from bleak import BleakScanner, BleakClient
    _BLEAK_AVAILABLE = True
except ImportError:
    bleak = None  # type: ignore
    BleakScanner = None  # type: ignore
    BleakClient = None  # type: ignore

# ============================================================
# BLE Service UUIDs — Nordic UART Service (NUS)
# ============================================================
NUS_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9F"
NUS_TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9F"  # notify (ESP32 → central)
NUS_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9F"  # write  (central → ESP32)

ESP32_BLE_NAME = "ESP32_CSI"

# timeout connessione
SCAN_TIMEOUT = 10
CONNECT_TIMEOUT = 15


# ============================================================
# BleReader
# ============================================================

class BleReader:
    """
    Legge CSI da ESP32 via BLE (Nordic UART Service).

    Usage:
        reader = BleReader()
        if reader.connect():
            for line in reader.iter_lines():
                print(line)  # "CSI:1:-45:-90:6:20:16:..."
    """

    def __init__(self, device_name: str = ESP32_BLE_NAME):
        self.device_name = device_name
        self._client: Optional[BleakClient] = None
        self._tx_char: Optional[bleak.BleakGATTCharacteristic] = None
        self._rx_char: Optional[bleak.BleakGATTCharacteristic] = None
        self._line_buffer = ""
        self._lines_queue: asyncio.Queue = asyncio.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected and self._client is not None and self._client.is_connected

    # ── Connect ──────────────────────────────────────────

    def connect(self, timeout: int = CONNECT_TIMEOUT) -> bool:
        """Cerca e connetti ESP32 via BLE. Bloccante."""
        if not self._ensure_bleak():
            return False

        # Trova il dispositivo
        print(f"  [BLE] Scansiono per '{self.device_name}'...")
        device = self._scan(timeout)
        if device is None:
            print(f"  [BLE] ERRORE: '{self.device_name}' non trovato")
            return False

        print(f"  [BLE] Trovato: {device.name} ({device.address})")

        # Connetti in un event loop
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._do_connect(device))
            self._connected = True
            print(f"  [BLE] Connesso a {device.address}")
            return True
        except Exception as e:
            print(f"  [BLE] ERRORE connessione: {e}")
            return False

    def disconnect(self):
        """Disconnetti BLE."""
        if self._loop and self._client:
            try:
                self._loop.run_until_complete(self._client.disconnect())
            except Exception:
                pass
        self._connected = False
        self._client = None

    # ── Send command ─────────────────────────────────────

    def send(self, command: str) -> bool:
        """Invia comando (ping, start, stop, status) all'ESP32 via BLE."""
        if not self.connected or not self._rx_char:
            return False
        try:
            cmd_bytes = (command.strip() + "\n").encode("utf-8")
            self._loop.run_until_complete(
                self._client.write_gatt_char(self._rx_char, cmd_bytes)
            )
            return True
        except Exception as e:
            print(f"  [BLE] Errore invio comando: {e}")
            return False

    # ── Iterate lines ────────────────────────────────────

    def iter_lines(self):
        """Generatore bloccante: yield linee CSI in arrivo via BLE."""
        if not self.connected:
            return

        while self.connected:
            try:
                line = self._loop.run_until_complete(
                    asyncio.wait_for(self._lines_queue.get(), timeout=1.0)
                )
                yield line
            except asyncio.TimeoutError:
                continue
            except (EOFError, GeneratorExit):
                break

    # ── Internal ─────────────────────────────────────────

    @staticmethod
    def _ensure_bleak() -> bool:
        """Verifica che bleak sia installato."""
        if not _BLEAK_AVAILABLE:
            print("  bleak non installato. pip install bleak")
            return False
        return True

    def _scan(self, timeout: int) -> Optional[bleak.BLEDevice]:
        """Scansiona dispositivi BLE e cerca ESP32_CSI."""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            device = loop.run_until_complete(
                BleakScanner.find_device_by_name(self.device_name, timeout=timeout)
            )
            loop.close()
            return device
        except Exception as e:
            print(f"  [BLE] Scan error: {e}")
            return None

    async def _do_connect(self, device: bleak.BLEDevice):
        """Connessione asincrona + setup notifiche."""
        self._client = BleakClient(device, timeout=CONNECT_TIMEOUT)
        await self._client.connect()

        srv_list = self._client.services
        if srv_list is None or len(srv_list) == 0:
            srv_list = await self._client.get_services()

        for service in srv_list:
            if service.uuid == NUS_SERVICE_UUID:
                for char in service.characteristics:
                    if char.uuid == NUS_TX_CHAR_UUID:
                        self._tx_char = char
                    elif char.uuid == NUS_RX_CHAR_UUID:
                        self._rx_char = char

        if not self._tx_char or not self._rx_char:
            found = [s.uuid for s in srv_list] if srv_list else []
            raise RuntimeError(f"NUS service non trovato. Trovati servizi: {found}")

        # Sottoscrivi notifiche
        await self._client.start_notify(
            self._tx_char,
            self._notification_handler
        )

    def _notification_handler(self, sender: int, data: bytearray):
        """Handler chiamato ad ogni notifica BLE dall'ESP32."""
        text = data.decode("utf-8", errors="replace")
        self._line_buffer += text

        # Estrai linee complete
        while "\n" in self._line_buffer:
            idx = self._line_buffer.index("\n")
            line = self._line_buffer[:idx].strip()
            self._line_buffer = self._line_buffer[idx + 1:]

            if line:
                self._lines_queue.put_nowait(line)


# ============================================================
# Helper: BLE reader con callback (API compatibile con serial_reader)
# ============================================================

def ble_reader(device_name: str = ESP32_BLE_NAME,
               callback: Callable[[str], None] = print,
               stop_event=None):
    """Reader BLE in stile serial_reader: chiama callback per ogni linea.

    Bloccante: connette, poi loopa finche' stop_event e' settato.
    Riconnessione automatica su disconnessione.

    Args:
        device_name: Nome BLE dell'ESP32
        callback: Chiamata con ogni linea ricevuta
        stop_event: threading.Event per fermare
    """
    if not _BLEAK_AVAILABLE:
        print("  bleak non installato. pip install bleak")
        return

    import threading

    if stop_event is None:
        stop_event = threading.Event()

    while not stop_event.is_set():
        reader = BleReader(device_name)
        if not reader.connect():
            print("  [BLE] Riprovo tra 3s...")
            if stop_event.wait(3.0):
                return
            continue

        print(f"  [BLE] Connesso! In attesa dati...")

        try:
            for line in reader.iter_lines():
                if stop_event.is_set():
                    break
                if line:
                    callback(line)
        except Exception as e:
            print(f"  [BLE] Errore lettura: {e}")
        finally:
            reader.disconnect()

        if not stop_event.is_set():
            print("  [BLE] Disconnesso, riconnessione tra 2s...")
            stop_event.wait(2.0)


# ============================================================
# CLI
# ============================================================

def main():
    """Test BLE reader: connetti e stampa linee."""
    import argparse
    import signal

    parser = argparse.ArgumentParser(description="BLE CSI Reader — test")
    parser.add_argument("--name", default=ESP32_BLE_NAME,
                        help=f"Nome BLE device (default: {ESP32_BLE_NAME})")
    parser.add_argument("--timeout", type=int, default=SCAN_TIMEOUT,
                        help="Scan timeout in secondi")
    args = parser.parse_args()

    if not _BLEAK_AVAILABLE:
        print("Installa bleak: pip install bleak")
        sys.exit(1)

    stop_event = __import__("threading").Event()

    def signal_handler(sig, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print(f"  BLE Reader — connessione a '{args.name}'...")
    ble_reader(args.name, callback=lambda line: print(line), stop_event=stop_event)
    print("\n  Fermato.")


if __name__ == "__main__":
    main()
