"""
Network Manager — interface discovery and management.

Auto-detects available WAN interfaces, resolves IPs and gateways,
and sets up policy routing per interface. Never hardcodes interface names.
"""

import logging
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("hyperagg.controller.network")


@dataclass
class InterfaceInfo:
    """Discovered network interface state."""
    name: str
    ip_addr: str = ""
    gateway: str = ""
    is_up: bool = False
    mtu: int = 1500


class NetworkManager:
    """Discovers and manages network interfaces."""

    def __init__(self, config: dict):
        self._config = config
        self._interfaces: dict[str, InterfaceInfo] = {}

    def discover_interfaces(self) -> list[InterfaceInfo]:
        """Auto-detect all UP interfaces with IP addresses."""
        interfaces = []
        try:
            result = subprocess.run(
                ["ip", "-4", "-o", "addr", "show"],
                capture_output=True, text=True, check=True,
            )
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split()
                if len(parts) < 4:
                    continue
                iface_name = parts[1]
                # Skip loopback and tunnel interfaces
                if iface_name in ("lo",) or iface_name.startswith(("hagg", "ratan", "tun")):
                    continue

                # Extract IP
                for i, p in enumerate(parts):
                    if p == "inet":
                        ip_cidr = parts[i + 1]
                        ip_addr = ip_cidr.split("/")[0]
                        info = InterfaceInfo(
                            name=iface_name,
                            ip_addr=ip_addr,
                            is_up=True,
                        )
                        # Get gateway
                        info.gateway = self._get_gateway(iface_name)
                        info.mtu = self._get_mtu(iface_name)
                        interfaces.append(info)
                        self._interfaces[iface_name] = info
                        break

        except subprocess.CalledProcessError as e:
            logger.error(f"Interface discovery failed: {e}")

        return interfaces

    def discover_interface(self, iface_name: str) -> Optional[InterfaceInfo]:
        """Discover a specific interface by name."""
        try:
            result = subprocess.run(
                ["ip", "-4", "-o", "addr", "show", "dev", iface_name],
                capture_output=True, text=True,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None

            line = result.stdout.strip().split("\n")[0]
            match = re.search(r"inet\s+(\S+)", line)
            if not match:
                return None

            ip_addr = match.group(1).split("/")[0]
            info = InterfaceInfo(
                name=iface_name,
                ip_addr=ip_addr,
                gateway=self._get_gateway(iface_name),
                is_up=True,
                mtu=self._get_mtu(iface_name),
            )
            self._interfaces[iface_name] = info
            return info

        except subprocess.CalledProcessError:
            return None

    def _get_gateway(self, iface_name: str) -> str:
        """Find default gateway for an interface."""
        try:
            result = subprocess.run(
                ["ip", "route", "show", "dev", iface_name, "default"],
                capture_output=True, text=True,
            )
            match = re.search(r"via\s+(\S+)", result.stdout)
            if match:
                return match.group(1)
        except subprocess.CalledProcessError:
            pass
        return ""

    def _get_mtu(self, iface_name: str) -> int:
        """Get MTU for an interface."""
        try:
            result = subprocess.run(
                ["ip", "link", "show", "dev", iface_name],
                capture_output=True, text=True,
            )
            match = re.search(r"mtu\s+(\d+)", result.stdout)
            if match:
                return int(match.group(1))
        except subprocess.CalledProcessError:
            pass
        return 1500

    def setup_policy_routing(
        self, iface_name: str, table_id: int, priority: int = 100
    ) -> None:
        """Set up policy routing so traffic can be directed to specific interfaces."""
        info = self._interfaces.get(iface_name)
        if not info or not info.gateway:
            logger.warning(f"Cannot set up policy routing for {iface_name}: no gateway")
            return

        try:
            # Add routing table
            subprocess.run(
                ["ip", "route", "add", "default",
                 "via", info.gateway, "dev", iface_name,
                 "table", str(table_id)],
                capture_output=True,
            )
            # Add rule to use table for traffic from this interface's IP
            subprocess.run(
                ["ip", "rule", "add", "from", info.ip_addr,
                 "table", str(table_id), "priority", str(priority)],
                capture_output=True,
            )
            logger.info(
                f"Policy routing: {iface_name} ({info.ip_addr}) → "
                f"table {table_id} via {info.gateway}"
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Policy routing setup failed: {e}")

    def setup_lan_gateway(
        self,
        lan_interface: str,
        lan_ip: str = "192.168.50.1",
        lan_subnet: str = "192.168.50.0/24",
        tun_name: str = "hagg0",
    ) -> None:
        """
        Configure the Beelink/TP-Link as a LAN gateway so connected laptops
        route through the HyperAgg tunnel.

        Physical setup:
            Laptop → (WiFi/eth) → Beelink LAN port → HyperAgg TUN → VPS → Internet

        This sets up:
            1. LAN interface IP assignment
            2. IP forwarding
            3. FORWARD rules: LAN ↔ TUN
            4. MASQUERADE from LAN through TUN
        """
        try:
            # Assign LAN IP if not already set
            subprocess.run(
                ["ip", "addr", "add", f"{lan_ip}/24", "dev", lan_interface],
                capture_output=True,
            )
            subprocess.run(
                ["ip", "link", "set", "dev", lan_interface, "up"],
                capture_output=True,
            )
            logger.info(f"LAN interface {lan_interface} at {lan_ip}")

            # Enable IP forwarding
            subprocess.run(
                ["sysctl", "-w", "net.ipv4.ip_forward=1"],
                capture_output=True,
            )

            # FORWARD rules: allow LAN ↔ TUN traffic
            for cmd in [
                ["iptables", "-A", "FORWARD", "-i", lan_interface, "-o", tun_name, "-j", "ACCEPT"],
                ["iptables", "-A", "FORWARD", "-i", tun_name, "-o", lan_interface,
                 "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
            ]:
                subprocess.run(cmd, capture_output=True)

            # NAT: masquerade LAN traffic going through TUN
            subprocess.run(
                ["iptables", "-t", "nat", "-A", "POSTROUTING",
                 "-s", lan_subnet, "-o", tun_name, "-j", "MASQUERADE"],
                capture_output=True,
            )

            logger.info(f"LAN gateway ready: {lan_subnet} → {tun_name}")

        except Exception as e:
            logger.error(f"LAN gateway setup failed: {e}")

    def get_all_interfaces(self) -> dict[str, InterfaceInfo]:
        return dict(self._interfaces)


if __name__ == "__main__":
    nm = NetworkManager({})
    interfaces = nm.discover_interfaces()
    print(f"Discovered {len(interfaces)} interface(s):")
    for iface in interfaces:
        print(f"  {iface.name}: ip={iface.ip_addr}, gw={iface.gateway}, "
              f"mtu={iface.mtu}, up={iface.is_up}")
