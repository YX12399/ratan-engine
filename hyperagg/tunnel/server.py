"""
Tunnel Server — VPS-side UDP bonding receiver.

Receives bonded packets from both paths, reassembles them,
and forwards to the internet (or back to the edge node for return traffic).

Mirrors the client architecture:
  1. RX from edge: both paths → dedup → FEC decode → reorder → TUN
  2. TX to edge: TUN → encrypt → FEC → send on BOTH paths back
  3. Keepalive echo: respond to probes for RTT measurement
"""

import asyncio
import json
import logging
import socket
import subprocess
import time
from typing import Optional

from hyperagg.tunnel.packet import (
    HEADER_SIZE, HyperAggPacket, set_connection_start, get_timestamp_us,
)
from hyperagg.tunnel.crypto import PacketCrypto
from hyperagg.fec.fec_engine import FecEngine
from hyperagg.scheduler.path_monitor import PathMonitor

logger = logging.getLogger("hyperagg.tunnel.server")


class ClientSession:
    """State for a connected edge client."""

    def __init__(self, client_id: str):
        self.client_id = client_id
        self.path_addrs: dict[int, tuple[str, int]] = {}  # path_id → (ip, port)
        self.created_at = time.monotonic()
        self.last_seen = time.monotonic()
        self.packets_received = 0
        self.packets_sent = 0


class ReorderBuffer:
    """Server-side reorder buffer (same logic as client)."""

    def __init__(self, window_size: int = 1024, timeout_ms: float = 100.0):
        self._window = window_size
        self._timeout_ms = timeout_ms
        self._buffer: dict[int, bytes] = {}
        self._next_seq = 0
        self._timestamps: dict[int, float] = {}
        self._seen: set[int] = set()

    def is_duplicate(self, global_seq: int) -> bool:
        if global_seq < self._next_seq:
            return True
        return global_seq in self._seen

    def insert(self, global_seq: int, payload: bytes) -> list[bytes]:
        if self.is_duplicate(global_seq):
            return []

        self._seen.add(global_seq)
        if len(self._seen) > self._window * 2:
            cutoff = self._next_seq
            self._seen = {s for s in self._seen if s >= cutoff}

        self._buffer[global_seq] = payload
        self._timestamps[global_seq] = time.monotonic()
        return self._deliver_in_order()

    def check_timeouts(self) -> list[bytes]:
        now = time.monotonic()
        cutoff = now - (self._timeout_ms / 1000.0)
        delivered = []
        while self._next_seq not in self._buffer:
            if not self._buffer:
                break
            ts = self._timestamps.get(min(self._buffer.keys()), now)
            if ts > cutoff:
                break
            self._next_seq += 1
        delivered.extend(self._deliver_in_order())
        return delivered

    def _deliver_in_order(self) -> list[bytes]:
        delivered = []
        while self._next_seq in self._buffer:
            delivered.append(self._buffer.pop(self._next_seq))
            self._timestamps.pop(self._next_seq, None)
            self._next_seq += 1
        return delivered

    @property
    def depth(self) -> int:
        return len(self._buffer)


