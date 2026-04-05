"""
Test Session Runner — captures metrics during named test runs.

Snapshots system state every 2 seconds for the test duration,
computes summary statistics, and saves results to disk.
"""

import asyncio
import json
import logging
import os
import statistics
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hyperagg.testing.runner")

RESULTS_DIR = Path(__file__).parent.parent.parent / "tests" / "results"


@dataclass
class TestSnapshot:
    """One measurement during a test (taken every 2 seconds)."""
    timestamp: float
    elapsed_sec: float
    paths: dict
    metrics: dict
    fec: dict
    scheduler: dict
    variant: str = ""  # "A" or "B" for A/B tests, empty for standard


@dataclass
class TestSummary:
    """Computed summary of a completed test."""
    duration_sec: float
    snapshots_count: int
    per_path: dict  # path_id -> {avg_rtt, p50_rtt, p95_rtt, avg_loss, avg_jitter, avg_score}
    aggregate: dict  # {avg_throughput, avg_effective_loss, total_fec_recoveries}
    scheduler_mode: str
    fec_mode: str


@dataclass
class TestSession:
    """A named test run with all captured data."""
    id: str
    name: str
    description: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    duration_target_sec: float = 60.0
    status: str = "pending"  # pending, running, completed, stopped
    test_type: str = "standard"  # standard, ab
    variant_a: Optional[dict] = None  # Config for A/B variant A
    variant_b: Optional[dict] = None  # Config for A/B variant B
    snapshots: list[TestSnapshot] = field(default_factory=list)
    summary: Optional[TestSummary] = None


