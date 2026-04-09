"""
VPS-side Device Registry + Agent WebSocket coordinator.

Manages connected edge devices. Dashboard calls REST API to list devices
and send commands. Edge agents connect via WebSocket to report status
and receive commands.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("hyperagg.dashboard.agent_ws")


@dataclass
class DeviceEntry:
    """A registered/connected edge device."""
    device_id: str
    ws: Optional[WebSocket] = None
    connected: bool = False
    last_heartbeat: float = 0.0
    registered_at: float = 0.0
    status: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)


class DeviceRegistry:
    """Manages edge device connections and state."""

    def __init__(self):
        self._devices: dict[str, DeviceEntry] = {}
        self._provisioned_tokens: dict[str, str] = {}  # token_hash -> device_id

    # ── Device Management ──

    def get_device(self, device_id: str) -> Optional[DeviceEntry]:
        return self._devices.get(device_id)

    def list_devices(self) -> list[dict]:
        """List all devices with current status."""
        now = time.time()
        result = []
        for dev in self._devices.values():
            age = round(now - dev.last_heartbeat) if dev.last_heartbeat else None
            status = dev.status.copy() if dev.status else {}
            result.append({
                "device_id": dev.device_id,
                "connected": dev.connected,
                "last_seen_sec_ago": age,
                "tunnel_status": status.get("tunnel_status", "unknown"),
                "interfaces": status.get("interfaces", []),
                "vps_connected": dev.connected,
                "system": status.get("system", {}),
                "uptime_sec": status.get("uptime_sec", 0),
                "encryption_configured": status.get("encryption_configured", False),
            })
        return result

    async def send_command(self, device_id: str, action: str, config: dict = None) -> dict:
        """Send a command to a connected device."""
        dev = self._devices.get(device_id)
        if not dev:
            return {"status": "error", "detail": f"Device {device_id} not found"}
        if not dev.connected or not dev.ws:
            return {"status": "error", "detail": f"Device {device_id} not connected"}

        cmd = {"type": "command", "action": action}
        if config:
            cmd["config"] = config

        try:
            await dev.ws.send_json(cmd)
            logger.info(f"Command '{action}' sent to {device_id}")
            return {"status": "sent", "action": action}
        except Exception as e:
            logger.error(f"Failed to send command to {device_id}: {e}")
            dev.connected = False
            dev.ws = None
            return {"status": "error", "detail": str(e)}

    # ── Provisioning ──

    def provision_device(self, device_name: str = "") -> dict:
        """Generate a new device config bundle."""
        device_id = device_name or f"edge-{os.urandom(4).hex()}"
        encryption_key = base64.b64encode(os.urandom(32)).decode()

        # Pre-register the device
        self._devices[device_id] = DeviceEntry(
            device_id=device_id,
            registered_at=time.time(),
        )

        return {
            "device_id": device_id,
            "encryption_key": encryption_key,
            "config_yaml": (
                f"device_id: \"{device_id}\"\n"
                f"vps_host: \"{{VPS_HOST}}\"\n"
                f"vps_port: 8080\n"
                f"tunnel_port: 9999\n"
                f"encryption_key: \"{encryption_key}\"\n"
                f"auto_start: false\n"
                f"interfaces: []  # auto-detect\n"
            ),
        }

    # ── WebSocket Handler ──

    async def handle_agent_ws(self, ws: WebSocket) -> None:
        """Handle an edge agent WebSocket connection."""
        await ws.accept()
        device_id = None

        try:
            # Wait for auth message
            auth_msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
            if auth_msg.get("type") != "auth":
                await ws.close(code=4001, reason="Expected auth message")
                return

            device_id = auth_msg.get("device_id", "unknown")
            logger.info(f"Agent connected: {device_id}")

            # Register or update device
            if device_id not in self._devices:
                self._devices[device_id] = DeviceEntry(
                    device_id=device_id,
                    registered_at=time.time(),
                )

            dev = self._devices[device_id]
            dev.ws = ws
            dev.connected = True
            dev.last_heartbeat = time.time()

            # Main loop: receive heartbeats and command results
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=30)

                    if msg.get("type") == "heartbeat":
                        dev.status = msg
                        dev.last_heartbeat = time.time()

                    elif msg.get("type") == "command_result":
                        logger.info(
                            f"Command result from {device_id}: "
                            f"{msg.get('action')} → {msg.get('status')}"
                        )

                except asyncio.TimeoutError:
                    # No message for 30s — send ping to check
                    try:
                        await ws.send_json({"type": "ping"})
                    except Exception:
                        break

        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug(f"Agent WS error ({device_id}): {e}")
        finally:
            if device_id and device_id in self._devices:
                self._devices[device_id].connected = False
                self._devices[device_id].ws = None
                logger.info(f"Agent disconnected: {device_id}")
