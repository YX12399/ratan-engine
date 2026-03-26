"""
Claude AI Optimizer — uses the Claude API to observe RATAN engine metrics
and recommend configuration changes for optimal network aggregation.

This agent periodically collects the full engine state (health, scheduling,
metrics, config history) and asks Claude to analyze the data and suggest
tuning changes. All changes flow through ConfigStore (source="ai_agent")
so they're versioned, auditable, and visible in the dashboard.

Once patterns stabilize, they can be codified into local rules that run
without API calls.
"""

import asyncio
import json
import time
import logging
import os
from typing import Optional
from collections import deque

logger = logging.getLogger("ratan.ai.optimizer")

SYSTEM_PROMPT = """\
You are an AI agent optimizing a multi-WAN network aggregation system called RATAN.

RATAN bonds Starlink (satellite) and Cellular (mobile) connections through a VPS tunnel.
You observe real-time metrics and recommend configuration changes to maximize performance.

## Your Goals (in priority order)
1. Maximize combined throughput across both paths
2. Keep effective RTT below {rtt_target_ms}ms
3. Keep packet loss below {loss_target_pct}%
4. Minimize failover events
5. Ensure sub-200ms failover when a path degrades

## Available Configuration Paths
- `mptcp_scheduling.scheduler_mode` — "weighted", "min_rtt", "redundant", "adaptive"
- `mptcp_scheduling.subflow_weights.starlink` — 0.0 to 1.0 (must sum to 1.0 with cellular)
- `mptcp_scheduling.subflow_weights.cellular` — 0.0 to 1.0
- `mptcp_scheduling.reinjection_threshold_ms` — 10 to 5000
- `path_health.probe_interval_ms` — 10 to 5000
- `path_health.rtt_threshold_ms` — 10 to 5000
- `path_health.loss_threshold_pct` — 0.1 to 100
- `failover.trigger_threshold` — 0.0 to 1.0
- `failover.switchback_hysteresis_ms` — 0 to 60000

## Rules
- Respond ONLY with a JSON object containing your recommended changes
- Use dot-notation config paths as keys
- Include a "reasoning" field explaining your logic
- Return empty changes `{}` if the current config is optimal
- Maximum {max_changes} config changes per response
- Be conservative — small incremental changes are better than large swings

## Response Format
```json
{{
  "changes": {{
    "config.path": value,
    ...
  }},
  "reasoning": "Brief explanation of why these changes will improve performance"
}}
```
"""

USER_PROMPT_TEMPLATE = """\
## Current Engine State (timestamp: {timestamp})

### Path Health
{health_data}

### Scheduling Directives
{directives}

### Aggregation Metrics
{agg_metrics}

### Current Configuration
{current_config}

### Recent Config Change History (last 10 changes)
{config_history}

### Your Past Recommendations & Outcomes
{past_outcomes}

Analyze the current state and recommend config changes (or confirm current config is optimal).
"""


