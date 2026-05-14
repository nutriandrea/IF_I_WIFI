#!/usr/bin/env python3
"""
Bridge Client — Arduino UNO Q RPC Communication

Si connette all'arduino-router via Unix socket e comunica
con lo STM32 (MCU) via MessagePack RPC.

Usage:
  # Test connessione
  python3 bridge_client.py --ping

  # Leggi sensori
  python3 bridge_client.py --get-sensors

  # Accendi/spegni relay
  python3 bridge_client.py --set-relay 1
  python3 bridge_client.py --set-relay 0

  # Monitor streaming (come Serial Monitor)
  python3 bridge_client.py --monitor

  # Lista metodi registrati
  python3 bridge_client.py --list-methods

  # Benchmark throughput
  python3 bridge_client.py --benchmark
"""

import socket
import struct
import select
import sys
import time
import argparse
import json
from datetime import datetime

SOCKET_PATH = "/var/run/arduino-router.sock"
TIMEOUT_S = 5
MONITOR_PORT = 51023  # default arduino monitor port, may vary
DEBUG = False


# ============================================================
# MessagePack encode/decode built-in (zero dipendenze)
# ============================================================
def _msgpack_encode(obj):
    """Codifica un oggetto Python in MessagePack binario."""
    if obj is None:
        return b'\xc0'
    if isinstance(obj, bool):
        return b'\xc3' if obj else b'\xc2'
    if isinstance(obj, int):
        if 0 <= obj <= 0x7f:
            return bytes([obj])
        if -32 <= obj < 0:
            return bytes([obj & 0xff])
        if 0x80 <= obj <= 0xff:
            return b'\xcc' + bytes([obj])
        if 0x100 <= obj <= 0xffff:
            return b'\xcd' + struct.pack('>H', obj)
        return b'\xce' + struct.pack('>I', obj)
    if isinstance(obj, float):
        return b'\xcb' + struct.pack('>d', obj)
    if isinstance(obj, str):
        data = obj.encode()
        n = len(data)
        if n <= 0x1f:
            return bytes([0xa0 | n]) + data
        if n <= 0xff:
            return b'\xd9' + bytes([n]) + data
        return b'\xda' + struct.pack('>H', n) + data
    if isinstance(obj, (list, tuple)):
        n = len(obj)
        if n <= 0x0f:
            buf = bytes([0x90 | n])
        elif n <= 0xffff:
            buf = b'\xdc' + struct.pack('>H', n)
        else:
            buf = b'\xdd' + struct.pack('>I', n)
        for item in obj:
            buf += _msgpack_encode(item)
        return buf
    if isinstance(obj, bytes):
        n = len(obj)
        if n <= 0x1f:
            return bytes([0xc4 | (n >> 8 if n > 0x1f else 0)]) + (bytes([n]) if n <= 0x1f else b'') + obj
        if n <= 0xff:
            return b'\xc4' + bytes([n]) + obj
        return b'\xc5' + struct.pack('>H', n) + obj
    raise ValueError(f"Cannot encode {type(obj)}")


def _msgpack_decode(data, pos=0):
    """Decodifica MessagePack binario in oggetto Python."""
    b = data[pos]
    pos += 1
    if b <= 0x7f:
        return b, pos
    if b >= 0xe0:
        return b - 256, pos
    if 0xa0 <= b <= 0xbf:
        n = b & 0x1f
        return data[pos:pos + n].decode(), pos + n
    if b == 0xc0:
        return None, pos
    if b == 0xc2:
        return False, pos
    if b == 0xc3:
        return True, pos
    if b == 0xca:
        return struct.unpack('>f', data[pos:pos + 4])[0], pos + 4
    if b == 0xcb:
        return struct.unpack('>d', data[pos:pos + 8])[0], pos + 8
    if b == 0xcc:
        return data[pos], pos + 1
    if b == 0xcd:
        return struct.unpack('>H', data[pos:pos + 2])[0], pos + 2
    if b == 0xce:
        return struct.unpack('>I', data[pos:pos + 4])[0], pos + 4
    if b == 0xd0:
        return struct.unpack('>b', data[pos:pos + 1])[0], pos + 1
    if b == 0xd1:
        return struct.unpack('>h', data[pos:pos + 2])[0], pos + 2
    if b == 0xd2:
        return struct.unpack('>i', data[pos:pos + 4])[0], pos + 4
    if 0x90 <= b <= 0x9f:
        n = b & 0x0f
        result = []
        for _ in range(n):
            val, pos = _msgpack_decode(data, pos)
            result.append(val)
        return result, pos
    if b == 0xdc:
        n = struct.unpack('>H', data[pos:pos + 2])[0]
        pos += 2
        result = []
        for _ in range(n):
            val, pos = _msgpack_decode(data, pos)
            result.append(val)
        return result, pos
    if b == 0xd9:
        n = data[pos]
        pos += 1
        return data[pos:pos + n].decode(), pos + n
    raise ValueError(f"Unknown msgpack byte: 0x{b:02x}")


