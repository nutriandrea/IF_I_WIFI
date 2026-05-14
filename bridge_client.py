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
import msgpack
import sys
import time
import argparse
from datetime import datetime

SOCKET_PATH = "/var/run/arduino-router.sock"
TIMEOUT_S = 5
MONITOR_PORT = 51023  # default arduino monitor port, may vary


# ============================================================
# MessagePack RPC Client
# ============================================================
class RouterClient:
    """Client RPC per l'arduino-router via Unix socket."""

    def __init__(self, socket_path: str = SOCKET_PATH, timeout: int = TIMEOUT_S):
        self.socket_path = socket_path
        self.timeout = timeout
        self._msgid = 0

    def _next_id(self) -> int:
        self._msgid += 1
        return self._msgid

    def call(self, method: str, *params, timeout: int | None = None) -> any:
        """Esegue una chiamata RPC al router."""
        msgid = self._next_id()
        request = [0, msgid, method, list(params)]
        packed = msgpack.packb(request)

        t = timeout or self.timeout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(t)
            client.connect(self.socket_path)
            client.sendall(packed)
            response_data = client.recv(65536)

        response = msgpack.unpackb(response_data)
        # Formato: [type, msgid, error, result]
        if response[0] != 1:
            raise RuntimeError(f"Unexpected response type: {response[0]}")
        if response[2] is not None:
            raise RuntimeError(f"RPC error: {response[2]}")
        return response[3]

    def discover_methods(self) -> list[str]:
        """Scopre i metodi registrati chiamando $/listMethods se disponibile."""
        try:
            result = self.call("$/listMethods", timeout=2)
            return result if isinstance(result, list) else []
        except (RuntimeError, socket.timeout, ConnectionRefusedError,
                msgpack.exceptions.UnpackException):
            return []

    def ping_mcu(self) -> bool:
        """Chiama ping() sullo STM32."""
        try:
            result = self.call("ping", timeout=3)
            return result is True or result == "true"
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
    parser.add_argument("--benchmark", action="store_true",
                        help="Benchmark throughput RPC")
    parser.add_argument("--list-methods", action="store_true",
                        help="Lista metodi registrati sul router")
    parser.add_argument("--timeout", type=int, default=TIMEOUT_S,
                        help=f"Timeout secondi (default: {TIMEOUT_S})")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  Bridge Client — Arduino UNO Q")
    print(f"  Socket: {SOCKET_PATH}")
    print(f"  Timeout: {args.timeout}s")
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
            print("    Metodi MCU attesi: ping, get_sensors, set_relay")

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
        print(f"  {'✅' if ok/ max(n_calls,1) > 0.5 else '❌'} "
              f"RPC {'funzionante' if ok > n_calls/2 else 'instabile'}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
