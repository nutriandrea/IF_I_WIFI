"""
ws_client.py — WebSocket client for remote CSI sensing-server streams.

Yields typed messages (EdgeVitals, ConnectionEstablished) from a
RuView-compatible sensing-server's WS endpoint.  Ported from RuView
ADR-117 P4 SensingClient pattern.

Optional dependency: ``websockets>=12`` (import guard at runtime).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)

_WS_AVAILABLE: bool = False
try:
    import websockets  # type: ignore[import-not-found]
    from websockets.exceptions import ConnectionClosed  # type: ignore[import-not-found]
    _WS_AVAILABLE = True
except ImportError:
    websockets = None  # type: ignore
    ConnectionClosed = Exception


# ─── Typed messages ─────────────────────────────────────────────────

@dataclass(frozen=True)
class WsMessage:
    type: str
    raw: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)


@dataclass(frozen=True)
class ConnectionEstablished(WsMessage):
    node_id: str = ""
    version: str = ""
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class EdgeVitals(WsMessage):
    node_id: str = ""
    presence: bool = False
    fall_detected: bool = False
    motion: float = 0.0
    breathing_rate_bpm: Optional[float] = None
    heartrate_bpm: Optional[float] = None
    n_persons: int = 0
    motion_energy: float = 0.0
    presence_score: float = 0.0
    rssi: Optional[float] = None


@dataclass(frozen=True)
class PoseData(WsMessage):
    node_id: str = ""
    timestamp: float = 0.0
    persons: list[dict] = field(default_factory=list)
    confidence: float = 0.0


@dataclass(frozen=True)
class NodeSensingInfo:
    """Per-node amplitude snapshot from a Rust sensing-server broadcast."""
    node_id: int = 0
    rssi_dbm: float = 0.0
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    amplitude: tuple[float, ...] = ()
    subcarrier_count: int = 0


@dataclass(frozen=True)
class SensingUpdate(WsMessage):
    """Raw sensing broadcast from a RuView-compatible Rust sensing-server.

    Carries per-subcarrier amplitudes (no phases) for each ESP32 node,
    plus optional vital-sign estimates and multi-person detections.
    """
    timestamp: float = 0.0
    source: str = ""
    tick: int = 0
    nodes: tuple[NodeSensingInfo, ...] = ()
    persons: tuple[dict, ...] = ()
    vital_signs: dict[str, Any] = field(default_factory=dict)


# ─── Parsing ────────────────────────────────────────────────────────

def _parse_message(raw: dict[str, Any]) -> WsMessage:
    msg_type = raw.get("type", "")
    if msg_type == "connection_established":
        return ConnectionEstablished(
            type=msg_type, raw=raw,
            node_id=raw.get("node_id", ""),
            version=raw.get("version", ""),
            capabilities=tuple(raw.get("capabilities", [])),
        )
    elif msg_type == "edge_vitals":
        return EdgeVitals(
            type=msg_type, raw=raw,
            node_id=raw.get("node_id", ""),
            presence=bool(raw.get("presence", False)),
            fall_detected=bool(raw.get("fall_detected", False)),
            motion=float(raw.get("motion", 0.0)),
            breathing_rate_bpm=_opt_float(raw, "breathing_rate_bpm"),
            heartrate_bpm=_opt_float(raw, "heartrate_bpm"),
            n_persons=int(raw.get("n_persons", 0)),
            motion_energy=float(raw.get("motion_energy", 0.0)),
            presence_score=float(raw.get("presence_score", 0.0)),
            rssi=_opt_float(raw, "rssi"),
        )
    elif msg_type == "pose_data":
        return PoseData(
            type=msg_type, raw=raw,
            node_id=raw.get("node_id", ""),
            timestamp=float(raw.get("timestamp", 0.0)),
            persons=raw.get("persons", []),
            confidence=float(raw.get("confidence", 0.0)),
        )
    elif msg_type == "sensing_update":
        raw_nodes: list[dict] = raw.get("nodes", [])
        nodes = tuple(
            NodeSensingInfo(
                node_id=int(n.get("node_id", 0)),
                rssi_dbm=float(n.get("rssi_dbm", 0.0)),
                position=tuple(n.get("position", [0.0, 0.0, 0.0])),
                amplitude=tuple(n.get("amplitude", [])),
                subcarrier_count=int(n.get("subcarrier_count", 0)),
            )
            for n in raw_nodes
        )
        raw_persons: list[dict] = raw.get("persons", [])
        return SensingUpdate(
            type=msg_type, raw=raw,
            timestamp=float(raw.get("timestamp", 0.0)),
            source=raw.get("source", ""),
            tick=int(raw.get("tick", 0)),
            nodes=nodes,
            persons=tuple(raw_persons),
            vital_signs=raw.get("vital_signs", {}),
        )
    else:
        return WsMessage(type=msg_type, raw=raw)


def _opt_float(data: dict, key: str) -> Optional[float]:
    v = data.get(key)
    if v is None:
        return None
    return float(v)


# ─── Client ─────────────────────────────────────────────────────────

class SensingWsClient:
    """
    Asyncio WebSocket client for a RuView-compatible sensing server.

    Connects to ``ws://host:port/ws/sensing`` and yields typed
    messages via ``stream()``.

    Parameters
    ----------
    uri : str
        WebSocket URI (e.g. ``ws://localhost:8765/ws/sensing``).
    reconnect_delay : float
        Seconds to wait before reconnecting on disconnect (default 5.0).
    max_reconnects : int
        Maximum reconnection attempts (-1 for infinite).

    Example
    -------
    >>> client = SensingWsClient("ws://192.168.1.100:8765/ws/sensing")
    >>> async with client:
    ...     async for msg in client.stream():
    ...         if isinstance(msg, EdgeVitals):
    ...             print(f"BR={msg.breathing_rate_bpm}")
    """

    def __init__(
        self,
        uri: str,
        reconnect_delay: float = 5.0,
        max_reconnects: int = -1,
    ) -> None:
        if not _WS_AVAILABLE:
            raise ImportError(
                "websockets is required for SensingWsClient. "
                "Install: pip install websockets"
            )
        self._uri = uri
        self._reconnect_delay = reconnect_delay
        self._max_reconnects = max_reconnects
        self._ws: Any = None
        self._connect_count = 0

    async def connect(self) -> None:
        self._ws = await websockets.connect(self._uri)  # type: ignore
        self._connect_count += 1
        logger.info("Connected to %s", self._uri)

    async def disconnect(self) -> None:
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def __aenter__(self) -> SensingWsClient:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.disconnect()

    async def stream(self) -> AsyncIterator[WsMessage]:
        reconnect_count = 0
        while True:
            try:
                if not self._ws:
                    await self.connect()
                async for raw in self._ws:
                    data = json.loads(raw)
                    yield _parse_message(data)
                break
            except (ConnectionClosed, OSError) as exc:
                logger.warning("WS disconnected: %s", exc)
                self._ws = None
                if self._max_reconnects >= 0:
                    reconnect_count += 1
                    if reconnect_count > self._max_reconnects:
                        logger.error("Max reconnects reached")
                        break
                await asyncio.sleep(self._reconnect_delay)
