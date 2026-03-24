"""
Tunnel Client — runs on the edge node (Raspberry Pi or ThinkPad).
Establishes MPTCP subflows over each available network interface
(Starlink + cellular) and sends traffic through the VPS tunnel server.

This is the "splitter" — it takes outbound traffic and distributes it
across paths based on directives from the path balancer.
"""

import asyncio
import struct
import time
import logging
import json
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger("ratan.tunnel_client")

PKT_DATA = 0x01
PKT_PROBE = 0x02
PKT_PROBE_ACK = 0x03
PKT_CONTROL = 0x04
PKT_KEEPALIVE = 0x05


@dataclass
class SubflowConnection:
    path_id: str
    interface: str
    reader: Optional[asyncio.StreamReader] = None
    writer: Optional[asyncio.StreamWriter] = None
    connected: bool = False
    bytes_sent: int = 0
    packets_sent: int = 0
    rtt_ms: float = 0.0
    last_probe_at: float = 0.0


class TunnelClient:
    """
    Edge-side tunnel client. Connects to VPS via multiple network interfaces.
    Distributes outbound traffic based on scheduling directives.
    """

    def __init__(self, config_store, health_monitor, path_balancer,
                 client_id: str, server_addr: str, server_data_port: int = 9000,
                 server_probe_port: int = 9001):
        self.config = config_store
        self.health = health_monitor
        self.balancer = path_balancer
        self.client_id = client_id
        self.server_addr = server_addr
        self.server_data_port = server_data_port
        self.server_probe_port = server_probe_port

        self._subflows: dict[str, SubflowConnection] = {}
        self._seq = 0
        self._running = False

    async def add_interface(self, path_id: str, interface: str, local_addr: Optional[str] = None):
        """Register a network interface as a path and connect."""
        self._subflows[path_id] = SubflowConnection(
            path_id=path_id,
            interface=interface,
        )

        # Register with health monitor
        self.health.register_path(path_id, interface, self.server_addr, self.server_probe_port)

        # Connect subflow
        await self._connect_subflow(path_id, local_addr)

    async def _connect_subflow(self, path_id: str, local_addr: Optional[str] = None):
        """Establish TCP connection to VPS for a specific path."""
        sf = self._subflows[path_id]
        try:
            reader, writer = await asyncio.open_connection(
                self.server_addr, self.server_data_port,
                local_addr=(local_addr, 0) if local_addr else None,
            )

            # Handshake
            client_bytes = self.client_id.encode()
            path_bytes = path_id.encode()
            writer.write(struct.pack("!BB", len(client_bytes), len(path_bytes)))
            writer.write(client_bytes)
            writer.write(path_bytes)
            await writer.drain()

            # Wait for ACK
            ack = await asyncio.wait_for(reader.readexactly(1), timeout=10)
            if ack != b"\x01":
                raise ConnectionError("Handshake failed")

            sf.reader = reader
            sf.writer = writer
            sf.connected = True
            logger.info(f"Subflow connected: path={path_id}, interface={sf.interface}")

        except Exception as e:
            logger.error(f"Failed to connect subflow {path_id}: {e}")
            sf.connected = False

    async def send(self, data: bytes):
        """
        Send data through the tunnel, distributed across active subflows
        based on current scheduling directives.
        """
        directives = self.balancer.get_directives()
        active = [(pid, d) for pid, d in directives.items()
                   if d.get("active") and pid in self._subflows
                   and self._subflows[pid].connected]

        if not active:
            logger.warning("No active subflows available")
            return

        # Weighted distribution
        total_weight = sum(d["weight"] for _, d in active)
        if total_weight <= 0:
            # Fallback: equal distribution
            for pid, _ in active:
                await self._send_on_path(pid, data, self._seq)
                self._seq += 1
            return

        # For simplicity, pick path probabilistically based on weight
        # In production, this would chunk large payloads across paths
        import random
        rand = random.random() * total_weight
        cumulative = 0
        chosen_pid = active[0][0]
        for pid, d in active:
            cumulative += d["weight"]
            if rand <= cumulative:
                chosen_pid = pid
                break

        await self._send_on_path(chosen_pid, data, self._seq)
        self._seq += 1

        # If reinjection is enabled for the chosen path, also send on backup
        chosen_directive = directives.get(chosen_pid, {})
        if chosen_directive.get("reinjection"):
            for pid, d in active:
                if pid != chosen_pid:
                    await self._send_on_path(pid, data, self._seq - 1)
                    break

    async def _send_on_path(self, path_id: str, data: bytes, seq: int):
        """Send a single packet on a specific path."""
        sf = self._subflows.get(path_id)
        if not sf or not sf.connected or not sf.writer:
            return

        try:
            header = struct.pack("!BQI", PKT_DATA, seq, len(data))
            sf.writer.write(header + data)
            await sf.writer.drain()
            sf.bytes_sent += len(data)
            sf.packets_sent += 1
        except Exception as e:
            logger.error(f"Send failed on {path_id}: {e}")
            sf.connected = False
            self.health.record_probe(path_id, None, False)

    async def start_probing(self):
        """Start inline probe loop on all subflows."""
        self._running = True
        tasks = [self._probe_loop(pid) for pid in self._subflows]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _probe_loop(self, path_id: str):
        """Send periodic probes to measure RTT."""
        while self._running:
            sf = self._subflows.get(path_id)
            if not sf or not sf.connected:
                await asyncio.sleep(1)
                continue

            interval = self.config.get("path_health.probe_interval_ms", 50) / 1000
            try:
                ts = time.time()
                sf.writer.write(struct.pack("!Bd", PKT_PROBE, ts))
                await sf.writer.drain()

                # Read probe ack
                raw = await asyncio.wait_for(sf.reader.readexactly(9), timeout=2)
                pkt_type, echo_ts = struct.unpack("!Bd", raw)
                if pkt_type == PKT_PROBE_ACK:
                    rtt = (time.time() - echo_ts) * 1000
                    sf.rtt_ms = rtt
                    self.health.record_probe(path_id, rtt, True)

            except asyncio.TimeoutError:
                self.health.record_probe(path_id, None, False)
            except Exception:
                self.health.record_probe(path_id, None, False)

            sf.last_probe_at = time.time()
            await asyncio.sleep(interval)

    async def stop(self):
        self._running = False
        for sf in self._subflows.values():
            if sf.writer:
                sf.writer.close()
                try:
                    await sf.writer.wait_closed()
                except Exception:
                    pass

    def get_status(self) -> dict:
        """Get client status for all subflows."""
        return {
            "client_id": self.client_id,
            "server": f"{self.server_addr}:{self.server_data_port}",
            "subflows": {
                pid: {
                    "path_id": sf.path_id,
                    "interface": sf.interface,
                    "connected": sf.connected,
                    "bytes_sent": sf.bytes_sent,
                    "packets_sent": sf.packets_sent,
                    "rtt_ms": round(sf.rtt_ms, 2),
                }
                for pid, sf in self._subflows.items()
            },
        }
