"""
Main service orchestrator for WiFi sensing system.
Manages lifecycle of all services.
"""

from __future__ import annotations
import asyncio
import logging
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager

from .health_check import HealthCheckService, HealthStatus
from .metrics import MetricsService

logger = logging.getLogger(__name__)


class ServiceOrchestrator:
    """
    Main service orchestrator that manages lifecycle of all services.
    """

    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        self.settings = settings or {}
        self._services: Dict[str, Any] = {}
        self._background_tasks: List[asyncio.Task] = []
        self._initialized = False
        self._started = False

        # Core services
        self.health_service = HealthCheckService(
            self.settings.get("health_check_interval_s", 30.0)
        )
        self.metrics_service = MetricsService(
            self.settings.get("metrics_retention", 1000)
        )

        # App service placeholders
        self.csi_service = None
        self.detector_service = None
        self.stream_service = None

    async def initialize(self):
        if self._initialized:
            return
        logger.info("Initializing services...")
        try:
            # Init core
            self.health_service.register_component("orchestrator")
            self.health_service.register_component("csi")
            self.health_service.register_component("detector")

            self._services = {
                'health': self.health_service,
                'metrics': self.metrics_service,
                'csi': self.csi_service,
                'detector': self.detector_service,
            }
            self._initialized = True
            self.health_service.update_component("orchestrator", HealthStatus.HEALTHY, "Initialized")
            logger.info("Services initialized")
        except Exception as e:
            logger.error(f"Init failed: {e}")
            await self.shutdown()
            raise

    async def start(self):
        if self._started:
            return
        self._started = True
        # Start background health check loop
        task = asyncio.create_task(self._health_check_loop())
        self._background_tasks.append(task)
        logger.info("Services started")

    async def _health_check_loop(self):
        while self._started:
            await asyncio.sleep(self.health_service.check_interval_s)
            self.health_service.update_component("orchestrator",
                HealthStatus.HEALTHY, "Running")

    async def shutdown(self):
        self._started = False
        for task in self._background_tasks:
            task.cancel()
        await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        logger.info("Services shut down")

    def register_service(self, name: str, service: Any) -> None:
        self._services[name] = service

    def get_service(self, name: str) -> Optional[Any]:
        return self._services.get(name)

    def health_summary(self) -> Dict[str, Any]:
        return self.health_service.get_summary()

    def metrics_summary(self) -> Dict[str, Any]:
        return self.metrics_service.get_summary() if hasattr(self.metrics_service, 'get_summary') else {}


@asynccontextmanager
async def service_lifetime(orchestrator: ServiceOrchestrator):
    """Context manager for service lifecycle."""
    await orchestrator.initialize()
    await orchestrator.start()
    try:
        yield orchestrator
    finally:
        await orchestrator.shutdown()
