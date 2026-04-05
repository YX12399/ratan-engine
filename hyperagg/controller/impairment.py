"""
Network Impairment Controller — add/remove latency, loss, or block paths.

Uses Linux tc (traffic control) to apply network impairments on specific
interfaces. This makes live demos compelling — instead of unplugging cables,
click a button to simulate Starlink dropout.
"""

import logging
import subprocess
from typing import Optional

logger = logging.getLogger("hyperagg.controller.impairment")


class ImpairmentController:
    """Controls network impairments via Linux tc for demo purposes."""

    def __init__(self):
        self._state: dict[int, dict] = {}  # path_id -> {action, value, interface}
        self._path_interfaces: dict[int, str] = {}

    def register_path(self, path_id: int, interface: str) -> None:
        self._path_interfaces[path_id] = interface
        self._state[path_id] = {"action": "clear", "detail": "No impairment"}

    def apply(self, path_id: int, action: str, **kwargs) -> dict:
        """
        Apply an impairment to a path.

        Actions:
            latency: add delay (value_ms)
            loss: add packet loss (value_pct)
            down: block all traffic (100% loss)
            clear: remove all impairments
        """
        iface = self._path_interfaces.get(path_id)
        if not iface:
            return {"status": "error", "detail": f"Path {path_id} not registered"}

        try:
            if action == "clear":
                self._tc_clear(iface)
                self._state[path_id] = {"action": "clear", "detail": "No impairment"}
            elif action == "latency":
                ms = kwargs.get("value_ms", 100)
                self._tc_clear(iface)
                self._tc_netem(iface, f"delay {ms}ms")
                self._state[path_id] = {"action": "latency", "detail": f"+{ms}ms latency"}
            elif action == "loss":
                pct = kwargs.get("value_pct", 5)
                self._tc_clear(iface)
                self._tc_netem(iface, f"loss {pct}%")
                self._state[path_id] = {"action": "loss", "detail": f"{pct}% packet loss"}
            elif action == "down":
                self._tc_clear(iface)
                self._tc_netem(iface, "loss 100%")
                self._state[path_id] = {"action": "down", "detail": "Path blocked (100% loss)"}
            else:
                return {"status": "error", "detail": f"Unknown action: {action}"}

            logger.info(f"Impairment on path {path_id} ({iface}): {self._state[path_id]['detail']}")
            return {"status": "ok", **self._state[path_id]}

        except Exception as e:
            return {"status": "error", "detail": str(e)}

    def _tc_clear(self, iface: str) -> None:
        subprocess.run(
            ["tc", "qdisc", "del", "dev", iface, "root"],
            capture_output=True,
        )

    def _tc_netem(self, iface: str, params: str) -> None:
        cmd = ["tc", "qdisc", "replace", "dev", iface, "root", "netem"] + params.split()
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"tc failed: {result.stderr}")

    def get_state(self) -> dict:
        return dict(self._state)

    def clear_all(self) -> None:
        for pid in list(self._path_interfaces):
            self.apply(pid, "clear")
