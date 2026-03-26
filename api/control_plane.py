"""
Control Plane API — FastAPI REST endpoints for all tunable variables.
This is the single interface through which the dashboard, Cowork bridge,
and any external tool configures and monitors the RATAN engine.

Every config change is versioned. Every metric is queryable.
CORS is wide open for dashboard access — secure with token in production.
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Any, Optional
import os
import time

APP_VERSION = os.environ.get("RATAN_APP_VERSION", "1.0.0")
CORS_ORIGINS = os.environ.get("RATAN_CORS_ORIGINS", "*").split(",")


def create_app(config_store, health_monitor, path_balancer, metrics_collector, tunnel_server=None):
    app = FastAPI(
        title="RATAN Control Plane",
        description="Resilient Aggregation and Transition for Advanced Networking",
        version=APP_VERSION,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Health ────────────────────────────────────────────────────────

    @app.get("/health")
    async def health_check():
        return {
            "status": "ok",
            "timestamp": time.time(),
            "config_version": config_store.version,
            "paths": health_monitor.get_all_health(),
        }

    # ── Configuration ─────────────────────────────────────────────────

    @app.get("/config")
    async def get_config(path: str = ""):
        """Get full config or a specific path."""
        value = config_store.get(path)
        if value is None:
            raise HTTPException(404, f"Config path not found: {path}")
        return {"version": config_store.version, "path": path or "(root)", "value": value}

    class ConfigUpdate(BaseModel):
        path: str
        value: Any
        source: str = "api"

    @app.put("/config")
    async def set_config(update: ConfigUpdate):
        """Set a single config value."""
        change = config_store.set(update.path, update.value, update.source)
        return {
            "version": change.version,
            "path": change.path,
            "old_value": change.old_value,
            "new_value": change.new_value,
        }

    class BatchConfigUpdate(BaseModel):
        updates: dict[str, Any]
        source: str = "api"

    @app.put("/config/batch")
    async def batch_set_config(batch: BatchConfigUpdate):
        """Set multiple config values atomically."""
        changes = config_store.batch_set(batch.updates, batch.source)
        return {
            "version": config_store.version,
            "changes": [
                {"path": c.path, "old_value": c.old_value, "new_value": c.new_value}
                for c in changes
            ],
        }

    @app.post("/config/reset")
    async def reset_config():
        """Reset all config to defaults."""
        config_store.reset_to_defaults()
        return {"status": "reset", "version": config_store.version}

    @app.get("/config/history")
    async def config_history(since_version: int = 0, limit: int = 100):
        """Get config change history."""
        return {"history": config_store.get_history(since_version, limit)}

    # ── Path Health Variables ─────────────────────────────────────────

    @app.get("/config/path-health")
    async def get_path_health_config():
        """Get all path health tuning variables."""
        return config_store.get("path_health")

    @app.put("/config/path-health/probe-interval")
    async def set_probe_interval(value: int = Query(..., ge=10, le=5000)):
        change = config_store.set("path_health.probe_interval_ms", value, "dashboard")
        return {"set": change.new_value, "previous": change.old_value}

    @app.put("/config/path-health/rtt-threshold")
    async def set_rtt_threshold(value: int = Query(..., ge=10, le=5000)):
        change = config_store.set("path_health.rtt_threshold_ms", value, "dashboard")
        return {"set": change.new_value, "previous": change.old_value}

    @app.put("/config/path-health/zombie-timeout")
    async def set_zombie_timeout(value: int = Query(..., ge=100, le=30000)):
        change = config_store.set("path_health.zombie_timeout_ms", value, "dashboard")
        return {"set": change.new_value, "previous": change.old_value}

    @app.put("/config/path-health/loss-threshold")
    async def set_loss_threshold(value: float = Query(..., ge=0.1, le=100)):
        change = config_store.set("path_health.loss_threshold_pct", value, "dashboard")
        return {"set": change.new_value, "previous": change.old_value}

    @app.put("/config/path-health/weights")
    async def set_health_weights(rtt: float = 0.4, loss: float = 0.35, jitter: float = 0.25):
        total = rtt + loss + jitter
        if abs(total - 1.0) > 0.01:
            raise HTTPException(400, f"Weights must sum to 1.0, got {total}")
        change = config_store.set("path_health.health_score_weights",
                                   {"rtt": rtt, "loss": loss, "jitter": jitter}, "dashboard")
        return {"set": change.new_value}

    # ── MPTCP Scheduling Variables ────────────────────────────────────

    @app.get("/config/scheduling")
    async def get_scheduling_config():
        return config_store.get("mptcp_scheduling")

    @app.put("/config/scheduling/mode")
    async def set_scheduler_mode(mode: str = Query(..., pattern="^(weighted|min_rtt|redundant|adaptive)$")):
        change = config_store.set("mptcp_scheduling.scheduler_mode", mode, "dashboard")
        return {"set": change.new_value, "previous": change.old_value}

    @app.put("/config/scheduling/weights")
    async def set_subflow_weights(starlink: float = 0.7, cellular: float = 0.3):
        total = starlink + cellular
        if abs(total - 1.0) > 0.01:
            raise HTTPException(400, f"Weights must sum to 1.0, got {total}")
        change = config_store.set("mptcp_scheduling.subflow_weights",
                                   {"starlink": starlink, "cellular": cellular}, "dashboard")
        return {"set": change.new_value}

    @app.put("/config/scheduling/reinjection-threshold")
    async def set_reinjection(value: int = Query(..., ge=10, le=5000)):
        change = config_store.set("mptcp_scheduling.reinjection_threshold_ms", value, "dashboard")
        return {"set": change.new_value, "previous": change.old_value}

    @app.put("/config/scheduling/promotion-threshold")
    async def set_promotion(value: float = Query(..., ge=0, le=1)):
        change = config_store.set("mptcp_scheduling.path_promotion_threshold", value, "dashboard")
        return {"set": change.new_value}

    @app.put("/config/scheduling/demotion-threshold")
    async def set_demotion(value: float = Query(..., ge=0, le=1)):
        change = config_store.set("mptcp_scheduling.path_demotion_threshold", value, "dashboard")
        return {"set": change.new_value}

    @app.put("/config/scheduling/bandwidth-target")
    async def set_bandwidth_target(value: float = Query(..., ge=1, le=10000)):
        change = config_store.set("mptcp_scheduling.bandwidth_target_mbps", value, "dashboard")
        return {"set": change.new_value}

    # ── Failover Variables ────────────────────────────────────────────

    @app.get("/config/failover")
    async def get_failover_config():
        return config_store.get("failover")

    @app.put("/config/failover/trigger-threshold")
    async def set_failover_trigger(value: float = Query(..., ge=0, le=1)):
        change = config_store.set("failover.trigger_threshold", value, "dashboard")
        return {"set": change.new_value}

    @app.put("/config/failover/recovery-interval")
    async def set_recovery_interval(value: int = Query(..., ge=100, le=30000)):
        change = config_store.set("failover.recovery_probe_interval_ms", value, "dashboard")
        return {"set": change.new_value}

    @app.put("/config/failover/hysteresis")
    async def set_hysteresis(value: int = Query(..., ge=0, le=60000)):
        change = config_store.set("failover.switchback_hysteresis_ms", value, "dashboard")
        return {"set": change.new_value}

    # ── Live Engine State ─────────────────────────────────────────────

    @app.get("/paths")
    async def get_paths():
        """Get health status of all network paths."""
        return health_monitor.get_all_health()

    @app.get("/paths/{path_id}")
    async def get_path(path_id: str):
        health = health_monitor.get_health(path_id)
        if not health:
            raise HTTPException(404, f"Path not found: {path_id}")
        return health

    @app.get("/scheduling/directives")
    async def get_directives():
        """Get current scheduling directives for all paths."""
        return path_balancer.get_directives()

    @app.get("/scheduling/metrics")
    async def get_aggregation_metrics():
        """Get aggregation performance metrics."""
        return path_balancer.get_metrics()

    @app.post("/scheduling/rebalance")
    async def force_rebalance():
        """Force an immediate scheduling rebalance."""
        path_balancer.rebalance()
        return {"status": "rebalanced", "directives": path_balancer.get_directives()}

    # ── Sessions ──────────────────────────────────────────────────────

    @app.get("/sessions")
    async def get_sessions():
        """Get connected client sessions."""
        if tunnel_server:
            return tunnel_server.get_sessions()
        return {}

    # ── Metrics / Time Series ─────────────────────────────────────────

    @app.get("/metrics/series")
    async def list_metric_series():
        """List all available metric series."""
        return {"series": metrics_collector.list_series()}

    @app.get("/metrics/query")
    async def query_metrics(
        name: str,
        path_id: Optional[str] = None,
        since: Optional[float] = None,
        limit: int = 1000,
    ):
        """Query time-series metrics."""
        tags = {"path_id": path_id} if path_id else None
        data = metrics_collector.query(name, tags, since, limit)
        markers = metrics_collector.get_config_markers(since)
        return {"name": name, "data": data, "config_markers": markers}

    @app.get("/metrics/dashboard")
    async def dashboard_snapshot():
        """Get full dashboard snapshot — all latest metrics in one call."""
        return metrics_collector.get_dashboard_snapshot()

    # ── Test Orchestration ────────────────────────────────────────────

    @app.get("/config/testing")
    async def get_test_config():
        return config_store.get("test_orchestration")

    class ABTestDefinition(BaseModel):
        name: str
        duration_sec: int = 300
        variants: dict[str, dict[str, Any]]  # variant_name -> {config_path: value}
        metric: str = "aggregation.throughput"

    @app.post("/tests/ab")
    async def start_ab_test(test_def: ABTestDefinition):
        """Start an A/B test with different config variants."""
        # TODO: Implement full A/B test runner
        return {
            "status": "created",
            "test": test_def.name,
            "variants": list(test_def.variants.keys()),
            "duration_sec": test_def.duration_sec,
        }

    return app
