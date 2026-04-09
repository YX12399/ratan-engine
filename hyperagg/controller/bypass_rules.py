"""
Bypass Rules — route specific domains/IPs directly, skipping the tunnel.

Similar to OMR-Bypass: allows selected traffic to go direct instead of
through the bonded tunnel. Useful for:
  - Local LAN traffic (already handled by routing)
  - Specific cloud services that perform better on direct connection
  - VPN services that shouldn't be double-tunneled
  - Speed test sites (to measure raw path speed)
"""

import ipaddress
import logging
import subprocess
from typing import Optional

logger = logging.getLogger("hyperagg.controller.bypass")


class BypassRules:
    """Manages traffic bypass rules — routes specified destinations direct."""

    def __init__(self):
        self._ip_rules: set[str] = set()        # IPs/CIDRs that bypass tunnel
        self._domain_rules: set[str] = set()     # Domains to bypass (resolved to IPs)
        self._resolved: dict[str, set[str]] = {} # domain → {resolved IPs}

    def add_ip(self, cidr: str) -> dict:
        """Add an IP or CIDR to bypass list."""
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            return {"status": "error", "detail": f"Invalid CIDR: {cidr}"}

        self._ip_rules.add(cidr)
        self._apply_route(cidr)
        logger.info(f"Bypass added: {cidr}")
        return {"status": "ok", "cidr": cidr}

    def add_domain(self, domain: str) -> dict:
        """Add a domain to bypass list (resolves to IPs)."""
        import socket
        try:
            ips = set()
            for result in socket.getaddrinfo(domain, None, socket.AF_INET):
                ips.add(result[4][0])
            self._domain_rules.add(domain)
            self._resolved[domain] = ips
            for ip in ips:
                self._ip_rules.add(f"{ip}/32")
                self._apply_route(f"{ip}/32")
            logger.info(f"Bypass added: {domain} → {ips}")
            return {"status": "ok", "domain": domain, "resolved_ips": list(ips)}
        except socket.gaierror as e:
            return {"status": "error", "detail": f"DNS resolution failed: {e}"}

    def remove_ip(self, cidr: str) -> dict:
        self._ip_rules.discard(cidr)
        self._remove_route(cidr)
        return {"status": "ok"}

    def remove_domain(self, domain: str) -> dict:
        self._domain_rules.discard(domain)
        for ip in self._resolved.pop(domain, set()):
            cidr = f"{ip}/32"
            self._ip_rules.discard(cidr)
            self._remove_route(cidr)
        return {"status": "ok"}

    def clear_all(self) -> dict:
        for cidr in list(self._ip_rules):
            self._remove_route(cidr)
        self._ip_rules.clear()
        self._domain_rules.clear()
        self._resolved.clear()
        return {"status": "ok"}

    def _apply_route(self, cidr: str) -> None:
        """Add a direct route bypassing the TUN device."""
        # Route this CIDR via the default gateway instead of TUN
        # This uses a higher-priority route than the TUN default route
        subprocess.run(
            ["ip", "route", "add", cidr, "via", "default", "metric", "5"],
            capture_output=True,
        )

    def _remove_route(self, cidr: str) -> None:
        subprocess.run(
            ["ip", "route", "del", cidr],
            capture_output=True,
        )

    def get_rules(self) -> dict:
        return {
            "ip_rules": sorted(self._ip_rules),
            "domain_rules": sorted(self._domain_rules),
            "resolved": {d: sorted(ips) for d, ips in self._resolved.items()},
            "total_bypassed": len(self._ip_rules),
        }

    def is_bypassed(self, ip: str) -> bool:
        """Check if an IP should bypass the tunnel."""
        try:
            addr = ipaddress.ip_address(ip)
            for cidr in self._ip_rules:
                if addr in ipaddress.ip_network(cidr, strict=False):
                    return True
        except ValueError:
            pass
        return False