def _rpc_call(method, *params, timeout=TIMEOUT_S):
    """Chiamata RPC diretta via Unix socket. Ritorna result o None."""
    msgid = 1
    request = [0, msgid, method, list(params)]
    packed = _msgpack_encode(request)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(SOCKET_PATH)
            s.sendall(packed)
            resp = s.recv(65536)
        if not resp:
            return None
        result, _ = _msgpack_decode(resp)
        if isinstance(result, list) and len(result) >= 4:
            return result[3]
        return result
    except (socket.timeout, ConnectionRefusedError, OSError, ValueError):
        return None


# ============================================================
# RPC Client class
# ============================================================
class RouterClient:
    """Client RPC per l'arduino-router via Unix socket (zero dipendenze)."""

    def __init__(self, socket_path: str = SOCKET_PATH, timeout: int = TIMEOUT_S):
        self.socket_path = socket_path
        self.timeout = timeout
        self._msgid = 0

    def _next_id(self) -> int:
        self._msgid += 1
        return self._msgid

    def call(self, method: str, *params, timeout: int | None = None) -> any:
        """Esegue una chiamata RPC al router."""
        global DEBUG
        msgid = self._next_id()
        request = [0, msgid, method, list(params)]
        packed = _msgpack_encode(request)

        if DEBUG:
            print(f"    [DEBUG] Request: {request}")
            print(f"    [DEBUG] Packed ({len(packed)} bytes): {packed.hex()}")

        t = timeout or self.timeout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(t)
            client.connect(self.socket_path)
            client.sendall(packed)
            # Leggi in loop: il router Go fa write() separati per
            # header e body → su Unix socket arrivano come recv() distinti.
            # Usiamo select() con 10ms poll invece di un solo recv().
            client.setblocking(False)
            response_data = b""
            polls = 0
            while polls < 20:  # max 20 tentativi (~200ms polling)
                ready = select.select([client], [], [], 0.01)
                if ready[0]:
                    try:
                        chunk = client.recv(65536)
                        if not chunk:
                            break
                        response_data += chunk
                        polls = 0  # reset: nuovi dati, continua
                        if DEBUG:
                            print(f"    [DEBUG] Chunk ({len(chunk)} bytes): {chunk.hex()}")
                    except BlockingIOError:
                        polls += 1
                else:
                    if response_data:
                        polls += 1  # nessun dato nuovo
                    else:
                        polls += 1  # aspetta primo dato
            client.setblocking(True)

        if DEBUG:
            print(f"    [DEBUG] Total response ({len(response_data)} bytes): {response_data.hex()}")

        response, _ = _msgpack_decode(response_data)

        if DEBUG:
            print(f"    [DEBUG] Decoded: {response}")

        if not isinstance(response, list):
            raise RuntimeError(f"Response is not a list: {type(response).__name__} = {response}")
        if len(response) < 4:
            # Could be [result] or [result, error] format
            if len(response) == 1:
                return response[0]
            if len(response) == 2:
                if response[1] is not None:
                    raise RuntimeError(f"RPC error: {response[1]}")
                return response[0]
            raise RuntimeError(f"Response list too short: {len(response)} elements = {response}")
        if response[0] != 1:
            raise RuntimeError(f"Unexpected response type: {response[0]}")
        if response[2] is not None:
            err = response[2]
            if isinstance(err, list) and len(err) >= 2:
                raise RuntimeError(f"code={err[0]}: {err[1]}")
            raise RuntimeError(f"RPC error: {err}")
        return response[3]

    def discover_methods(self) -> list[str]:
        """Scopre i metodi registrati chiamando $/listMethods se disponibile."""
        try:
            result = self.call("$/listMethods", timeout=2)
            return result if isinstance(result, list) else []
        except (RuntimeError, socket.timeout, ConnectionRefusedError, ValueError):
            return []

    def ping_mcu(self) -> bool:
        """Chiama ping() sullo STM32."""
        try:
            result = self.call("ping", timeout=3)
            return result is True or result == "true"
        except RuntimeError as e:
            msg = str(e)
            print(f"    RPC error: {msg}")
            if "method" in msg and "not available" in msg:
                print(f"    -> Il metodo 'ping' non e registrato sullo STM32.")
                print(f"    -> Lo sketch e stato caricato correttamente?")
                print(f"    -> Bridge.provide('ping', ping) chiamato in setup()?")
            return False
        except Exception as e:
            print(f"    Ping error: {e}")
            return False

    def get_sensors(self) -> str | None:
        """Chiama get_sensors() sullo STM32."""
        try:
            result = self.call("get_sensors", timeout=3)
            return str(result)
        except Exception as e:
            print(f"    get_sensors error: {e}")
            return None

    def set_relay(self, state: bool) -> bool | None:
        """Chiama set_relay(state) sullo STM32."""
        try:
            result = self.call("set_relay", 1 if state else 0, timeout=3)
            return bool(result)
        except Exception as e:
            print(f"    set_relay error: {e}")
            return None


