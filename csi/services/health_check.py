"""
Health check service for WiFi sensing system.
Provides component-level health monitoring with status tracking.
"""

from __future__ import annotations
import asyncio
import logging
import time
from typing import Dict, Any, List, Optional, Callable
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class HealthStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class HealthCheck:
    name: str
    status: HealthStatus
    message: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ServiceHealth:
    name: str
    status: HealthStatus = HealthStatus.UNKNOWN
    last_check: Optional[datetime] = None
    checks: List[HealthCheck] = field(default_factory=list)
    uptime: float = 0.0
    error_count: int = 0
    last_error: Optional[str] = None


class HealthCheckService:
    """
    Service for monitoring application health.

    Usage:
        health = HealthCheckService(check_interval_s=30.0)
        health.register_component("csi_processor")
        health.update_component("csi_processor", HealthStatus.HEALTHY, "Processing")
        health.record_error("csi_processor", "Timeout")
        summary = health.get_summary()
    """

    def __init__(self, check_interval_s: float = 30.0):
        self.check_interval_s = check_interval_s
        self._services: Dict[str, ServiceHealth] = {}
        self._start_time = time.time()
        self._initialized = False
        self._running = False
        self._lock = asyncio.Lock()

    def register_component(self, name: str) -> None:
        """Register a component for health monitoring."""
        if name not in self._services:
            self._services[name] = ServiceHealth(name=name)

    def update_component(self, name: str, status: HealthStatus,
                         message: str = "", details: Optional[Dict] = None) -> None:
        """Update health status of a component."""
        now = datetime.now(timezone.utc)
        if name not in self._services:
            self._services[name] = ServiceHealth(name=name)

        svc = self._services[name]
        svc.status = status
        svc.last_check = now
        svc.uptime = time.time() - self._start_time

        check = HealthCheck(
            name=name, status=status, message=message,
            timestamp=now, details=details or {}
        )
        svc.checks.append(check)

    def record_error(self, component: str, error: str) -> None:
        """Record an error for a component."""
        if component in self._services:
            self._services[component].error_count += 1
            self._services[component].last_error = error
        self.update_component(component, HealthStatus.UNHEALTHY, error)

    def get_component_health(self, name: str) -> Optional[ServiceHealth]:
        return self._services.get(name)

    def get_all_health(self) -> Dict[str, ServiceHealth]:
        return dict(self._services)

    def get_overall_status(self) -> HealthStatus:
        """Returns the worst status across all components."""
        if not self._services:
            return HealthStatus.UNKNOWN
        rank = {s.value: i for i, s in enumerate(HealthStatus)}
        worst = max(self._services.values(),
                   key=lambda s: rank.get(s.status.value, 0))
        return worst.status

    @property
    def uptime_s(self) -> float:
        return time.time() - self._start_time

    def get_summary(self) -> Dict[str, Any]:
        """JSON-serializable status report."""
        return {
            "overall_status": self.get_overall_status().value,
            "uptime_s": round(self.uptime_s, 1),
            "components": {
                name: {
                    "status": svc.status.value,
                    "error_count": svc.error_count,
                    "last_error": svc.last_error,
                    "last_check": svc.last_check.isoformat() if svc.last_check else None,
                    "uptime_s": round(svc.uptime, 1),
                }
                for name, svc in self._services.items()
            }
        }

    def run_check(self, component: str, check_fn: Callable[[], HealthCheck]) -> HealthCheck:
        """Run a health check function with timing."""
        start = time.time()
        try:
            result = check_fn()
        except Exception as e:
            result = HealthCheck(
                name=component,
                status=HealthStatus.UNHEALTHY,
                message=str(e),
            )
        result.duration_ms = (time.time() - start) * 1000
        self._services.setdefault(component, ServiceHealth(name=component))
        self._services[component].checks.append(result)
        return result
