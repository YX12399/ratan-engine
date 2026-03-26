"""
NAT & Route Manager — handles system-level network setup for the tunnel.
VPS side: enables IP forwarding + iptables MASQUERADE.
Edge side: sets TUN as default route while preserving VPS connectivity.

Requires root/CAP_NET_ADMIN.
"""

import subprocess
import logging

logger = logging.getLogger("ratan.nat_manager")


class NatManager:
    """Manages iptables, sysctl, and ip route rules for the RATAN tunnel."""

    def __init__(self):
        self._rules_applied: list[list[str]] = []
        self._routes_applied: list[list[str]] = []
        self._original_forwarding: str = "0"

    def setup_server_nat(self, tun_name: str, subnet: str, outbound_iface: str = "eth0") -> None:
        """
        Configure VPS for packet forwarding:
        - Enable ip_forward
        - Add iptables MASQUERADE for tunnel subnet → outbound interface
        """
        # Save and enable IP forwarding
        try:
            result = subprocess.run(
                ["sysctl", "-n", "net.ipv4.ip_forward"],
                capture_output=True, text=True, check=True,
            )
            self._original_forwarding = result.stdout.strip()
        except Exception:
            self._original_forwarding = "0"

        self._run(["sysctl", "-w", "net.ipv4.ip_forward=1"])
        logger.info("Enabled IP forwarding")

        # MASQUERADE: tunnel subnet → outbound interface
        nat_rule = [
            "iptables", "-t", "nat", "-A", "POSTROUTING",
            "-s", subnet, "-o", outbound_iface, "-j", "MASQUERADE",
        ]
        self._run(nat_rule)
        self._rules_applied.append(nat_rule)
        logger.info(f"NAT: {subnet} → MASQUERADE via {outbound_iface}")

        # Allow forwarding for the TUN device
        fwd_in = [
            "iptables", "-A", "FORWARD",
            "-i", tun_name, "-o", outbound_iface, "-j", "ACCEPT",
        ]
        fwd_out = [
            "iptables", "-A", "FORWARD",
            "-i", outbound_iface, "-o", tun_name,
            "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT",
        ]
        self._run(fwd_in)
        self._run(fwd_out)
        self._rules_applied.extend([fwd_in, fwd_out])
        logger.info(f"FORWARD rules: {tun_name} ↔ {outbound_iface}")

    def setup_client_routes(
        self, tun_name: str, server_addr: str, server_gateway: str = "",
    ) -> None:
        """
        Configure edge node routing so all traffic goes through the TUN,
        while preserving the route to the VPS itself via the original gateway.

        The VPS route must be added BEFORE the default route change,
        otherwise the tunnel connection itself gets routed into the TUN (loop).
        """
        if server_gateway:
            # Ensure VPS is reachable via its original gateway
            route = ["ip", "route", "add", f"{server_addr}/32", "via", server_gateway]
            self._run(route)
            self._routes_applied.append(route)
            logger.info(f"Preserved VPS route: {server_addr} via {server_gateway}")

        # Set TUN as default route with low metric
        default_route = [
            "ip", "route", "add", "default", "dev", tun_name, "metric", "10",
        ]
        self._run(default_route)
        self._routes_applied.append(default_route)
        logger.info(f"Default route set to {tun_name}")

    def cleanup(self) -> None:
        """Remove all rules/routes we added."""
        # Remove iptables rules (change -A to -D for deletion)
        for rule in reversed(self._rules_applied):
            delete_rule = list(rule)
            try:
                idx = delete_rule.index("-A")
                delete_rule[idx] = "-D"
            except ValueError:
                continue
            self._run(delete_rule, check=False)

        # Remove routes
        for route in reversed(self._routes_applied):
            delete_route = list(route)
            try:
                idx = delete_route.index("add")
                delete_route[idx] = "del"
            except ValueError:
                continue
            self._run(delete_route, check=False)

        # Restore original IP forwarding setting
        self._run(
            ["sysctl", "-w", f"net.ipv4.ip_forward={self._original_forwarding}"],
            check=False,
        )

        self._rules_applied.clear()
        self._routes_applied.clear()
        logger.info("NAT manager cleaned up")

    @staticmethod
    def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run a system command."""
        logger.debug(f"Running: {' '.join(cmd)}")
        return subprocess.run(cmd, capture_output=True, text=True, check=check)
