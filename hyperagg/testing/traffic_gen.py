"""
Traffic Generator — generates traffic through the tunnel for demo purposes.

Modes:
  bulk: sends at maximum rate (simulates large download)
  realtime: fixed bitrate (simulates telemetry/video)
  mixed: concurrent streams (JLR vehicle scenario)
"""

import asyncio
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("hyperagg.testing.traffic_gen")


class TrafficGenerator:
    """Generates traffic for demo and testing."""

    def __init__(self):
        self._running = False
        self._mode = ""
        self._task: Optional[asyncio.Task] = None
        self._start_time = 0.0
        self._duration_sec = 0
        self._packets_sent = 0
        self._bytes_sent = 0

    async def start(self, mode: str = "mixed", duration_sec: int = 60,
                    bitrate_kbps: int = 2000, tun_device=None) -> None:
        """Start traffic generation."""
        if self._running:
            raise RuntimeError("Traffic generator already running")

        self._running = True
        self._mode = mode
        self._start_time = time.monotonic()
        self._duration_sec = duration_sec
        self._packets_sent = 0
        self._bytes_sent = 0

        if mode == "bulk":
            self._task = asyncio.create_task(self._bulk_loop(duration_sec, tun_device))
        elif mode == "realtime":
            self._task = asyncio.create_task(self._realtime_loop(bitrate_kbps, duration_sec, tun_device))
        elif mode == "mixed":
            self._task = asyncio.create_task(self._mixed_loop(duration_sec, tun_device))
        else:
            self._running = False
            raise ValueError(f"Unknown mode: {mode}")

        logger.info(f"Traffic generator started: mode={mode}, duration={duration_sec}s")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(f"Traffic generator stopped: {self._packets_sent} packets, "
                     f"{self._bytes_sent/1e6:.1f} MB")

    async def _bulk_loop(self, duration_sec: int, tun=None) -> None:
        """Send at maximum rate."""
        try:
            deadline = time.monotonic() + duration_sec
            while self._running and time.monotonic() < deadline:
                pkt = os.urandom(1400)
                if tun and hasattr(tun, "is_open") and tun.is_open:
                    await tun.write_packet(pkt)
                self._packets_sent += 1
                self._bytes_sent += len(pkt)
                if self._packets_sent % 100 == 0:
                    await asyncio.sleep(0.001)  # Yield to event loop
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    async def _realtime_loop(self, bitrate_kbps: int, duration_sec: int, tun=None) -> None:
        """Send at fixed bitrate (simulates telemetry/video)."""
        try:
            pkt_size = 1400
            interval = pkt_size * 8 / (bitrate_kbps * 1000)  # seconds between packets
            deadline = time.monotonic() + duration_sec
            while self._running and time.monotonic() < deadline:
                pkt = os.urandom(pkt_size)
                if tun and hasattr(tun, "is_open") and tun.is_open:
                    await tun.write_packet(pkt)
                self._packets_sent += 1
                self._bytes_sent += len(pkt)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    async def _mixed_loop(self, duration_sec: int, tun=None) -> None:
        """JLR vehicle scenario: telemetry + streaming + bulk concurrent."""
        try:
            tasks = [
                self._realtime_loop(100, duration_sec, tun),   # 100 kbps telemetry
                self._realtime_loop(2000, duration_sec, tun),  # 2 Mbps streaming
                self._bulk_loop(duration_sec, tun),             # Best-effort bulk
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    def get_status(self) -> dict:
        elapsed = time.monotonic() - self._start_time if self._running else 0
        remaining = max(0, self._duration_sec - elapsed) if self._running else 0
        rate_mbps = (self._bytes_sent * 8 / max(elapsed, 0.1)) / 1e6 if elapsed > 0 else 0
        return {
            "running": self._running,
            "mode": self._mode,
            "elapsed_sec": round(elapsed, 1),
            "remaining_sec": round(remaining, 1),
            "packets_sent": self._packets_sent,
            "bytes_sent_mb": round(self._bytes_sent / 1e6, 1),
            "rate_mbps": round(rate_mbps, 1),
        }
