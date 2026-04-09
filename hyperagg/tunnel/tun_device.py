"""
TUN Device Manager — creates and manages a TUN (layer 3) virtual network interface.

All application traffic destined for the bonded connection is routed through
this TUN device. The HyperAgg engine reads packets from TUN, bonds them
across multiple paths, and writes received packets back to TUN.

This is a userspace application — no kernel module required.

Performance target: handle 10,000+ packets/second using non-blocking I/O
with asyncio add_reader (no executor thread pool overhead).
"""

import asyncio
import errno
import fcntl
import logging
import os
import struct
import subprocess
from typing import Optional

logger = logging.getLogger("hyperagg.tun")

# Linux TUN/TAP ioctl constants
TUNSETIFF = 0x400454CA
IFF_TUN = 0x0001
IFF_NO_PI = 0x1000


class TunDevice:
    """
    Async TUN device wrapper using loop.add_reader for zero-overhead I/O.
    """

    def __init__(self, name: str = "hagg0", mtu: int = 1400):
        self.name = name
        self.mtu = mtu
        self._fd: Optional[int] = None
        self._queue: Optional[asyncio.Queue] = None
        self._running = False

        # Stats
        self.packets_read = 0
        self.packets_written = 0
        self.bytes_read = 0
        self.bytes_written = 0
        self.drops = 0

    async def open(self, ip_addr: str, subnet_mask: int = 30,
                   queue_size: int = 8192) -> int:
        """
        Create TUN device and configure it.

        Args:
            ip_addr: IP address to assign (e.g., "10.99.0.1").
            subnet_mask: Subnet mask bits (default /30).
            queue_size: Max packets to buffer before dropping.

        Returns:
            The file descriptor for the TUN device.
        """
        # Open /dev/net/tun
        self._fd = os.open("/dev/net/tun", os.O_RDWR)

        # Create TUN interface with IFF_TUN | IFF_NO_PI
        ifr = struct.pack("16sH", self.name.encode(), IFF_TUN | IFF_NO_PI)
        fcntl.ioctl(self._fd, TUNSETIFF, ifr)

        # Set non-blocking for add_reader
        os.set_blocking(self._fd, False)

        # Set MTU
        subprocess.run(
            ["ip", "link", "set", "dev", self.name, "mtu", str(self.mtu)],
            check=True, capture_output=True,
        )

        # Assign IP
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

        # Initialize async queue and register reader
        self._queue = asyncio.Queue(maxsize=queue_size)
        self._running = True

        loop = asyncio.get_event_loop()
        loop.add_reader(self._fd, self._on_readable)

        logger.info(f"TUN {self.name} is UP (mtu={self.mtu}, fd={self._fd})")
        return self._fd

    def _on_readable(self) -> None:
        """
        Callback fired by event loop when TUN fd has data.
        Drains all available packets in a single callback (batch reading).
        """
        batch = 0
        while batch < 64:  # Read up to 64 packets per callback
            try:
                data = os.read(self._fd, self.mtu + 64)
                if not data:
                    break
                self.packets_read += 1
                self.bytes_read += len(data)
                try:
                    self._queue.put_nowait(data)
                except asyncio.QueueFull:
                    # Drop oldest packet under pressure
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    self._queue.put_nowait(data)
                    self.drops += 1
                batch += 1
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    break  # No more data available
                if self._running:
                    logger.error(f"TUN read error: {e}")
                break

    async def read_packet(self) -> bytes:
        """Read one IP packet from the TUN device (async)."""
        return await self._queue.get()

    def read_packets_nowait(self, max_batch: int = 32) -> list[bytes]:
        """Read up to max_batch packets without blocking."""
        packets = []
        for _ in range(max_batch):
            try:
                packets.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return packets

    async def write_packet(self, data: bytes) -> None:
        """Write an IP packet into the TUN device (async)."""
        if self._fd is None or not self._running:
            return
        try:
            os.write(self._fd, data)
            self.packets_written += 1
            self.bytes_written += len(data)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                # fd buffer full — use event loop to wait
                loop = asyncio.get_event_loop()
                future = loop.create_future()

                def _on_writable():
                    loop.remove_writer(self._fd)
                    try:
                        os.write(self._fd, data)
                        self.packets_written += 1
                        self.bytes_written += len(data)
                    except OSError as we:
                        logger.error(f"TUN write error: {we}")
                    future.set_result(None)

                loop.add_writer(self._fd, _on_writable)
                await future
            elif self._running:
                logger.error(f"TUN write error: {e}")

    def write_packet_sync(self, data: bytes) -> None:
        """Synchronous write for hot-path (no await overhead)."""
        if self._fd is None or not self._running:
            return
        try:
            os.write(self._fd, data)
            self.packets_written += 1
            self.bytes_written += len(data)
        except OSError as e:
            if self._running and e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                logger.error(f"TUN sync write error: {e}")

    async def setup_routing(
        self,
        via_tun: Optional[list[str]] = None,
        vps_ip: Optional[str] = None,
        wan_routes: Optional[list[dict]] = None,
        lan_subnet: Optional[str] = None,
    ) -> None:
        """
        Set up routing so traffic flows through the TUN device.

        Args:
            via_tun: List of CIDRs to route through TUN, or None for default route.
            vps_ip: VPS IP address (must NOT route through TUN to avoid loops).
            wan_routes: List of {"ip": vps_ip, "gateway": gw, "interface": iface}
                        to keep direct routes for outer UDP tunnel packets.
            lan_subnet: LAN subnet to EXCLUDE from tunnel (e.g., "192.168.50.0/24").
        """
        # LOCAL SUBNET BYPASS — LAN traffic must NOT enter the tunnel
        if lan_subnet:
            subprocess.run(
                ["ip", "route", "add", lan_subnet, "dev", self.name.replace("hagg", "eth"),
                 "scope", "link", "metric", "1"],
                capture_output=True,
            )
            # Simpler: just ensure the local subnet route exists via the LAN interface
            # (kernel should already have this, but be explicit)
            logger.info(f"Local subnet {lan_subnet} bypasses tunnel")

        if via_tun:
            for cidr in via_tun:
                subprocess.run(
                    ["ip", "route", "add", cidr, "dev", self.name],
                    capture_output=True,
                )
                logger.info(f"Route {cidr} via {self.name}")
        else:
            # Default route through TUN with low metric
            subprocess.run(
                ["ip", "route", "add", "default", "dev", self.name, "metric", "50"],
                capture_output=True,
            )
            logger.info(f"Default route via {self.name} metric 50")

        # Keep direct routes to VPS so outer UDP tunnel doesn't loop
        if wan_routes:
            for i, route in enumerate(wan_routes):
                subprocess.run(
                    ["ip", "route", "add", f"{route['ip']}/32",
                     "via", route["gateway"], "dev", route["interface"],
                     "metric", str(10 + i)],
                    capture_output=True,
                )
                logger.info(
                    f"Direct route {route['ip']} via {route['gateway']} "
                    f"dev {route['interface']}"
                )

    async def close(self) -> None:
        """Close the TUN device and clean up."""
        self._running = False
        if self._fd is not None:
            try:
                loop = asyncio.get_event_loop()
                loop.remove_reader(self._fd)
            except Exception:
                pass
            os.close(self._fd)
            self._fd = None
        logger.info(f"TUN {self.name} closed")

    @property
    def is_open(self) -> bool:
        return self._fd is not None and self._running

    @property
    def stats(self) -> dict:
        return {
            "packets_read": self.packets_read,
            "packets_written": self.packets_written,
            "bytes_read": self.bytes_read,
            "bytes_written": self.bytes_written,
            "drops": self.drops,
            "queue_depth": self._queue.qsize() if self._queue else 0,
        }


if __name__ == "__main__":
    import sys
    print("TUN device module loaded successfully.")
    print(f"Constants: TUNSETIFF=0x{TUNSETIFF:08X}, IFF_TUN=0x{IFF_TUN:04X}")
    print("NOTE: Creating a real TUN device requires root/CAP_NET_ADMIN.")
    print("Run with sudo for live testing.")
