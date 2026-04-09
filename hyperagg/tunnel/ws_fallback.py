"""
WebSocket Fallback Tunnel — TCP-based fallback when UDP is blocked.

Some networks (corporate firewalls, restrictive ISPs) block UDP entirely.
This module encapsulates HyperAgg packets inside WebSocket frames over
TCP port 443, which is almost never blocked.

Use when: UDP keepalives time out 3+ times on all paths.
"""

import asyncio
import logging
import struct
from typing import Optional

logger = logging.getLogger("hyperagg.tunnel.ws_fallback")


class WSFallbackClient:
    """WebSocket-based tunnel client — fallback when UDP is blocked."""

    def __init__(self, server_url: str, encryption_key: str = ""):
        self._url = server_url  # ws://vps:8080/ws/tunnel
        self._key = encryption_key
        self._ws = None
        self._connected = False
        self._rx_queue: asyncio.Queue = asyncio.Queue(maxsize=4096)

    async def connect(self) -> bool:
        """Establish WebSocket tunnel connection."""
        try:
            import aiohttp
            self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(self._url, timeout=10)
            self._connected = True
            logger.info(f"WS fallback connected to {self._url}")
            return True
        except Exception as e:
            logger.warning(f"WS fallback connect failed: {e}")
            self._connected = False
            return False

    async def send(self, data: bytes) -> bool:
        """Send a packet through the WebSocket tunnel."""
        if not self._connected or not self._ws:
            return False
        try:
            await self._ws.send_bytes(data)
            return True
        except Exception:
            self._connected = False
            return False

    async def recv(self, timeout: float = 1.0) -> Optional[bytes]:
        """Receive a packet from the WebSocket tunnel."""
        if not self._connected or not self._ws:
            return None
        try:
            msg = await asyncio.wait_for(self._ws.receive(), timeout=timeout)
            if msg.type == 2:  # BINARY
                return msg.data
            return None
        except asyncio.TimeoutError:
            return None
        except Exception:
            self._connected = False
            return None

    async def close(self) -> None:
        if self._ws:
            await self._ws.close()
        if hasattr(self, '_session') and self._session:
            await self._session.close()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected


class WSFallbackServer:
    """WebSocket tunnel endpoint on the VPS — receives fallback connections."""

    def __init__(self):
        self._clients: dict[str, object] = {}

    async def handle_ws_tunnel(self, ws) -> None:
        """Handle a WebSocket tunnel connection from an edge client."""
        from fastapi import WebSocket, WebSocketDisconnect

        await ws.accept()
        client_id = f"ws-{id(ws)}"
        logger.info(f"WS tunnel client connected: {client_id}")

        try:
            while True:
                data = await ws.receive_bytes()
                # Forward the encapsulated packet to the normal server RX pipeline
                # The caller (api.py) should route this to server._on_packet_received()
                yield data
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug(f"WS tunnel error: {e}")
        finally:
            logger.info(f"WS tunnel client disconnected: {client_id}")
