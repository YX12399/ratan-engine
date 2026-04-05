"""
AI Chat Handler — Claude-powered network analysis and recommendations.

Gathers current system state, sends it as context to Claude API,
returns analysis + optional config change suggestions.

The suggested_changes format uses {endpoint, body, description} so the
dashboard can apply changes directly via POST.
"""

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("hyperagg.ai.chat")

# Action → API endpoint mapping
ACTION_MAP = {
    "set_scheduler_mode": lambda v: ("/api/scheduler/mode", {"mode": v}),
    "set_fec_mode": lambda v: ("/api/fec/mode", {"mode": v}),
    "force_path": lambda v: ("/api/force-path", {"path_id": int(v)}),
    "release_path": lambda v: ("/api/release-path", {}),
}

SYSTEM_PROMPT = """\
You are the AI analyst for RATAN HyperAgg, a packet-level network bonding engine.

HyperAgg bonds multiple WAN paths (typically Starlink satellite + cellular) through \
a UDP tunnel to a VPS. It uses per-packet AI scheduling, Forward Error Correction (FEC), \
and real-time path quality prediction to achieve zero-disruption failover.

## Current System State
{state_json}

## What You Can Do
Analyze the state data and provide clear, actionable advice. If you recommend changes, \
include them as structured actions the dashboard can apply with one click.

## Available Actions
- set_scheduler_mode: "ai", "weighted", "round_robin", "replicate"
- set_fec_mode: "auto", "xor", "reed_solomon", "replicate", "none"
- force_path: 0 or 1 (force all traffic to one path)
- release_path: true (return to AI scheduling)

## Response Format
Always respond with valid JSON:
{{
  "analysis": "Your analysis in markdown. Reference specific numbers.",
  "suggested_changes": [
    {{
      "endpoint": "/api/scheduler/mode",
      "body": {{"mode": "weighted"}},
      "description": "Switch to weighted scheduling because..."
    }}
  ]
}}

If no changes are needed, use an empty suggested_changes array.
Keep analysis concise. Use **bold** for key metrics.
"""