# ============================================================
# Monitor TCP client (per lo stream Monitor.println)
# ============================================================
class MonitorClient:
    """Legge lo stream Monitor dal router (come Serial Monitor)."""

    def __init__(self, host: str = "127.0.0.1", port: int = MONITOR_PORT):
        self.host = host
        self.port = port
        self._buf = b""

    def stream(self, duration_s: int = 10):
        """Legge lo stream Monitor per `duration_s` secondi."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(3)
                sock.connect((self.host, self.port))
                print(f"  Connected to Monitor on {self.host}:{self.port}")

                t0 = time.time()
                lines = 0
                while time.time() - t0 < duration_s:
                    try:
                        data = sock.recv(4096)
                        if not data:
                            break
                        self._buf += data
                        while b"\n" in self._buf:
                            line, self._buf = self._buf.split(b"\n", 1)
                            line_str = line.decode().strip()
                            if line_str:
                                print(f"  [{time.time()-t0:.1f}s] {line_str}")
                                lines += 1
                    except socket.timeout:
                        break

                rate = lines / duration_s
                print(f"\n  Received {lines} lines ({rate:.1f}/s)")
                return lines

        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            print(f"  Monitor connection failed: {e}")
            print(f"  (Monitor port might be different. Try --monitor-scan)")
            return 0


def scan_monitor_port() -> int | None:
    """Cerca la porta del Monitor TCP."""
    import subprocess
    try:
        result = subprocess.check_output(
            "ss -tlnp | grep arduino", shell=True, timeout=5,
            stderr=subprocess.DEVNULL
        ).decode()
        import re
        ports = re.findall(r":(\d+)", result)
        if ports:
            return int(ports[0])
    except Exception:
        pass
    return None


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Bridge Client per Arduino UNO Q RPC")
    parser.add_argument("--ping", action="store_true",
                        help="Test connessione STM32")
    parser.add_argument("--get-sensors", action="store_true",
                        help="Leggi sensori dallo STM32")
    parser.add_argument("--set-relay", type=int, choices=[0, 1],
                        help="Accendi (1) / spegni (0) relay")
    parser.add_argument("--monitor", action="store_true",
                        help="Streaming Monitor (come Serial Monitor)")
    parser.add_argument("--monitor-seconds", type=int, default=10,
                        help="Durata monitor in secondi (default: 10)")
    parser.add_argument("--monitor-scan", action="store_true",
                        help="Scansiona porta Monitor TCP")
    parser.add_argument("--test-uart", action="store_true",
                        help="Test UART D0/D1 (loopback o ESP32)")
    parser.add_argument("--benchmark", action="store_true",
                        help="Benchmark throughput RPC")
    parser.add_argument("--list-methods", action="store_true",
                        help="Lista metodi registrati sul router")
    parser.add_argument("--timeout", type=int, default=TIMEOUT_S,
                        help=f"Timeout secondi (default: {TIMEOUT_S})")
    parser.add_argument("--debug", action="store_true",
                        help="Debug: stampa bytes raw e decodifica")
    parser.add_argument("--call", type=str, metavar="METHOD",
                        help="Chiama metodo RPC arbitrario (es. $/version, $/reset)")
    parser.add_argument("--call-args", type=str, metavar="ARGS",
                        help="Argomenti JSON per --call (es. [\"ping\"] per $/register)")
    args = parser.parse_args()

    global DEBUG
    if args.debug:
        DEBUG = True

    print(f"\n{'='*60}")
    print(f"  Bridge Client — Arduino UNO Q")
    print(f"  Socket: {SOCKET_PATH}")
    print(f"  Timeout: {args.timeout}s")
    if DEBUG:
        print(f"  DEBUG: ON")
    print(f"{'='*60}")

    # Check socket exists
    import os
    if not os.path.exists(SOCKET_PATH):
        print(f"\n  ERRORE: Socket {SOCKET_PATH} non trovata.")
        print(f"  Il servizio arduino-router e in esecuzione?")
        print(f"  Controlla: systemctl status arduino-router")
        sys.exit(1)

    client = RouterClient(timeout=args.timeout)

    # --- List methods ---
    if args.list_methods:
        print("\n  --- Metodi registrati ---")
        methods = client.discover_methods()
        if methods:
            for m in methods:
                print(f"    - {m}")
        else:
            print("    (nessun metodo trovato o $/listMethods non supportato)")
            print("    Metodi noti del router: $/serial/open, $/serial/close")
            print("    Metodi MCU attesi: ping, get_sensors, set_relay, test_uart")

    # --- Ping ---
    elif args.ping:
        print("\n  --- Ping STM32 ---")
        t0 = time.time()
        ok = client.ping_mcu()
        elapsed = (time.time() - t0) * 1000
        if ok:
            print(f"  ✅ STM32 risponde ({elapsed:.0f}ms)")
        else:
            print(f"  ❌ STM32 non raggiungibile ({elapsed:.0f}ms)")
            print(f"     Lo sketch e caricato sullo STM32?")

    # --- Get sensors ---
    elif args.get_sensors:
        print("\n  --- Get Sensors ---")
        result = client.get_sensors()
        if result:
            parts = result.split(",")
            if len(parts) >= 5:
                print(f"  Timestamp:  {parts[0]}s")
                print(f"  Temperatura: {parts[1]}°C")
                print(f"  Umidita:    {parts[2]}%")
                print(f"  Aria (MQ135): {parts[3]}")
                print(f"  Luce (LDR):  {parts[4]}")
            else:
                print(f"  Raw: {result}")
        else:
            print(f"  ❌ Nessun dato ricevuto")

    # --- Set relay ---
    elif args.set_relay is not None:
        state = bool(args.set_relay)
        print(f"\n  --- Set Relay {'ON' if state else 'OFF'} ---")
        result = client.set_relay(state)
        if result is not None:
            print(f"  ✅ Relay: {'ON' if result else 'OFF'}")
        else:
            print(f"  ❌ Comando fallito")

    # --- Test UART D0/D1 ---
    elif args.test_uart:
        print("\n  --- Test UART D0/D1 ---")
        print(f"  Chiamata RPC: test_uart()...")
        result = client.call("test_uart", timeout=5)
        if result is not None:
            result_str = str(result)
            if result_str == "LOOPBACK_OK":
                print(f"  ✅ Loopback OK — D0/D1 UART funziona (cortocircuita con jumper)")
            elif result_str == "ESP32_OK":
                print(f"  ✅ ESP32 risponde su D0/D1")
            elif result_str.startswith("FAIL"):
                print(f"  ❌ {result_str}")
                print(f"     D0-D1 sono cortocircuitati (jumper)? O ESP32 accesa?")
            else:
                print(f"  ⚠️  Risposta inattesa: {result_str}")
        else:
            print(f"  ❌ STM32 non raggiungibile o test_uart non registrato")
            print(f"     Lo sketch modificato e caricato sullo STM32?")

    # --- Monitor scan ---
    elif args.monitor_scan:
        print("\n  --- Scansione Monitor TCP ---")
        port = scan_monitor_port()
        if port:
            print(f"  ✅ Monitor trovato su 127.0.0.1:{port}")
        else:
            print(f"  Monitor TCP non rilevato. Porta default: {MONITOR_PORT}")
            print(f"  Prova: python3 bridge_client.py --monitor")

    # --- Monitor stream ---
    elif args.monitor:
        port = scan_monitor_port() or MONITOR_PORT
        print(f"\n  --- Monitor Stream ({args.monitor_seconds}s) ---")
        mc = MonitorClient(port=port)
        mc.stream(duration_s=args.monitor_seconds)

    # --- Benchmark ---
    elif args.benchmark:
        print("\n  --- Benchmark RPC ---")
        n_calls = 50
        ok = 0
        errors = 0
        t0 = time.time()
        for i in range(n_calls):
            try:
                result = client.call("ping", timeout=2)
                if result is True or result == "true":
                    ok += 1
                else:
                    errors += 1
            except Exception:
                errors += 1
        elapsed = time.time() - t0
        rate = ok / elapsed if elapsed > 0 else 0
        print(f"  Chiamate: {n_calls}, OK: {ok}, Errori: {errors}")
        print(f"  Throughput: {rate:.1f} call/s ({elapsed:.1f}s totali)")
        print(f"  {'OK' if ok / max(n_calls, 1) > 0.5 else 'FAIL'} "
              f"RPC {'funzionante' if ok > n_calls / 2 else 'instabile'}")

    # --- Raw RPC call ---
    elif args.call:
        method = args.call
        params = []
        if args.call_args:
            params = json.loads(args.call_args)
        print(f"\n  --- RPC call: {method}{params} ---")
        try:
            result = client.call(method, *params, timeout=args.timeout)
            print(f"  Response: {result!r}")
        except Exception as e:
            print(f"  Error: {e}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
