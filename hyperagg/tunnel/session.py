"""
Session Resumption — survives complete path dropout and reconnects.

On total path loss (tunnel/bridge), state is persisted to disk:
  - session_id, global_seq, per_path_seq, fec_group_id
  - scheduler mode, path monitor history

On reconnect:
  - Client sends RECONNECT with session_id + last_global_seq
  - Server checks gap < 10000 packets → resumes from last_global_seq + 1
  - Scheduler inherits saved history (no cold start)
"""

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hyperagg.tunnel.session")

STATE_DIR = Path("/tmp/hyperagg_sessions")


@dataclass
class SessionState:
    """Persistable session state."""
    session_id: str
    global_seq: int = 0
    path_seqs: dict = field(default_factory=dict)      # {path_id: seq}
    fec_group_id: int = 0
    scheduler_mode: str = "ai"
    created_at: float = 0.0
    last_saved_at: float = 0.0
    # Path monitor RTT history (last 20 per path)
    path_rtt_history: dict = field(default_factory=dict)  # {path_id: [rtt, ...]}


class SessionManager:
    """Manages session persistence and resumption."""

    def __init__(self, state_dir: Optional[str] = None, max_gap: int = 10000,
                 save_interval_sec: float = 1.0):
        self._state_dir = Path(state_dir) if state_dir else STATE_DIR
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._max_gap = max_gap
        self._save_interval = save_interval_sec
        self._state: Optional[SessionState] = None
        self._last_save = 0.0
        self._status = "new"  # new, active, reconnecting, resumed

    def create_session(self) -> SessionState:
        """Create a new session with a random 128-bit ID."""
        session_id = uuid.uuid4().hex
        self._state = SessionState(
            session_id=session_id,
            created_at=time.time(),
        )
        self._status = "active"
        self._save()
        logger.info(f"Session created: {session_id[:16]}...")
        return self._state

    def update(self, global_seq: int, path_seqs: Optional[dict] = None,
               fec_group_id: int = 0, scheduler_mode: str = "",
               path_rtt_history: Optional[dict] = None) -> None:
        """Update session state (call periodically from tunnel client)."""
        if not self._state:
            return
        self._state.global_seq = global_seq
        if path_seqs:
            self._state.path_seqs = dict(path_seqs)
        self._state.fec_group_id = fec_group_id
        if scheduler_mode:
            self._state.scheduler_mode = scheduler_mode
        if path_rtt_history:
            # Keep last 20 RTT samples per path
            self._state.path_rtt_history = {
                str(k): list(v)[-20:] for k, v in path_rtt_history.items()
            }

        # Periodic save
        now = time.monotonic()
        if now - self._last_save >= self._save_interval:
            self._save()

    def _save(self) -> None:
        """Persist session state to disk."""
        if not self._state:
            return
        self._state.last_saved_at = time.time()
        path = self._state_dir / f"{self._state.session_id}.json"
        try:
            path.write_text(json.dumps(asdict(self._state), indent=2))
            self._last_save = time.monotonic()
        except Exception as e:
            logger.error(f"Session save failed: {e}")

    def try_resume(self, session_id: str, last_global_seq: int) -> Optional[SessionState]:
        """
        Attempt to resume a previous session.

        Args:
            session_id: The session ID to resume.
            last_global_seq: The last sequence number the client processed.

        Returns:
            SessionState if resumable, None if gap too large or not found.
        """
        path = self._state_dir / f"{session_id}.json"
        if not path.exists():
            logger.info(f"Session {session_id[:16]} not found on disk")
            return None

        try:
            data = json.loads(path.read_text())
            saved_state = SessionState(**data)
        except Exception as e:
            logger.error(f"Session load failed: {e}")
            return None

        # Check gap
        gap = saved_state.global_seq - last_global_seq
        if gap > self._max_gap:
            logger.warning(
                f"Session gap too large: {gap} > {self._max_gap}. "
                f"Starting new session."
            )
            return None

        self._state = saved_state
        self._state.global_seq = last_global_seq + 1
        self._status = "resumed"
        logger.info(
            f"Session resumed: {session_id[:16]}... "
            f"(gap={gap}, resuming from seq {last_global_seq + 1})"
        )
        return self._state

    def get_resume_data(self) -> Optional[dict]:
        """Get data needed to send a RECONNECT control packet."""
        if not self._state:
            return None
        return {
            "session_id": self._state.session_id,
            "last_global_seq": self._state.global_seq,
        }

    @property
    def session_id(self) -> Optional[str]:
        return self._state.session_id if self._state else None

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        if value != self._status:
            logger.info(f"Session status: {self._status} → {value}")
            self._status = value

    def get_stats(self) -> dict:
        return {
            "session_id": self._state.session_id[:16] + "..." if self._state else None,
            "status": self._status,
            "global_seq": self._state.global_seq if self._state else 0,
            "age_sec": round(time.time() - self._state.created_at, 1) if self._state else 0,
            "last_saved": round(
                time.time() - self._state.last_saved_at, 1
            ) if self._state and self._state.last_saved_at else None,
        }

    def cleanup(self) -> None:
        """Remove old session files (keep last 10)."""
        if not self._state_dir.exists():
            return
        files = sorted(self._state_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
        for f in files[:-10]:
            f.unlink()
