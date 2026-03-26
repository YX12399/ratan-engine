"""
Interface Manager — discovers network interfaces, resolves IPs/gateways,
and configures per-interface policy routing so each tunnel subflow
uses its dedicated physical path (Starlink via wlan0, Cellular via usb0).

Without policy routing, both subflows would use the kernel's default route
and travel over the same physical interface.
"""

import subprocess
import re
import time
import threading
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("ratan.interface_manager")


@dataclass
class InterfaceInfo:
    name: str
    ip_addr: str = ""
    gateway: str = ""
    is_up: bool = False
    routing_table: int = 0
    last_seen: float = 0.0


class InterfaceManager:
    """
    Manages network interfaces for RATAN's multi-path tunnel.
    Discovers IPs, sets up policy routing tables, monitors link state.
    """

    def __init__(self, config_store):
        self.config = config_store
        self._interfaces: dict[str, InterfaceInfo] = {}
        self._lock = threading.Lock()
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._rules_applied: list[list[str]] = []
        self._routes_applied: list[list[str]] = []

    def discover_interface(self, path_id: str, iface_name: str) -> Optional[InterfaceInfo]:
        """
        Discover IP and gateway for a named interface.
        Returns InterfaceInfo or None if interface not found.
        """
        ip_addr = self._get_interface_ip(iface_name)
        gateway = self._get_interface_gateway(iface_name)
        is_up = self._is_interface_up(iface_name)

        if not ip_addr:
            logger.warning(f"Interface {iface_name} has no IP address")
            return None

        table_base = self.config.get("interfaces.routing_table_base", 100)
        table_num = table_base + len(self._interfaces)

        info = InterfaceInfo(
            name=iface_name,
            ip_addr=ip_addr,
            gateway=gateway,
            is_up=is_up,
            routing_table=table_num,
            last_seen=time.time(),
        )

        with self._lock:
            self._interfaces[path_id] = info

        logger.info(f"Discovered {path_id}: {iface_name} ip={ip_addr} gw={gateway} up={is_up} table={table_num}")
        return info

    def setup_policy_routing(self, path_id: str) -> bool:
        """
        Set up policy routing for an interface so its traffic uses a dedicated
        routing table. This ensures subflows bind to the correct physical path.
        """
        with self._lock:
            info = self._interfaces.get(path_id)
            if not info or not info.ip_addr:
                return False

        # Add routing rule: traffic from this IP uses this table
        rule = ["ip", "rule", "add", "from", info.ip_addr, "table", str(info.routing_table)]
        if self._run(rule):
            self._rules_applied.append(rule)
        else:
            return False

        # Add default route in the per-interface table
        if info.gateway:
            route = [
                "ip", "route", "add", "default",
                "via", info.gateway, "dev", info.name,
                "table", str(info.routing_table),
            ]
            if self._run(route):
                self._routes_applied.append(route)

        logger.info(f"Policy routing for {path_id}: {info.ip_addr} → table {info.routing_table} via {info.gateway}")
        return True

    def get_source_addr(self, path_id: str) -> Optional[str]:
        """Get the source IP address for a path's interface (for subflow binding)."""
        with self._lock:
            info = self._interfaces.get(path_id)
            return info.ip_addr if info else None

    def get_all_interfaces(self) -> dict[str, dict]:
        """Get status of all managed interfaces."""
        with self._lock:
            return {
                pid: {
                    "name": info.name,
                    "ip_addr": info.ip_addr,
                    "gateway": info.gateway,
                    "is_up": info.is_up,
                    "routing_table": info.routing_table,
                }
                for pid, info in self._interfaces.items()
            }

    def start_monitoring(self, health_monitor=None, interval_sec: float = 5.0) -> None:
        """Start background thread to monitor interface state changes."""
        self._running = True
        self._health_monitor = health_monitor

        def monitor_loop():
            while self._running:
                with self._lock:
                    for path_id, info in self._interfaces.items():
                        was_up = info.is_up
                        info.is_up = self._is_interface_up(info.name)

                        # Detect state change
                        if was_up and not info.is_up:
                            logger.warning(f"Interface DOWN: {info.name} ({path_id})")
                            if self._health_monitor:
                                self._health_monitor.record_probe(path_id, None, False)
                        elif not was_up and info.is_up:
                            logger.info(f"Interface UP: {info.name} ({path_id})")
                            # Re-resolve IP in case it changed (DHCP)
                            new_ip = self._get_interface_ip(info.name)
                            if new_ip and new_ip != info.ip_addr:
                                logger.info(f"IP changed for {info.name}: {info.ip_addr} → {new_ip}")
                                info.ip_addr = new_ip

                        info.last_seen = time.time()

                time.sleep(interval_sec)

        self._monitor_thread = threading.Thread(target=monitor_loop, daemon=True, name="iface-monitor")
        self._monitor_thread.start()

    def stop(self) -> None:
        self._running = False

    def cleanup(self) -> None:
        """Remove all policy routing rules we added."""
        for rule in reversed(self._rules_applied):
            delete_rule = list(rule)
            try:
                idx = delete_rule.index("add")
                delete_rule[idx] = "del"
            except ValueError:
                continue
            self._run(delete_rule, check=False)

        for route in reversed(self._routes_applied):
            delete_route = list(route)
            try:
                idx = delete_route.index("add")
                delete_route[idx] = "del"
            except ValueError:
                continue
            self._run(delete_route, check=False)

        self._rules_applied.clear()
        self._routes_applied.clear()
        logger.info("Interface manager cleaned up")

    @staticmethod
    def _get_interface_ip(iface_name: str) -> str:
        """Get the primary IPv4 address of an interface."""
        try:
            result = subprocess.run(
                ["ip", "-4", "-o", "addr", "show", iface_name],
                capture_output=True, text=True, check=True,
            )
            match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", result.stdout)
            return match.group(1) if match else ""
        except Exception:
            return ""

    @staticmethod
    def _get_interface_gateway(iface_name: str) -> str:
        """Get the default gateway for an interface from ip route."""
        try:
            result = subprocess.run(
                ["ip", "route", "show", "dev", iface_name],
                capture_output=True, text=True, check=True,
            )
            for line in result.stdout.splitlines():
                if line.startswith("default"):
                    match = re.search(r"via (\d+\.\d+\.\d+\.\d+)", line)
                    if match:
                        return match.group(1)
            return ""
        except Exception:
            return ""

    @staticmethod
    def _is_interface_up(iface_name: str) -> bool:
        """Check if an interface is up and has carrier."""
        try:
            result = subprocess.run(
                ["ip", "link", "show", iface_name],
                capture_output=True, text=True, check=True,
            )
            return "state UP" in result.stdout
        except Exception:
            return False

    @staticmethod
    def _run(cmd: list[str], check: bool = True) -> bool:
        """Run a system command, return success."""
        try:
            logger.debug(f"Running: {' '.join(cmd)}")
            subprocess.run(cmd, capture_output=True, text=True, check=check)
            return True
        except subprocess.CalledProcessError as e:
            logger.warning(f"Command failed: {' '.join(cmd)} — {e.stderr.strip()}")
            return False
