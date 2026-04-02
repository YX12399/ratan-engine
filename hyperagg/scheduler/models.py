"""
Data models for the HyperAgg scheduler — path state, predictions, and decisions.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class PathMeasurement:
    """Single probe/measurement result for a path."""
    path_id: int
    timestamp: float        # time.monotonic()
    rtt_ms: float
    jitter_ms: float        # |current_rtt - previous_rtt|
    loss_detected: bool
    throughput_bytes: int = 0
    queue_depth: int = 0


@dataclass
class PathState:
    """Current state of a network path (computed from recent measurements)."""
    path_id: int
    avg_rtt_ms: float = 0.0
    min_rtt_ms: float = 0.0
    max_rtt_ms: float = 0.0
    jitter_ms: float = 0.0       # EWMA of absolute jitter
    loss_pct: float = 0.0        # losses / probes in last window
    throughput_mbps: float = 0.0
    is_alive: bool = True
    quality_score: float = 1.0   # 0.0 to 1.0, weighted composite
    rtt_history: list[float] = field(default_factory=list)
    loss_history: list[bool] = field(default_factory=list)


@dataclass(slots=True)
class PathPrediction:
    """AI prediction for a path's future behavior."""
    path_id: int
    predicted_rtt_ms: float
    rtt_confidence_interval: tuple[float, float]  # 95% CI
    predicted_loss_pct: float
    delivery_probability: float   # P(packet arrives within deadline)
    trend: str                    # "improving" | "stable" | "degrading"
    recommended_action: str       # "prefer" | "use" | "avoid" | "abandon"


@dataclass(slots=True)
class PathAssignment:
    """Per-path decision for a single packet."""
    path_id: int
    send: bool
    is_fec_parity: bool = False
    priority: int = 0            # lower = send first


@dataclass
class SchedulerDecision:
    """Complete scheduling decision for one packet."""
    packet_global_seq: int
    assignments: list[PathAssignment]
    reason: str


@dataclass
class SchedulerStats:
    """Scheduler performance statistics."""
    decisions_total: int = 0
    decisions_by_reason: dict[str, int] = field(default_factory=dict)
    avg_decision_time_us: float = 0.0
    path_utilization: dict[int, float] = field(default_factory=dict)
    current_mode: str = "ai"
    current_best_path: Optional[int] = None
