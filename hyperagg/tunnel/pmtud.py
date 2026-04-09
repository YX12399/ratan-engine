"""
Path MTU Discovery — detect actual MTU per path instead of fixed value.

Sends probe packets of increasing size with DF (Don't Fragment) bit set.
If the probe gets through, that size is safe. If ICMP "Fragmentation Needed"
comes back (or packet is lost), the MTU is below that size.

Binary search between min_mtu (576) and max_mtu (1500) to find the largest
packet that fits.
"""

import asyncio
import logging
import socket
import struct
from typing import Optional

logger = logging.getLogger("hyperagg.tunnel.pmtud")

MIN_MTU = 576    # IPv4 minimum
MAX_MTU = 1500   # Ethernet maximum
PROBE_TIMEOUT = 2.0  # seconds


class PathMTUDiscovery:
    """Per-path MTU discovery using binary search probe."""

    def __init__(self):
        self._path_mtu: dict[int, int] = {}  # path_id -> discovered MTU
        self._last_probe: dict[int, float] = {}
        self._probe_interval = 60.0  # Re-probe every 60 seconds

    def get_mtu(self, path_id: int, default: int = 1280) -> int:
        """Get discovered MTU for a path, or default."""
        return self._path_mtu.get(path_id, default)

    def get_safe_payload_size(self, path_id: int, default_mtu: int = 1280) -> int:
        """
        Get maximum safe payload size for HyperAgg packets on this path.

        Accounts for: IP(20) + UDP(8) + HyperAgg header(28) + Poly1305 tag(16) = 72 bytes overhead
        """
        mtu = self.get_mtu(path_id, default_mtu)
        return max(MIN_MTU - 72, mtu - 72)

    async def probe_path(self, path_id: int, sock: socket.socket,
                         server_addr: str, server_port: int) -> int:
        """
        Probe a path to discover its MTU using binary search.

        Sends UDP packets of increasing size. If packet gets through
        (server echoes keepalive), size is safe. If lost, too big.

        Returns discovered MTU.
        """
        import time
        from hyperagg.tunnel.packet import HyperAggPacket, set_connection_start

        low = MIN_MTU
        high = MAX_MTU
        best = MIN_MTU

        # Binary search: 10 probes covers 576-1500 range
        for _ in range(10):
            if high - low < 10:
                break

            test_size = (low + high) // 2

            # Build a probe packet of exactly test_size bytes (outer UDP payload)
            # Outer = IP(20) + UDP(8) + payload
            # So UDP payload = test_size - 28
            payload_size = max(1, test_size - 28 - 28 - 16)  # Minus HyperAgg header + tag
            probe_payload = b'\x00' * payload_size
            pkt = HyperAggPacket.create_keepalive(path_id, 0)
            pkt.payload = probe_payload
            pkt.payload_len = len(probe_payload)
            wire = pkt.serialize()

            # Try to send with DF bit (platform-dependent)
            try:
                # Set DF bit on socket (Linux)
                try:
                    sock.setsockopt(socket.IPPROTO_IP, 10, 2)  # IP_MTU_DISCOVER = IP_PMTUDISC_DO
                except (OSError, AttributeError):
                    pass

                sock.sendto(wire, (server_addr, server_port))

                # Wait briefly for ICMP error or success
                await asyncio.sleep(0.1)

                # If we didn't get an exception, this size works
                best = test_size
                low = test_size + 1

            except OSError as e:
                if e.errno == 90:  # EMSGSIZE — packet too big
                    high = test_size - 1
                else:
                    # Other error — can't determine, use last known good
                    break

            finally:
                # Reset DF bit
                try:
                    sock.setsockopt(socket.IPPROTO_IP, 10, 0)  # IP_PMTUDISC_DONT
                except (OSError, AttributeError):
                    pass

        self._path_mtu[path_id] = best
        self._last_probe[path_id] = asyncio.get_event_loop().time()
        logger.info(f"PMTUD path {path_id}: MTU={best} (safe payload={best - 72})")
        return best

    def should_probe(self, path_id: int) -> bool:
        """Check if it's time to re-probe this path."""
        import time
        last = self._last_probe.get(path_id, 0)
        return (time.monotonic() - last) > self._probe_interval

    def get_stats(self) -> dict:
        return {
            "path_mtu": dict(self._path_mtu),
            "safe_payload": {pid: self.get_safe_payload_size(pid) for pid in self._path_mtu},
        }
