"""
ha_bridge.py — Home Assistant MQTT integration for WiFi sensing data.

Publishes vitals (breathing rate, heart rate, presence, motion) as
Home Assistant MQTT discovery entities.  Designed after RuView ADR-115
topic namespace and ADR-117 P4 HA blueprint helpers.

Requires paho-mqtt (optional dependency — import guard at runtime).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

_HA_AVAILABLE: bool = False
try:
    import paho.mqtt.client as mqtt  # type: ignore[import-untyped]
    _HA_AVAILABLE = True
except ImportError:
    mqtt = None  # type: ignore[assignment]


# ─── Entity definitions ─────────────────────────────────────────────

@dataclass
class HaEntityDef:
    kind: str
    object_id: str
    name: str
    device_class: str = ""
    unit: str = ""
    icon: str = ""
    value_template: str = ""
    enabled_by_default: bool = True

SENSOR_ENTITIES: list[HaEntityDef] = [
    HaEntityDef("sensor", "breathing_rate", "Breathing Rate",
                device_class="respiratory_rate", unit="bpm",
                icon="mdi:lungs"),
    HaEntityDef("sensor", "heart_rate", "Heart Rate",
                device_class="heart_rate", unit="bpm",
                icon="mdi:heart-pulse"),
    HaEntityDef("sensor", "breathing_confidence", "Breathing Confidence",
                unit="%", icon="mdi:signal-variant"),
    HaEntityDef("sensor", "heart_rate_confidence", "Heart Rate Confidence",
                unit="%", icon="mdi:signal-variant"),
    HaEntityDef("sensor", "motion_energy", "Motion Energy",
                device_class="power", icon="mdi:run"),
    HaEntityDef("sensor", "rssi", "RSSI",
                device_class="signal_strength", unit="dBm",
                icon="mdi:wifi"),
]

BINARY_SENSOR_ENTITIES: list[HaEntityDef] = [
    HaEntityDef("binary_sensor", "presence", "Presence",
                device_class="presence", icon="mdi:home"),
    HaEntityDef("binary_sensor", "motion", "Motion",
                device_class="motion", icon="mdi:motion-sensor"),
    HaEntityDef("binary_sensor", "fall_detected", "Fall Detected",
                device_class="safety", icon="mdi:alert"),
]


# ─── Bridge ─────────────────────────────────────────────────────────

class HaBridge:
    """
    Publishes WiFi sensing data to Home Assistant via MQTT discovery.

    Parameters
    ----------
    node_id : str
        Unique identifier for this sensing node (e.g. MAC or hostname).
    mqtt_host : str
        MQTT broker hostname.
    mqtt_port : int
        MQTT broker port (default 1883).
    mqtt_user : str, optional
        MQTT username.
    mqtt_password : str, optional
        MQTT password.
    topic_prefix : str
        HA discovery topic prefix (default ``homeassistant``).
    node_name : str
        Human-readable node name for HA UI.

    Example
    -------
    >>> bridge = HaBridge(node_id="esp32_0", mqtt_host="core-mosquitto")
    >>> bridge.connect()
    >>> bridge.publish_vitals(br=16.2, hr=72.0, presence=True, motion=0.3)
    """

    def __init__(
        self,
        node_id: str,
        mqtt_host: str = "localhost",
        mqtt_port: int = 1883,
        mqtt_user: Optional[str] = None,
        mqtt_password: Optional[str] = None,
        topic_prefix: str = "homeassistant",
        node_name: str = "WiFi Sensing",
    ) -> None:
        if not _HA_AVAILABLE:
            raise ImportError(
                "paho-mqtt is required for Home Assistant integration. "
                "Install: pip install paho-mqtt"
            )

        self.node_id = node_id
        self.topic_prefix = topic_prefix
        self.node_name = node_name
        self._client: Any = None
        self._connected = False

        self._mqtt_host = mqtt_host
        self._mqtt_port = mqtt_port
        self._mqtt_user = mqtt_user
        self._mqtt_password = mqtt_password

        self._discovery_published = False

    # ── Connection ─────────────────────────────────────────────────

    def connect(self) -> None:
        if self._connected:
            return
        assert mqtt is not None  # guarded by _HA_AVAILABLE
        self._client = mqtt.Client()
        if self._mqtt_user:
            self._client.username_pw_set(self._mqtt_user, self._mqtt_password)
        self._client.connect(self._mqtt_host, self._mqtt_port)
        self._client.loop_start()
        self._connected = True
        logger.info("HA bridge connected to %s:%d", self._mqtt_host, self._mqtt_port)

    def disconnect(self) -> None:
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
        self._connected = False

    # ── Discovery ──────────────────────────────────────────────────

    def publish_discovery(self) -> None:
        """Publish MQTT discovery config for all entities."""
        if not self._client or not self._connected:
            raise RuntimeError("HA bridge not connected")

        state_topic = f"{self.topic_prefix}/sensor/wifi_sensing_{self.node_id}/state"

        device = {
            "identifiers": [f"wifi_sensing_{self.node_id}"],
            "name": self.node_name,
            "model": "Arduino WiFi Sensing",
            "manufacturer": "Custom",
            "sw_version": "2.0.0",
        }

        for ent in SENSOR_ENTITIES:
            config = {
                "name": ent.name,
                "unique_id": f"wifi_sensing_{self.node_id}_{ent.object_id}",
                "state_topic": state_topic,
                "device": device,
                "enabled_by_default": ent.enabled_by_default,
                "icon": ent.icon,
            }
            if ent.device_class:
                config["device_class"] = ent.device_class
            if ent.unit:
                config["unit_of_measurement"] = ent.unit
            if ent.value_template:
                config["value_template"] = ent.value_template
            else:
                config["value_template"] = f"{{{{ value_json.{ent.object_id} }}}}"

            topic = (f"{self.topic_prefix}/{ent.kind}/"
                     f"wifi_sensing_{self.node_id}/{ent.object_id}/config")
            self._client.publish(topic, json.dumps(config), retain=True)

        for ent in BINARY_SENSOR_ENTITIES:
            config = {
                "name": ent.name,
                "unique_id": f"wifi_sensing_{self.node_id}_{ent.object_id}",
                "state_topic": state_topic,
                "device": device,
                "enabled_by_default": ent.enabled_by_default,
                "icon": ent.icon,
                "payload_on": "true",
                "payload_off": "false",
            }
            if ent.device_class:
                config["device_class"] = ent.device_class
            config["value_template"] = f"{{{{ value_json.{ent.object_id} }}}}"

            topic = (f"{self.topic_prefix}/{ent.kind}/"
                     f"wifi_sensing_{self.node_id}/{ent.object_id}/config")
            self._client.publish(topic, json.dumps(config), retain=True)

        self._discovery_published = True
        logger.info("HA discovery published for node %s", self.node_id)

    # ── Publishing ─────────────────────────────────────────────────

    def publish_vitals(
        self,
        breathing_rate_bpm: Optional[float] = None,
        heart_rate_bpm: Optional[float] = None,
        presence: bool = False,
        motion: bool = False,
        motion_energy: float = 0.0,
        fall_detected: bool = False,
        breathing_confidence: float = 0.0,
        heart_rate_confidence: float = 0.0,
        rssi: Optional[float] = None,
    ) -> None:
        if not self._client or not self._connected:
            raise RuntimeError("HA bridge not connected")

        if not self._discovery_published:
            self.publish_discovery()

        payload: dict[str, Any] = {
            "breathing_rate": breathing_rate_bpm if breathing_rate_bpm is not None else 0.0,
            "heart_rate": heart_rate_bpm if heart_rate_bpm is not None else 0.0,
            "breathing_confidence": round(breathing_confidence * 100.0, 1),
            "heart_rate_confidence": round(heart_rate_confidence * 100.0, 1),
            "presence": "true" if presence else "false",
            "motion": "true" if motion else "false",
            "motion_energy": round(motion_energy, 3),
            "fall_detected": "true" if fall_detected else "false",
            "rssi": rssi if rssi is not None else 0.0,
        }

        state_topic = f"{self.topic_prefix}/sensor/wifi_sensing_{self.node_id}/state"
        self._client.publish(state_topic, json.dumps(payload))
        logger.debug("Published vitals to %s", state_topic)