class TunnelServer:
    """VPS-side UDP bonding tunnel server."""

    def __init__(self, config: dict):
        self._config = config
        vps_cfg = config.get("vps", {})
        tunnel_cfg = config.get("tunnel", {})
        server_cfg = config.get("server", {})

        self._bind_addr = server_cfg.get("host", "0.0.0.0")
        self._bind_port = vps_cfg.get("tunnel_port", 9999)

        # Encryption
        key_str = vps_cfg.get("encryption_key", "")
        if key_str and not key_str.startswith("$"):
            import base64
            self._crypto = PacketCrypto(base64.b64decode(key_str))
        else:
            self._crypto = None

        # FEC
        self._fec = FecEngine(config)

        # Path monitor (server-side, for return traffic scheduling)
        self._monitor = PathMonitor(config)

        # Reorder + dedup
        self._reorder = ReorderBuffer(
            window_size=tunnel_cfg.get("sequence_window", 1024),
            timeout_ms=tunnel_cfg.get("reorder_timeout_ms", 100),
        )

        # Client sessions
        self._sessions: dict[str, ClientSession] = {}
        self._addr_to_session: dict[tuple[str, int], ClientSession] = {}

        # TUN device (set externally)
        self.tun = None

        # UDP socket
        self._sock: Optional[socket.socket] = None

        # Return traffic sequencing
        self._return_global_seq = 0
        self._return_path_seq = 0

        # State
        self._running = False

        # Metrics
        self.packets_received = 0
        self.packets_sent = 0
        self.fec_recoveries = 0
        self.duplicates_dropped = 0

    async def start(self) -> None:
        """Start the tunnel server."""
        self._running = True
        set_connection_start()

        # Create UDP socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        except OSError:
            pass
        self._sock.bind((self._bind_addr, self._bind_port))

        logger.info(f"Tunnel server listening on {self._bind_addr}:{self._bind_port}")

        # Setup NAT
        await self._setup_nat()

        # Register UDP socket reader
        loop = asyncio.get_event_loop()
        loop.add_reader(self._sock.fileno(), self._on_udp_readable)

        tasks = [
            self._reorder_timeout_loop(),
        ]

        if self.tun and self.tun.is_open:
            tasks.append(self._return_traffic_loop())

        logger.info("Tunnel server started")
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                loop = asyncio.get_event_loop()
                loop.remove_reader(self._sock.fileno())
            except Exception:
                pass
            self._sock.close()

    async def _setup_nat(self) -> None:
        """Enable IP forwarding and NAT for tunnel traffic."""
        try:
            subprocess.run(
                ["sysctl", "-w", "net.ipv4.ip_forward=1"],
                capture_output=True,
            )
            # MASQUERADE for tunnel subnet
            tunnel_subnet = self._config.get("tunnel", {}).get(
                "internal_subnet", "10.99.0.0/30"
            )
            subprocess.run(
                ["iptables", "-t", "nat", "-C", "POSTROUTING",
                 "-s", tunnel_subnet, "-j", "MASQUERADE"],
                capture_output=True,
            )
            # Only add if not already present (check returned non-zero)
            result = subprocess.run(
                ["iptables", "-t", "nat", "-C", "POSTROUTING",
                 "-s", tunnel_subnet, "-j", "MASQUERADE"],
                capture_output=True,
            )
            if result.returncode != 0:
                subprocess.run(
                    ["iptables", "-t", "nat", "-A", "POSTROUTING",
                     "-s", tunnel_subnet, "-j", "MASQUERADE"],
                    capture_output=True,
                )
                logger.info(f"NAT configured for {tunnel_subnet}")
        except Exception as e:
            logger.warning(f"NAT setup failed (may need root): {e}")

    def _on_udp_readable(self) -> None:
        """Callback when UDP socket has data."""
        try:
            data, addr = self._sock.recvfrom(2048)
        except (OSError, BlockingIOError):
            return

        try:
            pkt = HyperAggPacket.deserialize(data)
        except ValueError as e:
            logger.debug(f"Invalid packet from {addr}: {e}")
            return

        # Handle keepalive — echo back immediately
        if pkt.is_keepalive:
            self._handle_keepalive(pkt, addr)
            return

        # Handle handshake
        if pkt.is_control:
            self._handle_control(pkt, addr)
            return

        # Map addr to session
        session = self._get_or_create_session(addr, pkt.path_id)
        session.last_seen = time.monotonic()
        session.packets_received += 1

        # Dedup
        if self._reorder.is_duplicate(pkt.global_seq):
            self.duplicates_dropped += 1
            return

        # Decrypt
        payload = pkt.payload
        if self._crypto and not pkt.is_fec_parity:
            try:
                payload = self._crypto.decrypt(
                    pkt.payload, pkt.header_bytes(),
                    pkt.global_seq, pkt.path_id,
                )
            except Exception:
                logger.warning(f"Decrypt failed for seq {pkt.global_seq}")
                return

        # FEC decode
        if pkt.fec_group_size > 0:
            recovered = self._fec.decode_packet(
                pkt.fec_group_id, pkt.fec_index, pkt.fec_group_size,
                pkt.is_fec_parity, payload if pkt.is_fec_parity else pkt.payload,
            )
            for _, rec_payload in recovered:
                self.fec_recoveries += 1
                if self.tun and self.tun.is_open:
                    self.tun.write_packet_sync(rec_payload)

        if pkt.is_fec_parity:
            return

        # Reorder and deliver to TUN
        delivered = self._reorder.insert(pkt.global_seq, payload)
        for pld in delivered:
            self.packets_received += 1
            if self.tun and self.tun.is_open:
                self.tun.write_packet_sync(pld)

    def _handle_keepalive(self, pkt: HyperAggPacket, addr: tuple) -> None:
        """Echo keepalive back with same timestamp."""
        response = HyperAggPacket.create_keepalive(pkt.path_id, pkt.seq)
        # Copy original timestamp so client can compute RTT
        response.timestamp_us = pkt.timestamp_us
        wire = response.serialize()
        try:
            self._sock.sendto(wire, addr)
        except OSError:
            pass

        # Track this address for the path
        session = self._get_or_create_session(addr, pkt.path_id)
        session.path_addrs[pkt.path_id] = addr

    def _handle_control(self, pkt: HyperAggPacket, addr: tuple) -> None:
        """Handle handshake/control messages."""
        try:
            msg = json.loads(pkt.payload.decode())
            if msg.get("type") == "handshake":
                client_id = f"client-{addr[0]}"
                session = self._get_or_create_session(addr, pkt.path_id)
                session.path_addrs[pkt.path_id] = addr
                logger.info(
                    f"Handshake from {client_id} on path {pkt.path_id}: "
                    f"fec={msg.get('fec_mode')}"
                )
        except Exception as e:
            logger.debug(f"Control parse error: {e}")

    def _get_or_create_session(
        self, addr: tuple, path_id: int
    ) -> ClientSession:
        """Get or create a client session from source address."""
        if addr in self._addr_to_session:
            return self._addr_to_session[addr]

        client_id = f"client-{addr[0]}"
        if client_id not in self._sessions:
            self._sessions[client_id] = ClientSession(client_id)
        session = self._sessions[client_id]
        session.path_addrs[path_id] = addr
        self._addr_to_session[addr] = session
        return session

    async def _return_traffic_loop(self) -> None:
        """Read return traffic from TUN and send back to client."""
        while self._running and self.tun and self.tun.is_open:
            try:
                ip_packet = await self.tun.read_packet()
                await self._send_return(ip_packet)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Return traffic error: {e}")
                await asyncio.sleep(0.001)

    async def _send_return(self, payload: bytes) -> None:
        """Send return traffic to all known client paths."""
        # Find the active session
        session = self._find_active_session()
        if not session or not session.path_addrs:
            return

        # Build packet
        pkt = HyperAggPacket.create_data(
            payload=payload,
            path_id=0,
            seq=self._return_path_seq,
            global_seq=self._return_global_seq,
        )

        # Encrypt
        if self._crypto:
            encrypted = self._crypto.encrypt(
                pkt.payload, pkt.header_bytes(),
                pkt.global_seq, pkt.path_id,
            )
            pkt.payload = encrypted
            pkt.payload_len = len(encrypted)

        wire = pkt.serialize()

        # Send to ALL client path addresses (client deduplicates)
        for path_id, addr in session.path_addrs.items():
            try:
                self._sock.sendto(wire, addr)
                self.packets_sent += 1
            except OSError as e:
                logger.debug(f"Return send failed to {addr}: {e}")

        self._return_global_seq += 1
        self._return_path_seq += 1

    def _find_active_session(self) -> Optional[ClientSession]:
        """Find the most recently active client session."""
        if not self._sessions:
            return None
        return max(self._sessions.values(), key=lambda s: s.last_seen)

    async def _reorder_timeout_loop(self) -> None:
        """Periodically check reorder buffer and expire FEC groups."""
        while self._running:
            delivered = self._reorder.check_timeouts()
            for payload in delivered:
                self.packets_received += 1
                if self.tun and self.tun.is_open:
                    self.tun.write_packet_sync(payload)

            self._fec.expire_groups()
            await asyncio.sleep(0.05)

    def get_sessions(self) -> dict:
        return {
            cid: {
                "client_id": s.client_id,
                "paths": {str(k): v for k, v in s.path_addrs.items()},
                "packets_received": s.packets_received,
                "packets_sent": s.packets_sent,
                "age_sec": round(time.monotonic() - s.created_at, 1),
            }
            for cid, s in self._sessions.items()
        }

    def get_metrics(self) -> dict:
        return {
            "packets_received": self.packets_received,
            "packets_sent": self.packets_sent,
            "fec_recoveries": self.fec_recoveries,
            "duplicates_dropped": self.duplicates_dropped,
            "reorder_buffer_depth": self._reorder.depth,
            "fec_mode": self._fec.current_mode,
            "active_sessions": len(self._sessions),
        }


if __name__ == "__main__":
    print("Tunnel Server module loaded successfully.")
    print("Use: python -m hyperagg --mode server --config config.yaml")