class ClaudeOptimizer:
    """
    Background AI agent that observes RATAN metrics and recommends
    configuration changes via the Claude API.
    """

    def __init__(self, config_store, health_monitor, path_balancer, metrics_collector):
        self.config = config_store
        self.health = health_monitor
        self.balancer = path_balancer
        self.metrics = metrics_collector
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._client = None  # anthropic.AsyncAnthropic

        # Track recommendations and outcomes
        self._history: deque = deque(maxlen=100)
        self._last_recommendation: Optional[dict] = None
        self._last_recommendation_at: float = 0
        self._recommendation_count: int = 0
        self._error_count: int = 0

    async def start(self) -> None:
        """Start the optimization loop."""
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            logger.warning("ANTHROPIC_API_KEY not set — AI optimizer disabled")
            return

        try:
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=api_key)
        except ImportError:
            logger.error("anthropic package not installed — run: pip install anthropic")
            return

        self._running = True
        self._task = asyncio.create_task(self._optimization_loop())
        logger.info("Claude AI optimizer started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Claude AI optimizer stopped")

    async def _optimization_loop(self) -> None:
        """Periodic optimization cycle."""
        # Wait for initial data to accumulate
        await asyncio.sleep(10)

        while self._running:
            ai_cfg = self.config.get("ai_optimizer", {})
            if not ai_cfg.get("enabled", False):
                await asyncio.sleep(5)
                continue

            interval = ai_cfg.get("interval_sec", 60)
            try:
                await self._run_optimization_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._error_count += 1
                logger.error(f"AI optimization cycle failed: {e}", exc_info=True)

            await asyncio.sleep(interval)

    async def _run_optimization_cycle(self) -> None:
        """Execute one optimization cycle: collect state → ask Claude → apply changes."""
        state = self._collect_state()
        recommendation = await self._get_recommendation(state)

        if recommendation and recommendation.get("changes"):
            self._apply_changes(recommendation["changes"])
            self._record_outcome(state, recommendation)
            self._recommendation_count += 1
            logger.info(f"AI recommendation applied: {recommendation['reasoning']}")
        elif recommendation:
            logger.info(f"AI: no changes needed — {recommendation.get('reasoning', 'optimal')}")

    def _collect_state(self) -> dict:
        """Gather all current engine state for the AI agent."""
        return {
            "timestamp": time.time(),
            "health": self.health.get_all_health(),
            "directives": self.balancer.get_directives(),
            "agg_metrics": self.balancer.get_metrics(),
            "config": {
                "mptcp_scheduling": self.config.get("mptcp_scheduling"),
                "path_health": self.config.get("path_health"),
                "failover": self.config.get("failover"),
            },
            "config_history": self.config.get_history(
                since_version=max(0, self.config.version - 10),
                limit=10,
            ),
        }

    async def _get_recommendation(self, state: dict) -> Optional[dict]:
        """Call Claude API with current state, get recommended changes."""
        if not self._client:
            return None

        ai_cfg = self.config.get("ai_optimizer", {})
        model = ai_cfg.get("model", "claude-sonnet-4-20250514")
        rtt_target = ai_cfg.get("rtt_target_ms", 100)
        loss_target = ai_cfg.get("loss_target_pct", 2.0)
        max_changes = ai_cfg.get("max_changes_per_cycle", 3)

        system = SYSTEM_PROMPT.format(
            rtt_target_ms=rtt_target,
            loss_target_pct=loss_target,
            max_changes=max_changes,
        )

        # Format past outcomes
        past_outcomes = "None yet" if not self._history else json.dumps(
            list(self._history)[-5:], indent=2, default=str
        )

        user_msg = USER_PROMPT_TEMPLATE.format(
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            health_data=json.dumps(state["health"], indent=2, default=str),
            directives=json.dumps(state["directives"], indent=2, default=str),
            agg_metrics=json.dumps(state["agg_metrics"], indent=2, default=str),
            current_config=json.dumps(state["config"], indent=2, default=str),
            config_history=json.dumps(state["config_history"], indent=2, default=str),
            past_outcomes=past_outcomes,
        )

        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )

            # Parse response
            text = response.content[0].text
            # Extract JSON from response (handle markdown code blocks)
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]

            result = json.loads(text.strip())
            self._last_recommendation = result
            self._last_recommendation_at = time.time()
            return result

        except json.JSONDecodeError as e:
            logger.warning(f"AI returned invalid JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"Claude API call failed: {e}")
            return None

    def _apply_changes(self, changes: dict) -> None:
        """Apply recommended config changes through ConfigStore."""
        ai_cfg = self.config.get("ai_optimizer", {})
        max_changes = ai_cfg.get("max_changes_per_cycle", 3)

        applied = 0
        for path, value in changes.items():
            if applied >= max_changes:
                logger.warning(f"AI change limit reached ({max_changes}), skipping remaining")
                break

            try:
                self.config.set(path, value, source="ai_agent")
                applied += 1
                logger.info(f"AI set {path} = {value}")
            except Exception as e:
                logger.error(f"AI failed to set {path}: {e}")

    def _record_outcome(self, state_before: dict, recommendation: dict) -> None:
        """Record a recommendation and the state it was made in."""
        self._history.append({
            "timestamp": time.time(),
            "state_before": {
                "agg_metrics": state_before["agg_metrics"],
                "health_summary": {
                    pid: {"score": h.get("score"), "state": h.get("state"), "rtt": h.get("rtt_avg_ms")}
                    for pid, h in state_before["health"].items()
                },
            },
            "changes": recommendation.get("changes", {}),
            "reasoning": recommendation.get("reasoning", ""),
        })

    def get_status(self) -> dict:
        """Get optimizer status for API/dashboard."""
        ai_cfg = self.config.get("ai_optimizer", {})
        return {
            "enabled": ai_cfg.get("enabled", False),
            "running": self._running and self._client is not None,
            "model": ai_cfg.get("model", "claude-sonnet-4-20250514"),
            "interval_sec": ai_cfg.get("interval_sec", 60),
            "recommendation_count": self._recommendation_count,
            "error_count": self._error_count,
            "last_recommendation": self._last_recommendation,
            "last_recommendation_at": self._last_recommendation_at,
            "history_size": len(self._history),
        }

    def get_history(self, limit: int = 20) -> list[dict]:
        """Get recent recommendation history."""
        return list(self._history)[-limit:]

    async def force_recommendation(self) -> Optional[dict]:
        """Force an immediate optimization cycle. Returns the recommendation."""
        state = self._collect_state()
        recommendation = await self._get_recommendation(state)
        if recommendation and recommendation.get("changes"):
            self._apply_changes(recommendation["changes"])
            self._record_outcome(state, recommendation)
            self._recommendation_count += 1
        return recommendation
