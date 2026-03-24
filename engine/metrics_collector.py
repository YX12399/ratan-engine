"""
Metrics Collector — records time-series data from all engine components.
Stores in-memory with configurable retention, exposes for dashboard consumption.
Correlates config changes with metric shifts.
"""

import time
import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional


@dataclass
class MetricPoint:
    timestamp: float
    name: str
    value: float
    tags: dict


class MetricsCollector:
    """
    In-memory time-series metrics store.
    Each metric is a named series with tags (e.g., path_id).
    """

    def __init__(self, config_store):
        self.config = config_store
        self._series: dict[str, deque] = {}
        self._lock = threading.Lock()
        self._running = False
        self._collection_thread: Optional[threading.Thread] = None

        # Config change markers — appear in metric queries for correlation
        self._config_markers: deque = deque(maxlen=1000)
        config_store.subscribe(self._on_config_change)

    def record(self, name: str, value: float, tags: Optional[dict] = None):
        """Record a single metric point."""
        with self._lock:
            key = self._series_key(name, tags or {})
            if key not in self._series:
                retention = self.config.get("monitoring.history_retention_hours", 168)
                # Estimate max points: retention_hours * 3600 / flush_interval_sec
                flush_interval = self.config.get("monitoring.metrics_flush_interval_ms", 5000) / 1000
                max_points = int(retention * 3600 / max(flush_interval, 1))
                self._series[key] = deque(maxlen=max(max_points, 1000))

            self._series[key].append(MetricPoint(
                timestamp=time.time(),
                name=name,
                value=value,
                tags=tags or {},
            ))

    def query(self, name: str, tags: Optional[dict] = None,
              since: Optional[float] = None, limit: int = 1000) -> list[dict]:
        """Query metrics for a named series."""
        with self._lock:
            key = self._series_key(name, tags or {})
            series = self._series.get(key, deque())

            results = []
            for point in series:
                if since and point.timestamp < since:
                    continue
                results.append({
                    "timestamp": point.timestamp,
                    "value": point.value,
                    "tags": point.tags,
                })
                if len(results) >= limit:
                    break

            return results

    def query_latest(self, name: str, tags: Optional[dict] = None) -> Optional[dict]:
        """Get most recent value for a metric."""
        with self._lock:
            key = self._series_key(name, tags or {})
            series = self._series.get(key)
            if series and len(series) > 0:
                p = series[-1]
                return {"timestamp": p.timestamp, "value": p.value, "tags": p.tags}
        return None

    def list_series(self) -> list[str]:
        """List all metric series names."""
        with self._lock:
            return list(self._series.keys())

    def get_config_markers(self, since: Optional[float] = None) -> list[dict]:
        """Get config change markers for correlation overlay."""
        markers = list(self._config_markers)
        if since:
            markers = [m for m in markers if m["timestamp"] > since]
        return markers

    def get_dashboard_snapshot(self) -> dict:
        """
        Get a full snapshot of current metrics for dashboard consumption.
        Returns latest values for all key metrics.
        """
        snapshot = {
            "timestamp": time.time(),
            "paths": {},
            "aggregation": {},
            "system": {},
        }

        with self._lock:
            # Collect latest per-path metrics
            for key, series in self._series.items():
                if not series:
                    continue
                latest = series[-1]

                if "path_health" in latest.name:
                    pid = latest.tags.get("path_id", "unknown")
                    if pid not in snapshot["paths"]:
                        snapshot["paths"][pid] = {}
                    snapshot["paths"][pid][latest.name] = latest.value

                elif "aggregation" in latest.name:
                    snapshot["aggregation"][latest.name] = latest.value

                elif "system" in latest.name:
                    snapshot["system"][latest.name] = latest.value

        return snapshot

    def start_collection(self, health_monitor, path_balancer):
        """Start periodic metric collection from engine components."""
        self._running = True

        def collect_loop():
            while self._running:
                interval = self.config.get("monitoring.metrics_flush_interval_ms", 5000) / 1000

                # Collect path health metrics
                for pid, health in health_monitor.get_all_health().items():
                    tags = {"path_id": pid}
                    self.record("path_health.score", health.get("score", 0), tags)
                    self.record("path_health.rtt_avg", health.get("rtt_avg_ms", 0), tags)
                    self.record("path_health.rtt_p95", health.get("rtt_p95_ms", 0), tags)
                    self.record("path_health.loss_pct", health.get("loss_pct", 0), tags)
                    self.record("path_health.jitter", health.get("jitter_ms", 0), tags)

                # Collect aggregation metrics
                agg = path_balancer.get_metrics()
                self.record("aggregation.throughput", agg.get("combined_throughput_mbps", 0))
                self.record("aggregation.effective_rtt", agg.get("effective_rtt_ms", 0))
                self.record("aggregation.active_paths", agg.get("active_paths", 0))
                self.record("aggregation.reinjection_rate", agg.get("reinjection_rate_pct", 0))

                # Collect scheduling directives
                for pid, directive in path_balancer.get_directives().items():
                    self.record("scheduling.weight", directive.get("weight", 0), {"path_id": pid})

                time.sleep(interval)

        self._collection_thread = threading.Thread(target=collect_loop, daemon=True, name="metrics-collector")
        self._collection_thread.start()

    def stop(self):
        self._running = False

    def _series_key(self, name: str, tags: dict) -> str:
        tag_str = ",".join(f"{k}={v}" for k, v in sorted(tags.items()))
        return f"{name}|{tag_str}" if tag_str else name

    def _on_config_change(self, change):
        self._config_markers.append({
            "timestamp": change.timestamp,
            "path": change.path,
            "old_value": change.old_value,
            "new_value": change.new_value,
            "version": change.version,
        })
