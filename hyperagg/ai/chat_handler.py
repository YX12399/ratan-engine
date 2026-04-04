"""
AI Chat Handler — Claude-powered network analysis and recommendations.

Gathers current system state, sends it as context to Claude API,
returns analysis + optional config change suggestions.
"""

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("hyperagg.ai.chat")

SYSTEM_PROMPT = """\
You are the AI analyst for RATAN HyperAgg, a packet-level network bonding engine.

HyperAgg bonds multiple WAN paths (typically Starlink satellite + cellular) through \
a UDP tunnel to a VPS. It uses per-packet AI scheduling, Forward Error Correction (FEC), \
and real-time path quality prediction to achieve zero-disruption failover.

## Current System State
{state_json}

## What You Can Do
When the user asks about their network, analyze the state data above and provide:
1. Clear explanations of what's happening and why
2. Specific recommendations with reasoning
3. If recommending config changes, include them in a structured format

## Available Config Changes
You can recommend these changes (the dashboard will show an "Apply" button):
- scheduler_mode: "ai", "weighted", "round_robin", "replicate"
- fec_mode: "auto", "xor", "reed_solomon", "replicate", "none"
- force_path: 0, 1, etc. (force all traffic to one path for testing)
- release_path: true (return to AI scheduling)

## Response Format
Always respond with JSON:
{{
  "analysis": "Your human-readable analysis text (use markdown for formatting)",
  "suggested_changes": [
    {{"action": "set_scheduler_mode", "value": "weighted", "reason": "..."}},
    {{"action": "set_fec_mode", "value": "reed_solomon", "reason": "..."}}
  ]
}}

If no changes are needed, return an empty suggested_changes array.
Keep analysis concise but actionable. Reference specific numbers from the state data.
"""


class ChatHandler:
    """Handles AI chat requests using the Anthropic Claude API."""

    def __init__(self, sdn_controller, api_key: Optional[str] = None):
        self._sdn = sdn_controller
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = None
        self._history: list[dict] = []
        self._model = os.environ.get("HYPERAGG_AI_MODEL", "claude-sonnet-4-20250514")

        if self._api_key:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self._api_key)
                logger.info(f"AI chat enabled (model: {self._model})")
            except ImportError:
                logger.warning("anthropic package not installed — AI chat disabled")
        else:
            logger.info("No ANTHROPIC_API_KEY — AI chat disabled (will return mock responses)")

    async def chat(self, user_message: str) -> dict:
        """
        Process a chat message.

        Returns:
            {"analysis": str, "suggested_changes": list[dict]}
        """
        # Gather current state
        state = self._sdn.get_system_state()
        state_json = json.dumps(state, indent=2, default=str)

        # Build system prompt with current state
        system = SYSTEM_PROMPT.format(state_json=state_json)

        if self._client:
            response = await self._call_claude(system, user_message)
        else:
            response = self._mock_response(user_message, state)

        # Log the interaction
        self._history.append({
            "timestamp": time.time(),
            "user": user_message,
            "response": response,
        })
        if len(self._history) > 100:
            self._history = self._history[-50:]

        return response

    async def _call_claude(self, system: str, user_message: str) -> dict:
        """Call the Anthropic API."""
        try:
            import asyncio
            # Run synchronous API call in executor to not block event loop
            loop = asyncio.get_event_loop()
            message = await loop.run_in_executor(
                None,
                lambda: self._client.messages.create(
                    model=self._model,
                    max_tokens=2048,
                    system=system,
                    messages=[{"role": "user", "content": user_message}],
                ),
            )

            # Parse response
            text = message.content[0].text
            try:
                parsed = json.loads(text)
                return parsed
            except json.JSONDecodeError:
                # Claude didn't return JSON — wrap it
                return {
                    "analysis": text,
                    "suggested_changes": [],
                }

        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return {
                "analysis": f"AI analysis unavailable: {e}",
                "suggested_changes": [],
            }

    def _mock_response(self, user_message: str, state: dict) -> dict:
        """Generate a mock response when API key is not available."""
        paths = state.get("paths", {})
        metrics = state.get("metrics", {})
        fec = state.get("fec", {})

        # Build a useful analysis from actual state data
        analysis_parts = ["**Network Status Summary**\n"]

        for pid, p in paths.items():
            name = "Starlink" if str(pid) == "0" else "Cellular"
            rtt = p.get("avg_rtt_ms", "?")
            loss = p.get("loss_pct", "?")
            score = p.get("quality_score", "?")
            alive = p.get("is_alive", False)
            pred = p.get("prediction", {})
            action = pred.get("recommended_action", "?")
            trend = pred.get("trend", "?")

            status = "UP" if alive else "DOWN"
            analysis_parts.append(
                f"- **{name}** ({status}): RTT={rtt}ms, Loss={loss}%, "
                f"Score={score}, Trend={trend}, Action={action}"
            )

        fec_mode = metrics.get("fec_mode", "?")
        recoveries = fec.get("total_recoveries", 0)
        throughput = metrics.get("aggregate_throughput_mbps", "?")
        analysis_parts.append(f"\n**Aggregate**: {throughput} Mbps, FEC={fec_mode}, Recoveries={recoveries}")
        analysis_parts.append(
            "\n*Note: AI analysis requires ANTHROPIC_API_KEY to be set. "
            "This is a summary generated from live metrics.*"
        )

        return {
            "analysis": "\n".join(analysis_parts),
            "suggested_changes": [],
        }

    def get_history(self, last_n: int = 20) -> list[dict]:
        return self._history[-last_n:]

    @property
    def is_enabled(self) -> bool:
        return self._client is not None