class ChatHandler:
    """Handles AI chat requests using the Anthropic Claude API."""

    def __init__(self, sdn_controller, api_key: Optional[str] = None):
        self._sdn = sdn_controller
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = None
        self._history: list[dict] = []
        self._conversation: list[dict] = []  # Multi-turn context
        self._model = os.environ.get("HYPERAGG_AI_MODEL", "claude-sonnet-4-20250514")

        if self._api_key:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self._api_key)
                logger.info(f"AI chat enabled (model: {self._model})")
            except ImportError:
                logger.warning("anthropic package not installed — AI chat disabled")
        else:
            logger.info("No ANTHROPIC_API_KEY — AI chat disabled (mock responses)")

    async def chat(self, user_message: str) -> dict:
        """
        Process a chat message. Returns:
            {"analysis": str, "suggested_changes": [{"endpoint", "body", "description"}]}
        """
        state = self._sdn.get_system_state()
        state_json = json.dumps(state, indent=2, default=str)
        system = SYSTEM_PROMPT.format(state_json=state_json)

        if self._client:
            response = await self._call_claude(system, user_message)
        else:
            response = self._mock_response(user_message, state)

        # Normalize suggested_changes to always have endpoint+body
        response["suggested_changes"] = self._normalize_changes(
            response.get("suggested_changes", [])
        )

        self._history.append({
            "timestamp": time.time(),
            "user": user_message,
            "response": response,
        })
        if len(self._history) > 100:
            self._history = self._history[-50:]

        return response

    async def _call_claude(self, system: str, user_message: str) -> dict:
        """Call the Anthropic API with conversation context."""
        try:
            import asyncio

            # Build messages with last 3 turns for context
            messages = []
            for turn in self._conversation[-3:]:
                messages.append({"role": "user", "content": turn["user"]})
                messages.append({"role": "assistant", "content": turn["assistant"]})
            messages.append({"role": "user", "content": user_message})

            loop = asyncio.get_event_loop()
            message = await loop.run_in_executor(
                None,
                lambda: self._client.messages.create(
                    model=self._model,
                    max_tokens=2048,
                    system=system,
                    messages=messages,
                ),
            )

            text = message.content[0].text

            # Save for conversation context
            self._conversation.append({"user": user_message, "assistant": text})
            if len(self._conversation) > 10:
                self._conversation = self._conversation[-5:]

            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"analysis": text, "suggested_changes": []}

        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return {"analysis": f"AI analysis unavailable: {e}", "suggested_changes": []}

    @staticmethod
    def _normalize_changes(changes: list) -> list:
        """Ensure every change has endpoint + body + description (for dashboard Apply)."""
        normalized = []
        for c in changes:
            if not isinstance(c, dict):
                continue
            # If it already has endpoint+body, use as-is
            if "endpoint" in c and "body" in c:
                c.setdefault("description", f"{c['endpoint']} → {json.dumps(c['body'])}")
                normalized.append(c)
                continue
            # Convert action+value format to endpoint+body
            action = c.get("action", "")
            value = c.get("value", "")
            if action in ACTION_MAP:
                endpoint, body = ACTION_MAP[action](value)
                normalized.append({
                    "endpoint": endpoint,
                    "body": body,
                    "description": c.get("reason", c.get("description", f"{action}: {value}")),
                })
        return normalized

    def _mock_response(self, user_message: str, state: dict) -> dict:
        """Generate a response from live metrics when no API key."""
        paths = state.get("paths", {})
        metrics = state.get("metrics", {})
        fec = state.get("fec", {})
        scheduler = state.get("scheduler", {})

        parts = ["**Network Status Summary**\n"]
        suggestions = []

        worst_path = None
        worst_score = 1.0
        best_path = None
        best_score = 0.0

        for pid, p in paths.items():
            name = "Starlink" if str(pid) == "0" else "Cellular"
            rtt = p.get("avg_rtt_ms", 0)
            loss = p.get("loss_pct", 0)
            score = p.get("quality_score", 1)
            alive = p.get("is_alive", False)
            pred = p.get("prediction", {})

            status = "UP" if alive else "**DOWN**"
            parts.append(
                f"- **{name}** ({status}): RTT=**{rtt}ms**, Loss=**{loss}%**, "
                f"Score=**{score}**, Trend={pred.get('trend', '?')}"
            )

            if score < worst_score:
                worst_score = score
                worst_path = (pid, name)
            if score > best_score:
                best_score = score
                best_path = (pid, name)

        throughput = metrics.get("aggregate_throughput_mbps", 0)
        eff_loss = metrics.get("effective_loss_pct", 0)
        fec_mode = fec.get("current_mode", "?")
        recoveries = fec.get("total_recoveries", 0)
        sched_mode = scheduler.get("mode", metrics.get("scheduler_mode", "?"))

        parts.append(
            f"\n**Aggregate**: {throughput} Mbps throughput, {eff_loss}% effective loss"
        )
        parts.append(f"**Config**: Scheduler={sched_mode}, FEC={fec_mode} ({recoveries} recoveries)")

        # Generate actionable suggestions based on actual metrics
        if worst_path and worst_score < 0.5 and best_path:
            suggestions.append({
                "endpoint": "/api/force-path",
                "body": {"path_id": int(best_path[0])},
                "description": f"Force traffic to {best_path[1]} — {worst_path[1]} score is critically low ({worst_score:.2f})",
            })

        if eff_loss > 2 and fec_mode in ("none", "xor"):
            suggestions.append({
                "endpoint": "/api/fec/mode",
                "body": {"mode": "reed_solomon"},
                "description": f"Upgrade FEC to Reed-Solomon — effective loss is {eff_loss}%",
            })

        if sched_mode != "ai" and worst_score > 0.7 and best_score > 0.7:
            suggestions.append({
                "endpoint": "/api/scheduler/mode",
                "body": {"mode": "ai"},
                "description": "Both paths healthy — switch to AI scheduler for optimal distribution",
            })

        if not suggestions:
            parts.append("\nAll paths performing well. No changes recommended.")

        parts.append(
            "\n*Mock analysis from live metrics. Set ANTHROPIC_API_KEY for full AI analysis.*"
        )

        return {"analysis": "\n".join(parts), "suggested_changes": suggestions}

    def get_history(self, last_n: int = 20) -> list[dict]:
        return self._history[-last_n:]

    @property
    def is_enabled(self) -> bool:
        return self._client is not None