class TestRunner:
    """Manages test sessions — start, capture, stop, save, list."""

    def __init__(self, sdn_controller):
        self._sdn = sdn_controller
        self._active: Optional[TestSession] = None
        self._capture_task: Optional[asyncio.Task] = None
        self._sessions: dict[str, TestSession] = {}

        # Ensure results directory exists
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        # Load existing results
        self._load_existing()

    def _load_existing(self) -> None:
        """Load saved test results from disk."""
        if not RESULTS_DIR.exists():
            return
        for f in RESULTS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                session = TestSession(
                    id=data["id"],
                    name=data["name"],
                    description=data.get("description", ""),
                    started_at=data.get("started_at", 0),
                    ended_at=data.get("ended_at", 0),
                    duration_target_sec=data.get("duration_target_sec", 60),
                    status=data.get("status", "completed"),
                    snapshots=[],  # Don't load full snapshots into memory
                )
                if data.get("summary"):
                    s = data["summary"]
                    session.summary = TestSummary(
                        duration_sec=s.get("duration_sec", 0),
                        snapshots_count=s.get("snapshots_count", 0),
                        per_path=s.get("per_path", {}),
                        aggregate=s.get("aggregate", {}),
                        scheduler_mode=s.get("scheduler_mode", ""),
                        fec_mode=s.get("fec_mode", ""),
                    )
                self._sessions[session.id] = session
            except Exception as e:
                logger.debug(f"Failed to load {f}: {e}")

    async def start_test(self, name: str, duration_minutes: float = 1.0,
                         description: str = "") -> TestSession:
        """Start a new test session."""
        if self._active and self._active.status == "running":
            raise RuntimeError("A test is already running")

        session = TestSession(
            id=str(uuid.uuid4())[:8],
            name=name,
            description=description,
            started_at=time.time(),
            duration_target_sec=duration_minutes * 60,
            status="running",
        )

        self._active = session
        self._sessions[session.id] = session

        # Start capture loop
        self._capture_task = asyncio.create_task(self._capture_loop(session))
        logger.info(f"Test '{name}' started (id={session.id}, duration={duration_minutes}m)")

        return session

    async def start_ab_test(
        self,
        name: str,
        duration_minutes: float = 1.0,
        variant_a: Optional[dict] = None,
        variant_b: Optional[dict] = None,
        description: str = "",
    ) -> TestSession:
        """
        Start an A/B test — runs variant A for the first half, variant B for the second.

        Args:
            variant_a: Config to apply for first half, e.g. {"scheduler_mode": "ai"}
            variant_b: Config to apply for second half, e.g. {"scheduler_mode": "weighted"}
        """
        if self._active and self._active.status == "running":
            raise RuntimeError("A test is already running")

        session = TestSession(
            id=str(uuid.uuid4())[:8],
            name=name,
            description=description or f"A/B: {variant_a} vs {variant_b}",
            started_at=time.time(),
            duration_target_sec=duration_minutes * 60,
            status="running",
            test_type="ab",
            variant_a=variant_a or {},
            variant_b=variant_b or {},
        )

        self._active = session
        self._sessions[session.id] = session
        self._capture_task = asyncio.create_task(self._ab_capture_loop(session))
        logger.info(f"A/B test '{name}' started (id={session.id})")

        return session

    async def _ab_capture_loop(self, session: TestSession) -> None:
        """Capture loop for A/B tests — switch config at halfway point."""
        try:
            half = session.duration_target_sec / 2.0
            current_variant = "A"

            # Apply variant A config
            self._apply_variant(session.variant_a)
            logger.info(f"A/B test: variant A active — {session.variant_a}")

            while True:
                elapsed = time.time() - session.started_at
                if elapsed >= session.duration_target_sec:
                    break

                # Switch to variant B at halfway
                if current_variant == "A" and elapsed >= half:
                    current_variant = "B"
                    self._apply_variant(session.variant_b)
                    logger.info(f"A/B test: switched to variant B — {session.variant_b}")

                state = self._sdn.get_system_state()
                snapshot = TestSnapshot(
                    timestamp=time.time(),
                    elapsed_sec=round(elapsed, 1),
                    paths=state.get("paths", {}),
                    metrics=state.get("metrics", {}),
                    fec=state.get("fec", {}),
                    scheduler=state.get("scheduler", {}),
                    variant=current_variant,
                )
                session.snapshots.append(snapshot)
                await asyncio.sleep(2.0)

        except asyncio.CancelledError:
            pass
        finally:
            if session.status == "running":
                session.status = "completed"
            self._finalize(session)
            self._active = None

    def _apply_variant(self, variant: Optional[dict]) -> None:
        """Apply a config variant via the SDN controller."""
        if not variant:
            return
        for key, value in variant.items():
            if key == "scheduler_mode" and hasattr(self._sdn, "set_scheduler_mode"):
                self._sdn.set_scheduler_mode(value)
            elif key == "fec_mode" and hasattr(self._sdn, "set_fec_mode"):
                self._sdn.set_fec_mode(value)

    async def stop_test(self) -> Optional[TestSession]:
        """Stop the active test early."""
        if not self._active or self._active.status != "running":
            return None

        self._active.status = "stopped"
        if self._capture_task:
            self._capture_task.cancel()
            try:
                await self._capture_task
            except asyncio.CancelledError:
                pass

        self._finalize(self._active)
        result = self._active
        self._active = None
        return result

    async def _capture_loop(self, session: TestSession) -> None:
        """Capture metrics every 2 seconds until duration expires."""
        try:
            while True:
                elapsed = time.time() - session.started_at
                if elapsed >= session.duration_target_sec:
                    break

                state = self._sdn.get_system_state()
                snapshot = TestSnapshot(
                    timestamp=time.time(),
                    elapsed_sec=round(elapsed, 1),
                    paths=state.get("paths", {}),
                    metrics=state.get("metrics", {}),
                    fec=state.get("fec", {}),
                    scheduler=state.get("scheduler", {}),
                )
                session.snapshots.append(snapshot)

                await asyncio.sleep(2.0)

        except asyncio.CancelledError:
            pass
        finally:
            if session.status == "running":
                session.status = "completed"
            self._finalize(session)
            self._active = None

    def _finalize(self, session: TestSession) -> None:
        """Compute summary and save to disk."""
        session.ended_at = time.time()

        if not session.snapshots:
            return

        # Compute per-path summaries
        per_path = {}
        path_ids = set()
        for snap in session.snapshots:
            for pid in snap.paths:
                path_ids.add(pid)

        for pid in path_ids:
            rtts = [s.paths[pid]["avg_rtt_ms"] for s in session.snapshots if pid in s.paths and s.paths[pid].get("avg_rtt_ms")]
            losses = [s.paths[pid]["loss_pct"] for s in session.snapshots if pid in s.paths]
            jitters = [s.paths[pid]["jitter_ms"] for s in session.snapshots if pid in s.paths]
            scores = [s.paths[pid]["quality_score"] for s in session.snapshots if pid in s.paths]

            per_path[str(pid)] = {
                "avg_rtt_ms": round(statistics.mean(rtts), 1) if rtts else 0,
                "p50_rtt_ms": round(statistics.median(rtts), 1) if rtts else 0,
                "p95_rtt_ms": round(sorted(rtts)[int(len(rtts) * 0.95)] if len(rtts) > 1 else (rtts[0] if rtts else 0), 1),
                "avg_loss_pct": round(statistics.mean(losses), 2) if losses else 0,
                "avg_jitter_ms": round(statistics.mean(jitters), 1) if jitters else 0,
                "avg_score": round(statistics.mean(scores), 3) if scores else 0,
            }

        # Aggregate metrics
        throughputs = [s.metrics.get("aggregate_throughput_mbps", 0) for s in session.snapshots]
        eff_losses = [s.metrics.get("effective_loss_pct", 0) for s in session.snapshots]
        fec_recoveries = max(
            (s.fec.get("total_recoveries", 0) for s in session.snapshots), default=0
        )

        last_state = session.snapshots[-1]
        session.summary = TestSummary(
            duration_sec=round(session.ended_at - session.started_at, 1),
            snapshots_count=len(session.snapshots),
            per_path=per_path,
            aggregate={
                "avg_throughput_mbps": round(statistics.mean(throughputs), 1) if throughputs else 0,
                "max_throughput_mbps": round(max(throughputs), 1) if throughputs else 0,
                "avg_effective_loss_pct": round(statistics.mean(eff_losses), 2) if eff_losses else 0,
                "total_fec_recoveries": fec_recoveries,
            },
            scheduler_mode=last_state.scheduler.get("mode", ""),
            fec_mode=last_state.fec.get("current_mode", ""),
        )

        # Save to disk
        self._save(session)
        logger.info(f"Test '{session.name}' finalized (id={session.id}, "
                     f"snapshots={len(session.snapshots)})")

    def _save(self, session: TestSession) -> None:
        """Save test session to JSON file."""
        path = RESULTS_DIR / f"{session.id}.json"
        data = {
            "id": session.id,
            "name": session.name,
            "description": session.description,
            "started_at": session.started_at,
            "ended_at": session.ended_at,
            "duration_target_sec": session.duration_target_sec,
            "status": session.status,
            "test_type": session.test_type,
            "variant_a": session.variant_a,
            "variant_b": session.variant_b,
            "summary": asdict(session.summary) if session.summary else None,
            "snapshots": [asdict(s) for s in session.snapshots],
        }
        path.write_text(json.dumps(data, indent=2, default=str))

    def get_active(self) -> Optional[dict]:
        """Get info about the currently running test."""
        if not self._active or self._active.status != "running":
            return None
        elapsed = round(time.time() - self._active.started_at, 1)
        current_variant = ""
        if self._active.test_type == "ab":
            half = self._active.duration_target_sec / 2.0
            current_variant = "A" if elapsed < half else "B"
        return {
            "id": self._active.id,
            "name": self._active.name,
            "status": self._active.status,
            "test_type": self._active.test_type,
            "elapsed_sec": elapsed,
            "duration_target_sec": self._active.duration_target_sec,
            "snapshots_count": len(self._active.snapshots),
            "current_variant": current_variant,
        }

    def list_tests(self) -> list[dict]:
        """List all test sessions (summary only, no snapshot data)."""
        results = []
        for s in sorted(self._sessions.values(), key=lambda x: x.started_at, reverse=True):
            results.append({
                "id": s.id,
                "name": s.name,
                "status": s.status,
                "started_at": s.started_at,
                "duration_sec": round(s.ended_at - s.started_at, 1) if s.ended_at else 0,
                "summary": asdict(s.summary) if s.summary else None,
            })
        return results

    def get_test(self, test_id: str) -> Optional[dict]:
        """Get full test data including snapshots."""
        session = self._sessions.get(test_id)
        if not session:
            # Try loading from disk
            path = RESULTS_DIR / f"{test_id}.json"
            if path.exists():
                return json.loads(path.read_text())
            return None

        if session.snapshots:
            return {
                "id": session.id,
                "name": session.name,
                "status": session.status,
                "started_at": session.started_at,
                "ended_at": session.ended_at,
                "summary": asdict(session.summary) if session.summary else None,
                "snapshots": [asdict(s) for s in session.snapshots],
            }
        else:
            # Load from disk (snapshots not in memory)
            path = RESULTS_DIR / f"{session.id}.json"
            if path.exists():
                return json.loads(path.read_text())
            return {"id": session.id, "name": session.name, "status": session.status}
