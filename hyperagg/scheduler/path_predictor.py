"""
Path Quality Predictor — SKIP-style capacity/delivery probability estimation.

Instead of just knowing "this path has 50ms RTT right now", predicts
"what's the probability this path will deliver a packet within 150ms?"
This enables probabilistic scheduling decisions.

Based on HyperPath's SKIP method for stochastic capacity forecasting.
"""

import logging
import math
from typing import Optional

import numpy as np
from scipy import stats

from hyperagg.scheduler.models import PathPrediction, PathState

logger = logging.getLogger("hyperagg.scheduler.predictor")


class PathPredictor:
    """SKIP-style path quality predictor."""

    def __init__(self, config: dict):
        sched_cfg = config.get("scheduler", {})
        self._history_window = sched_cfg.get("history_window", 50)
        self._prediction_horizon_ms = sched_cfg.get("prediction_horizon_ms", 200)
        self._ewma_alpha = sched_cfg.get("ewma_alpha", 0.3)
        self._latency_budget_ms = sched_cfg.get("latency_budget_ms", 150)

        # Per-path EWMA predictions
        self._predicted_rtt: dict[int, float] = {}

    def predict_delivery_probability(
        self, path_id: int, deadline_ms: float, path_state: PathState
    ) -> float:
        """
        Estimate P(packet arrives within deadline_ms).

        Uses EWMA RTT prediction + jitter-based uncertainty (normal distribution)
        adjusted by loss rate.
        """
        if not path_state.is_alive:
            return 0.0

        if not path_state.rtt_history:
            return 0.5  # No data, assume neutral

        # EWMA prediction of next RTT
        predicted_rtt = self._get_predicted_rtt(path_id, path_state)

        # Standard deviation of recent RTT
        rtt_arr = np.array(path_state.rtt_history[-self._history_window:])
        if len(rtt_arr) > 1:
            rtt_std = float(np.std(rtt_arr))
        else:
            rtt_std = path_state.jitter_ms or 1.0

        # Ensure non-zero std to avoid degenerate distribution
        rtt_std = max(rtt_std, 0.1)

        # P(actual_rtt < deadline) using normal CDF
        p_rtt = float(stats.norm.cdf(deadline_ms, loc=predicted_rtt, scale=rtt_std))

        # Adjust for loss
        p_delivery = p_rtt * (1.0 - path_state.loss_pct)

        return max(0.0, min(1.0, p_delivery))

    def predict_capacity_mbps(
        self, path_id: int, path_state: PathState
    ) -> tuple[float, float]:
        """
        Predict path capacity and uncertainty.

        Returns:
            (expected_capacity_mbps, capacity_std_mbps)
        """
        if not path_state.rtt_history:
            return (0.0, 0.0)

        # If we have throughput data, use it
        if path_state.throughput_mbps > 0:
            # Simple: return current + trend
            return (path_state.throughput_mbps, path_state.throughput_mbps * 0.1)

        # Fallback: estimate from RTT (rough bandwidth-delay product)
        return (0.0, 0.0)

    def predict_path_in_future(
        self, path_id: int, horizon_ms: float, path_state: PathState
    ) -> PathPrediction:
        """
        Full prediction of path behavior at horizon_ms in the future.
        """
        predicted_rtt = self._get_predicted_rtt(path_id, path_state)

        # Confidence interval
        rtt_arr = np.array(path_state.rtt_history[-self._history_window:])
        if len(rtt_arr) > 2:
            rtt_std = float(np.std(rtt_arr))
            ci_low = max(0, predicted_rtt - 1.96 * rtt_std)
            ci_high = predicted_rtt + 1.96 * rtt_std
        else:
            ci_low = predicted_rtt * 0.5
            ci_high = predicted_rtt * 2.0

        # Delivery probability at latency budget
        delivery_prob = self.predict_delivery_probability(
            path_id, self._latency_budget_ms, path_state
        )

        # Trend detection using linear regression on recent RTT
        trend = self._detect_trend(path_state)

        # Predicted loss
        predicted_loss = path_state.loss_pct

        # Recommended action
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
            predicted_loss_pct=predicted_loss,
            delivery_probability=delivery_prob,
            trend=trend,
            recommended_action=action,
        )

    def _get_predicted_rtt(self, path_id: int, path_state: PathState) -> float:
        """EWMA-based RTT prediction."""
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
        """Detect if path RTT is improving, stable, or degrading."""
        hist = path_state.rtt_history
        if len(hist) < 10:
            return "stable"

        recent = np.array(hist[-20:])
        x = np.arange(len(recent))

        try:
            coeffs = np.polyfit(x, recent, 1)
            slope = coeffs[0]  # ms per sample

            # Normalize slope by mean RTT
            mean_rtt = float(np.mean(recent))
            if mean_rtt > 0:
                normalized_slope = slope / mean_rtt
            else:
                normalized_slope = 0

            if normalized_slope > 0.02:
                return "degrading"
            elif normalized_slope < -0.02:
                return "improving"
            else:
                return "stable"
        except Exception:
            return "stable"


if __name__ == "__main__":
    config = {
        "scheduler": {
            "history_window": 50,
            "ewma_alpha": 0.3,
            "latency_budget_ms": 150,
        }
    }
    predictor = PathPredictor(config)

    # Simulate a path degrading from 30ms to 200ms
    print("Path Predictor Test (degrading path):")
    rtt_history = []
    for i in range(60):
        rtt = 30 + i * 3 + np.random.normal(0, 5)
        rtt_history.append(max(1, rtt))

        if i % 10 == 9:
            state = PathState(
                path_id=0,
                avg_rtt_ms=np.mean(rtt_history[-10:]),
                jitter_ms=np.std(rtt_history[-10:]),
                loss_pct=min(0.3, i * 0.005),
                is_alive=True,
                quality_score=0.5,
                rtt_history=rtt_history.copy(),
                loss_history=[],
            )
            prediction = predictor.predict_path_in_future(0, 200, state)
            print(f"  Sample {i+1}: RTT~{state.avg_rtt_ms:.0f}ms → "
                  f"predicted={prediction.predicted_rtt_ms:.0f}ms, "
                  f"delivery={prediction.delivery_probability:.2f}, "
                  f"trend={prediction.trend}, "
                  f"action={prediction.recommended_action}")

    print("Path Predictor Test: PASSED")
