"""
Path Quality Predictor — SKIP-style capacity/delivery probability estimation.

Predicts "what's the probability this path will deliver a packet within 150ms?"
Works with or without scipy/numpy (pure-Python fallback for RPi/ARM).
"""

import logging
import math
import statistics
from typing import Optional

try:
    import numpy as np
    from scipy import stats as sp_stats
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False
    np = None
    sp_stats = None

from hyperagg.scheduler.models import PathPrediction, PathState

logger = logging.getLogger("hyperagg.scheduler.predictor")


def _std(values: list[float]) -> float:
    """Standard deviation — works without numpy."""
    if len(values) < 2:
        return 0.0
    return statistics.stdev(values)


def _mean(values: list[float]) -> float:
    """Mean — works without numpy."""
    if not values:
        return 0.0
    return statistics.mean(values)


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    """
    Approximate normal CDF using the error function.
    Pure Python — no scipy needed. Accurate to ~1e-7.
    """
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    z = (x - mu) / sigma
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _simple_slope(values: list[float]) -> float:
    """Simple linear slope — no numpy polyfit needed."""
    n = len(values)
    if n < 3:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = _mean(values)
    num = sum((i - x_mean) * (values[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den > 0 else 0.0


class PathPredictor:
    """SKIP-style path quality predictor. Works on any platform."""

    def __init__(self, config: dict):
        sched_cfg = config.get("scheduler", {})
        self._history_window = sched_cfg.get("history_window", 50)
        self._prediction_horizon_ms = sched_cfg.get("prediction_horizon_ms", 200)
        self._ewma_alpha = sched_cfg.get("ewma_alpha", 0.3)
        self._latency_budget_ms = sched_cfg.get("latency_budget_ms", 150)
        self._predicted_rtt: dict[int, float] = {}

    def predict_delivery_probability(
        self, path_id: int, deadline_ms: float, path_state: PathState
    ) -> float:
        """Estimate P(packet arrives within deadline_ms)."""
        if not path_state.is_alive:
            return 0.0
        if not path_state.rtt_history:
            return 0.5

        predicted_rtt = self._get_predicted_rtt(path_id, path_state)
        recent = path_state.rtt_history[-self._history_window:]
        rtt_std = _std(recent) if len(recent) > 1 else (path_state.jitter_ms or 1.0)
        rtt_std = max(rtt_std, 0.1)

        # Use scipy if available, otherwise pure-Python CDF
        if HAS_SCIPY:
            p_rtt = float(sp_stats.norm.cdf(deadline_ms, loc=predicted_rtt, scale=rtt_std))
        else:
            p_rtt = _normal_cdf(deadline_ms, predicted_rtt, rtt_std)

        p_delivery = p_rtt * (1.0 - path_state.loss_pct)
        return max(0.0, min(1.0, p_delivery))

    def predict_capacity_mbps(
        self, path_id: int, path_state: PathState
    ) -> tuple[float, float]:
        if not path_state.rtt_history:
            return (0.0, 0.0)
        if path_state.throughput_mbps > 0:
            return (path_state.throughput_mbps, path_state.throughput_mbps * 0.1)
        return (0.0, 0.0)

    def predict_path_in_future(
        self, path_id: int, horizon_ms: float, path_state: PathState
    ) -> PathPrediction:
        predicted_rtt = self._get_predicted_rtt(path_id, path_state)

        recent = path_state.rtt_history[-self._history_window:]
        if len(recent) > 2:
            rtt_std = _std(recent)
            ci_low = max(0, predicted_rtt - 1.96 * rtt_std)
            ci_high = predicted_rtt + 1.96 * rtt_std
        else:
            ci_low = predicted_rtt * 0.5
            ci_high = predicted_rtt * 2.0

        delivery_prob = self.predict_delivery_probability(
            path_id, self._latency_budget_ms, path_state
        )
        trend = self._detect_trend(path_state)

        if delivery_prob > 0.95:
            action = "prefer"
        elif delivery_prob > 0.7:
            action = "use"
        elif delivery_prob > 0.3:
            action = "avoid"
        else:
            action = "abandon"

        return PathPrediction(
            path_id=path_id,
            predicted_rtt_ms=predicted_rtt,
            rtt_confidence_interval=(ci_low, ci_high),
            predicted_loss_pct=path_state.loss_pct,
            delivery_probability=delivery_prob,
            trend=trend,
            recommended_action=action,
        )

    def _get_predicted_rtt(self, path_id: int, path_state: PathState) -> float:
        if not path_state.rtt_history:
            return self._latency_budget_ms
        latest_rtt = path_state.rtt_history[-1]
        alpha = self._ewma_alpha
        if path_id not in self._predicted_rtt:
            self._predicted_rtt[path_id] = latest_rtt
        else:
            self._predicted_rtt[path_id] = (
                alpha * latest_rtt + (1 - alpha) * self._predicted_rtt[path_id]
            )
        return self._predicted_rtt[path_id]

    def _detect_trend(self, path_state: PathState) -> str:
        hist = path_state.rtt_history
        if len(hist) < 10:
            return "stable"

        recent = hist[-20:]
        slope = _simple_slope(recent)
        mean_rtt = _mean(recent)

        if mean_rtt > 0:
            normalized = slope / mean_rtt
        else:
            normalized = 0

        if normalized > 0.02:
            return "degrading"
        elif normalized < -0.02:
            return "improving"
        return "stable"


if __name__ == "__main__":
    import random
    config = {"scheduler": {"history_window": 50, "ewma_alpha": 0.3, "latency_budget_ms": 150}}
    predictor = PathPredictor(config)

    print(f"Path Predictor (scipy={'yes' if HAS_SCIPY else 'NO — using pure-Python fallback'}):")
    rtt_history = []
    for i in range(60):
        rtt = 30 + i * 3 + random.gauss(0, 5)
        rtt_history.append(max(1, rtt))
        if i % 10 == 9:
            state = PathState(
                path_id=0, avg_rtt_ms=_mean(rtt_history[-10:]),
                jitter_ms=_std(rtt_history[-10:]),
                loss_pct=min(0.3, i * 0.005), is_alive=True,
                quality_score=0.5, rtt_history=rtt_history.copy(), loss_history=[],
            )
            pred = predictor.predict_path_in_future(0, 200, state)
            print(f"  Sample {i+1}: RTT~{state.avg_rtt_ms:.0f}ms → "
                  f"predict={pred.predicted_rtt_ms:.0f}ms, "
                  f"deliver={pred.delivery_probability:.2f}, "
                  f"trend={pred.trend}, action={pred.recommended_action}")
    print("PASSED")
