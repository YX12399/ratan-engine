"""
Rate-Limited Send Queue — buffers packets on EAGAIN and retries with backoff.

Instead of dropping packets when the socket buffer is full, queues them
and drains at the socket's actual send rate. Prevents burst loss.
"""

import asyncio
import collections
import logging
import socket
import time
from typing import Optional

logger = logging.getLogger("hyperagg.tunnel.send_queue")


class SendQueue:
    """Per-path send queue with rate-limited drain."""

    def __init__(self, max_queue_size: int = 256, max_retry_ms: float = 50.0):
        self._queues: dict[int, collections.deque] = {}  # path_id -> deque of (wire, addr)
        self._max_size = max_queue_size
        self._max_retry_ms = max_retry_ms
        self._dropped = 0
        self._retried = 0
        self._total_sent = 0

    def try_send(self, path_id: int, sock: socket.socket,
                 wire: bytes, addr: tuple) -> bool:
        """
        Try to send immediately. If EAGAIN, queue for retry.

        Returns True if sent (or queued), False if queue is full (dropped).
        """
        try:
            sock.sendto(wire, addr)
            self._total_sent += 1
            return True
        except BlockingIOError:
            # Queue for retry
            return self._enqueue(path_id, wire, addr)
        except OSError:
            return False

    def _enqueue(self, path_id: int, wire: bytes, addr: tuple) -> bool:
        """Add to retry queue. Returns False if queue full (packet dropped)."""
        if path_id not in self._queues:
            self._queues[path_id] = collections.deque(maxlen=self._max_size)

        q = self._queues[path_id]
        if len(q) >= self._max_size:
            # Drop oldest to make room (tail drop)
            q.popleft()
            self._dropped += 1

        q.append((wire, addr, time.monotonic()))
        return True

    async def drain(self, path_id: int, sock: socket.socket) -> int:
        """
        Drain queued packets for a path. Call periodically (every 1-5ms).

        Returns number of packets drained.
        """
        q = self._queues.get(path_id)
        if not q:
            return 0

        drained = 0
        now = time.monotonic()
        cutoff = now - (self._max_retry_ms / 1000.0)

        while q:
            wire, addr, enqueued_at = q[0]

            # Drop if too old (past retry deadline)
            if enqueued_at < cutoff:
                q.popleft()
                self._dropped += 1
                continue

            # Try to send
            try:
                sock.sendto(wire, addr)
                q.popleft()
                self._retried += 1
                self._total_sent += 1
                drained += 1
            except BlockingIOError:
                break  # Still full, try again later
            except OSError:
                q.popleft()  # Discard on hard error
                self._dropped += 1

        return drained

    def pending_count(self, path_id: int) -> int:
        return len(self._queues.get(path_id, []))

    def total_pending(self) -> int:
        return sum(len(q) for q in self._queues.values())

    def get_stats(self) -> dict:
        return {
            "total_sent": self._total_sent,
            "total_retried": self._retried,
            "total_dropped": self._dropped,
            "pending": {pid: len(q) for pid, q in self._queues.items()},
            "retry_success_pct": round(
                self._retried / max(self._retried + self._dropped, 1) * 100, 1
            ),
        }
