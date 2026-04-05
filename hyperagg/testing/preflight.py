"""
Pre-flight connectivity checker for live demo validation.

Verifies all requirements before starting a physical demo:
interface connectivity, VPS health, TUN permissions, NAT, encryption.
"""

import asyncio
import logging
import os
import subprocess
from typing import Optional

logger = logging.getLogger("hyperagg.testing.preflight")


class PreflightChecker:
    """Verifies all requirements before starting a live demo."""

    def __init__(self, config: dict):
        self._config = config
        self._vps_host = config.get("vps", {}).get("host", "")

    async def run_all_checks(self) -> dict:
        """Returns {check_name: {status: pass/fail/warn, detail: str}}"""
        results = {}
        results["tun_permission"] = await self.check_tun_permission()
        results["encryption_key"] = await self.check_encryption_key(self._config)
        if self._vps_host and not self._vps_host.startswith("$"):
            results["vps_health"] = await self.check_vps_health(
                f"http://{self._vps_host}:{self._config.get('server', {}).get('port', 8080)}"
            )
        # Interface checks (skip if no interfaces configured)
        ifaces = self._config.get("interfaces", {}).get("wan_interfaces", [])
        for iface in ifaces:
            results[f"interface_{iface}"] = await self.check_interface_connectivity(
                iface, self._vps_host
            )
        return results

    async def check_interface_connectivity(self, iface: str, vps_ip: str) -> dict:
        """Ping VPS from specific interface."""
        if not vps_ip or vps_ip.startswith("$"):
            return {"status": "warn", "detail": "VPS IP not configured"}
        try:
            proc = await asyncio.create_subprocess_exec(
                "ping", "-I", iface, "-c", "3", "-W", "2", vps_ip,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode()
            if proc.returncode == 0:
                # Extract RTT
                for line in output.split("\n"):
                    if "avg" in line:
                        parts = line.split("=")[-1].split("/")
                        rtt = parts[1] if len(parts) > 1 else "?"
                        return {"status": "pass", "detail": f"{iface} → VPS: {rtt}ms avg RTT"}
                return {"status": "pass", "detail": f"{iface} → VPS: reachable"}
            return {"status": "fail", "detail": f"{iface} → VPS: unreachable"}
        except asyncio.TimeoutError:
            return {"status": "fail", "detail": f"{iface} → VPS: timeout"}
        except FileNotFoundError:
            return {"status": "warn", "detail": "ping command not available"}
        except Exception as e:
            return {"status": "fail", "detail": f"{iface}: {e}"}

    async def check_vps_health(self, vps_url: str) -> dict:
        """GET /api/health on VPS."""
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.get(f"{vps_url}/api/health") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("engine") == "hyperagg":
                            return {"status": "pass", "detail": f"VPS healthy (v{data.get('version', '?')})"}
                    return {"status": "fail", "detail": f"VPS returned status {resp.status}"}
        except ImportError:
            return {"status": "warn", "detail": "aiohttp not installed — skipping VPS check"}
        except Exception as e:
            return {"status": "fail", "detail": f"VPS unreachable: {e}"}

    async def check_tun_permission(self) -> dict:
        """Check if /dev/net/tun is accessible."""
        if os.path.exists("/dev/net/tun"):
            if os.access("/dev/net/tun", os.R_OK | os.W_OK):
                return {"status": "pass", "detail": "TUN device accessible"}
            elif os.geteuid() == 0:
                return {"status": "pass", "detail": "Running as root, TUN should work"}
            return {"status": "fail", "detail": "/dev/net/tun not writable — run as root"}
        return {"status": "warn", "detail": "/dev/net/tun not found (normal on non-Linux)"}

    async def check_encryption_key(self, config: dict) -> dict:
        """Verify encryption key is set and valid."""
        key_str = config.get("vps", {}).get("encryption_key", "")
        if not key_str or key_str.startswith("$"):
            return {"status": "warn", "detail": "No encryption key — traffic will be unencrypted"}
        try:
            import base64
            key = base64.b64decode(key_str)
            if len(key) == 32:
                return {"status": "pass", "detail": "256-bit ChaCha20 key configured"}
            return {"status": "fail", "detail": f"Key is {len(key)*8}-bit, expected 256-bit"}
        except Exception as e:
            return {"status": "fail", "detail": f"Invalid key: {e}"}

    async def check_nat_setup(self, tun_subnet: str = "10.99.0.0/24") -> dict:
        """Check iptables for MASQUERADE on tunnel subnet."""
        try:
            result = subprocess.run(
                ["iptables", "-t", "nat", "-L", "POSTROUTING", "-n"],
                capture_output=True, text=True,
            )
            if tun_subnet.split("/")[0] in result.stdout:
                return {"status": "pass", "detail": f"NAT configured for {tun_subnet}"}
            return {"status": "warn", "detail": f"NAT not found for {tun_subnet}"}
        except FileNotFoundError:
            return {"status": "warn", "detail": "iptables not available"}
        except Exception as e:
            return {"status": "fail", "detail": f"NAT check failed: {e}"}
