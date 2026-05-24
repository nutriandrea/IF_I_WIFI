"""
Metrics collection service for WiFi sensing system.
Provides time-series metric tracking with aggregation support.
"""

from __future__ import annotations
import logging
import time
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


@dataclass
class MetricPoint:
    """Single metric data point."""
    timestamp: datetime
    value: float
    labels: Dict[str, str] = field(default_factory=dict)


@dataclass
class MetricSeries:
    """Time series of metric points with rolling window."""
    name: str
    description: str = ""
    unit: str = ""
    points: deque = field(default_factory=lambda: deque(maxlen=1000))

    def add_point(self, value: float, labels: Optional[Dict[str, str]] = None):
        """Add a metric data point."""
        point = MetricPoint(
            timestamp=datetime.now(timezone.utc),
            value=value,
            labels=labels or {}
        )
        self.points.append(point)

    def get_latest(self) -> Optional[MetricPoint]:
        """Get the most recent metric point."""
        return self.points[-1] if self.points else None

    def get_average(self, duration: timedelta) -> Optional[float]:
        """Get average value over a time duration."""
        cutoff = datetime.now(timezone.utc) - duration
        relevant = [p for p in self.points if p.timestamp >= cutoff]
        if not relevant:
            return None
        return sum(p.value for p in relevant) / len(relevant)

    def get_max(self, duration: timedelta) -> Optional[float]:
        """Get maximum value over a time duration."""
        cutoff = datetime.now(timezone.utc) - duration
        relevant = [p for p in self.points if p.timestamp >= cutoff]
        if not relevant:
            return None
        return max(p.value for p in relevant)

    def get_min(self, duration: timedelta) -> Optional[float]:
        """Get minimum value over a time duration."""
        cutoff = datetime.now(timezone.utc) - duration
        relevant = [p for p in self.points if p.timestamp >= cutoff]
        if not relevant:
            return None
        return min(p.value for p in relevant)

    def get_values(self) -> List[float]:
        """Get all values in the series."""
        return [p.value for p in self.points]


class MetricsService:
    """Service for collecting and managing application metrics."""

    def __init__(self, retention_points: int = 1000):
        self.retention_points = retention_points
        self._series: Dict[str, MetricSeries] = {}
        self._counters: Dict[str, float] = defaultdict(float)
        self._gauges: Dict[str, float] = {}
        self._start_time = time.time()

    def create_series(self, name: str, description: str = "", unit: str = "") -> MetricSeries:
        """Create a new metric series."""
        series = MetricSeries(
            name=name,
            description=description,
            unit=unit,
            points=deque(maxlen=self.retention_points)
        )
        self._series[name] = series
        return series

    def record_value(self, series_name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """Record a value in a metric series, creating it if needed."""
        if series_name not in self._series:
            self.create_series(series_name)
        self._series[series_name].add_point(value, labels)

    def increment_counter(self, name: str, amount: float = 1.0) -> None:
        """Increment a named counter."""
        self._counters[name] += amount

    def set_gauge(self, name: str, value: float) -> None:
        """Set a named gauge value."""
        self._gauges[name] = value

    def get_series(self, name: str) -> Optional[MetricSeries]:
        """Get a metric series by name."""
        return self._series.get(name)

    def get_all_series(self) -> Dict[str, MetricSeries]:
        """Get all metric series."""
        return dict(self._series)

    def get_system_metrics(self) -> Dict[str, float]:
        """Get current system metrics (CPU, memory, disk)."""
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=0.1)
            mem = psutil.virtual_memory().percent
            disk = psutil.disk_usage('/').percent
            return {"cpu_percent": cpu, "memory_percent": mem, "disk_percent": disk}
        except ImportError:
            logger.debug("psutil not available, skipping system metrics")
            return {}
        except Exception as e:
            logger.error(f"Error collecting system metrics: {e}")
            return {}

    def get_uptime(self) -> float:
        """Get service uptime in seconds."""
        return time.time() - self._start_time

    def get_summary(self) -> Dict[str, Any]:
        result = {}
        for name, s in self._series.items():
            latest = s.get_latest()
            result[name] = {
                "description": s.description,
                "unit": s.unit,
                "count": len(s.points),
                "latest": latest.value if latest else None
            }
        return {
            "series": result,
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "uptime_s": round(self.get_uptime(), 1),
        }

    def clear(self) -> None:
        """Clear all collected metrics."""
        self._series.clear()
        self._counters.clear()
        self._gauges.clear()
        self._start_time = time.time()
