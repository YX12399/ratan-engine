"""
QoS Engine — traffic classification by port and protocol.

Classifies IP packets into tiers (realtime/streaming/bulk) based on
destination port, which determines FEC mode and scheduling priority.
"""

import struct
import logging
from typing import Optional

logger = logging.getLogger("hyperagg.controller.qos")


class QoSEngine:
    """Traffic classifier for QoS-aware scheduling."""

    def __init__(self, config: dict):
        qos_cfg = config.get("qos", {})
        self._enabled = qos_cfg.get("enabled", True)
        self._tiers: dict[str, dict] = qos_cfg.get("tiers", {})
        self._port_map: dict[int, str] = {}
        self._build_port_map()

    def _build_port_map(self) -> None:
        """Pre-compute port → tier mapping for O(1) lookup."""
        for tier_name, tier_cfg in self._tiers.items():
            ports_str = tier_cfg.get("ports", "")
            if not ports_str:
                continue
            try:
                if "-" in ports_str:
                    low, high = ports_str.split("-")
                    for port in range(int(low), int(high) + 1):
                        self._port_map[port] = tier_name
                else:
                    self._port_map[int(ports_str)] = tier_name
            except (ValueError, TypeError):
                logger.warning(f"Invalid port range for tier {tier_name}: {ports_str}")

    def classify(self, ip_packet: bytes) -> str:
        """
        Classify an IP packet into a QoS tier.

        Returns:
            Tier name: "realtime", "streaming", "bulk", etc.
        """
        if not self._enabled or len(ip_packet) < 20:
            return "bulk"

        protocol = ip_packet[9]
        dst_port = self._extract_dst_port(ip_packet, protocol)

        if dst_port is not None and dst_port in self._port_map:
            return self._port_map[dst_port]

        return "bulk"

    def _extract_dst_port(self, ip_packet: bytes, protocol: int) -> Optional[int]:
        """Extract destination port from TCP/UDP header."""
        ihl = (ip_packet[0] & 0x0F) * 4
        if protocol == 17:  # UDP
            if len(ip_packet) >= ihl + 4:
                return struct.unpack("!H", ip_packet[ihl + 2:ihl + 4])[0]
        elif protocol == 6:  # TCP
            if len(ip_packet) >= ihl + 4:
                return struct.unpack("!H", ip_packet[ihl + 2:ihl + 4])[0]
        return None

    def get_fec_mode(self, tier: str) -> str:
        """Get the configured FEC mode for a traffic tier."""
        tier_cfg = self._tiers.get(tier, {})
        return tier_cfg.get("fec_mode", "xor")

    def get_tier_info(self) -> dict:
        return {
            name: {
                "ports": cfg.get("ports", ""),
                "fec_mode": cfg.get("fec_mode", "xor"),
                "bandwidth_pct": cfg.get("bandwidth_pct", 0),
            }
            for name, cfg in self._tiers.items()
        }
