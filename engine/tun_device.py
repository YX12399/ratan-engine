"""
TUN Device — creates and manages a TUN virtual network interface.
Used by both edge client (ratan0) and VPS server (ratan-srv0) to
intercept and inject IP packets for the aggregation tunnel.

Requires Linux with /dev/net/tun and CAP_NET_ADMIN or root.
"""

import asyncio
import fcntl
import os
import struct
import subprocess
import logging
from typing import Optional

logger = logging.getLogger("ratan.tun_device")

# Linux TUN/TAP constants
TUNSETIFF = 0x400454ca
IFF_TUN = 0x0001
IFF_NO_PI = 0x1000


class TunDevice:
    """
    Async TUN device wrapper. Opens /dev/net/tun, creates a named interface,
    and provides async read/write for IP packets.
    """

    def __init__(self, name: str = "ratan0", mtu: int = 1400):
        self.name = name
        self.mtu = mtu
        self._fd: Optional[int] = None
        self._queue: Optional[asyncio.Queue] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._running = False

    async def open(self, ip_addr: str = "", subnet_mask: int = 24) -> None:
        """Open the TUN device and configure it."""
        # Open /dev/net/tun
        self._fd = os.open("/dev/net/tun", os.O_RDWR)

        # Create TUN interface with IFF_TUN | IFF_NO_PI (no packet info header)
        ifr = struct.pack("16sH", self.name.encode(), IFF_TUN | IFF_NO_PI)
        fcntl.ioctl(self._fd, TUNSETIFF, ifr)

        logger.info(f"TUN device created: {self.name}")

        # Set MTU
        subprocess.run(
            ["ip", "link", "set", "dev", self.name, "mtu", str(self.mtu)],
            check=True, capture_output=True,
        )

        # Assign IP if provided
        if ip_addr:
            subprocess.run(
                ["ip", "addr", "add", f"{ip_addr}/{subnet_mask}", "dev", self.name],
                check=True, capture_output=True,
            )
            logger.info(f"Assigned {ip_addr}/{subnet_mask} to {self.name}")

        # Bring interface up
        subprocess.run(
            ["ip", "link", "set", "dev", self.name, "up"],
            check=True, capture_output=True,
        )
        logger.info(f"TUN {self.name} is UP (mtu={self.mtu})")

        # Start async reader
        self._queue = asyncio.Queue(maxsize=4096)
        self._running = True
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self) -> None:
        """Background task: read IP packets from TUN fd in a thread, feed to async queue."""
        loop = asyncio.get_event_loop()
        while self._running:
            try:
                # Read from fd in a thread to avoid blocking the event loop
                data = await loop.run_in_executor(None, self._blocking_read)
                if data:
                    try:
                        self._queue.put_nowait(data)
                    except asyncio.QueueFull:
                        # Drop oldest packet under pressure
                        try:
                            self._queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        self._queue.put_nowait(data)
            except OSError:
                if self._running:
                    logger.error("TUN read error", exc_info=True)
                break

    def _blocking_read(self) -> Optional[bytes]:
        """Blocking read of one IP packet from the TUN fd."""
        try:
            return os.read(self._fd, self.mtu + 64)
        except OSError:
            if self._running:
                raise
            return None

    async def read_packet(self) -> bytes:
        """Read one IP packet from the TUN device (async)."""
        return await self._queue.get()

    async def write_packet(self, data: bytes) -> None:
        """Write an IP packet into the TUN device."""
        if self._fd is None:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._blocking_write, data)

    def _blocking_write(self, data: bytes) -> None:
        """Blocking write of one IP packet to the TUN fd."""
        try:
            os.write(self._fd, data)
        except OSError:
            if self._running:
                logger.error("TUN write error", exc_info=True)

    async def close(self) -> None:
        """Close the TUN device and clean up."""
        self._running = False
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        logger.info(f"TUN {self.name} closed")

    @property
    def is_open(self) -> bool:
        return self._fd is not None and self._running
