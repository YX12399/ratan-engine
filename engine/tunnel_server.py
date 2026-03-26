"""
Tunnel Server — runs on the Hetzner VPS.
Accepts MPTCP subflows from edge clients, reassembles traffic,
and forwards to the actual destination. This is the aggregation endpoint.

Architecture:
  Edge Client --[Starlink]--> VPS Tunnel Server --> Internet
  Edge Client --[Cellular]--> VPS Tunnel Server --> Internet

The server sees packets from both paths and reassembles them in order.
It also runs the health probe responder and feeds RTT data to the monitor.
"""

import asyncio
import struct
import time
import logging
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger("ratan.tunnel_server")


# Packet types
PKT_DATA = 0x01
PKT_PROBE = 0x02
PKT_PROBE_ACK = 0x03
PKT_CONTROL = 0x04
PKT_KEEPALIVE = 0x05

# Header: type(1) + seq(8) + path_id_len(1) + path_id(var) + timestamp(8) + payload_len(4)
HEADER_MIN_SIZE = 22


@dataclass
class SubflowState:
    path_id: str
    reader: Optional[asyncio.StreamReader] = None
    writer: Optional[asyncio.StreamWriter] = None
    bytes_received: int = 0
    bytes_sent: int = 0
    packets_received: int = 0
    last_seen: float = 0.0
    remote_addr: str = ""


@dataclass
class ClientSession:
    client_id: str
    subflows: dict[str, SubflowState] = field(default_factory=dict)
    reassembly_buffer: dict[int, bytes] = field(default_factory=dict)
    next_expected_seq: int = 0
    created_at: float = 0.0


class TunnelServer:
    """
    Async TCP server that accepts subflow connections from edge clients.
    Each client can have multiple subflows (one per network path).
    """

    def __init__(self, config_store, health_monitor, path_balancer,
                 bind_addr: str = "0.0.0.0", data_port: int = 9000, probe_port: int = 9001,
                 tun_device=None):
        self.config = config_store
        self.health = health_monitor
        self.balancer = path_balancer
        self.bind_addr = bind_addr
        self.data_port = data_port
        self.probe_port = probe_port
        self.tun = tun_device

        self._sessions: dict[str, ClientSession] = {}
        self._server: Optional[asyncio.AbstractServer] = None
        self._probe_transport = None
        self._running = False
        self._return_seq: int = 0

    async def start(self):
        """Start the tunnel server."""
        self._running = True

        # Start TCP server for data subflows
        self._server = await asyncio.start_server(
            self._handle_subflow, self.bind_addr, self.data_port
        )
        logger.info(f"Tunnel server listening on {self.bind_addr}:{self.data_port}")

        # Start UDP probe responder
        loop = asyncio.get_event_loop()
        self._probe_transport, _ = await loop.create_datagram_endpoint(
            lambda: ProbeResponder(self.health),
            local_addr=(self.bind_addr, self.probe_port),
        )
        logger.info(f"Probe responder on {self.bind_addr}:{self.probe_port}")

        # Start return traffic loop if TUN is available
        if self.tun and self.tun.is_open:
            asyncio.create_task(self._return_traffic_loop())
            logger.info("Return traffic loop started")

        async with self._server:
            await self._server.serve_forever()

    async def stop(self):
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._probe_transport:
            self._probe_transport.close()

    async def _handle_subflow(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a new subflow connection from an edge client."""
        addr = writer.get_extra_info("peername")
        logger.info(f"New subflow connection from {addr}")

        try:
            # Handshake: client sends client_id + path_id
            handshake_timeout = self.config.get("tunnel.handshake_timeout_sec", 10)
            header = await asyncio.wait_for(reader.readexactly(2), timeout=handshake_timeout)
            client_id_len, path_id_len = struct.unpack("!BB", header)
            client_id = (await reader.readexactly(client_id_len)).decode()
            path_id = (await reader.readexactly(path_id_len)).decode()

            logger.info(f"Subflow registered: client={client_id}, path={path_id}, addr={addr}")

            # Register session
            if client_id not in self._sessions:
                self._sessions[client_id] = ClientSession(
                    client_id=client_id, created_at=time.time()
                )

            session = self._sessions[client_id]
            session.subflows[path_id] = SubflowState(
                path_id=path_id,
                reader=reader,
                writer=writer,
                remote_addr=f"{addr[0]}:{addr[1]}",
                last_seen=time.time(),
            )

            # Register path with health monitor
            self.health.register_path(path_id, path_id, addr[0], self.probe_port)

            # Send ACK
            writer.write(b"\x01")
            await writer.drain()

            # Read data loop
            while self._running:
                try:
                    read_timeout = self.config.get("tunnel.read_timeout_sec", 30)
                    raw_type = await asyncio.wait_for(reader.readexactly(1), timeout=read_timeout)
                    pkt_type = struct.unpack("!B", raw_type)[0]

                    if pkt_type == PKT_DATA:
                        await self._handle_data_packet(session, path_id, reader)
                    elif pkt_type == PKT_PROBE:
                        await self._handle_inline_probe(session, path_id, reader, writer)
                    elif pkt_type == PKT_KEEPALIVE:
                        session.subflows[path_id].last_seen = time.time()
                        self.health.record_probe(path_id, 0, True)
                    elif pkt_type == PKT_CONTROL:
                        await self._handle_control(session, path_id, reader)

                except asyncio.TimeoutError:
                    # Send keepalive
                    writer.write(struct.pack("!B", PKT_KEEPALIVE))
                    await writer.drain()

        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError) as e:
            logger.warning(f"Subflow disconnected: {addr} - {e}")
        except Exception as e:
            logger.error(f"Subflow error: {addr} - {e}", exc_info=True)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_data_packet(self, session: ClientSession, path_id: str, reader):
        """Process a data packet: seq(8) + payload_len(4) + payload."""
        header = await reader.readexactly(12)
        seq, payload_len = struct.unpack("!QI", header)
        payload = await reader.readexactly(payload_len)

        subflow = session.subflows.get(path_id)
        if subflow:
            subflow.bytes_received += payload_len
            subflow.packets_received += 1
            subflow.last_seen = time.time()

        # Record successful data receipt as implicit health signal
        self.health.record_probe(path_id, None, True)

        # Reassembly
        session.reassembly_buffer[seq] = payload

        # Deliver in-order packets
        while session.next_expected_seq in session.reassembly_buffer:
            data = session.reassembly_buffer.pop(session.next_expected_seq)
            await self._deliver_packet(session, data)
            session.next_expected_seq += 1

    async def _handle_inline_probe(self, session, path_id, reader, writer):
        """Respond to inline probe — client measures RTT."""
        ts_data = await reader.readexactly(8)
        client_ts = struct.unpack("!d", ts_data)[0]

        # Send probe ack with original timestamp
        writer.write(struct.pack("!Bd", PKT_PROBE_ACK, client_ts))
        await writer.drain()

    async def _handle_control(self, session, path_id, reader):
        """Handle control messages (config sync, status requests)."""
        len_data = await reader.readexactly(4)
        msg_len = struct.unpack("!I", len_data)[0]
        msg = await reader.readexactly(msg_len)
        # Control messages are JSON — handled by bridge layer
        logger.debug(f"Control message from {path_id}: {msg[:100]}")

    async def _deliver_packet(self, session: ClientSession, data: bytes):
        """Deliver reassembled packet — write to TUN for forwarding to internet."""
        self.balancer.record_bytes("aggregate", len(data))
        if self.tun and self.tun.is_open:
            await self.tun.write_packet(data)

    async def _return_traffic_loop(self):
        """
        Read IP packets coming back from the internet (via TUN) and send
        them back to the appropriate client through their subflows.
        """
        logger.info("Return traffic loop listening on TUN")
        while self._running and self.tun and self.tun.is_open:
            try:
                data = await self.tun.read_packet()
                if not data:
                    continue

                # For single-client setups, send to the first (only) session.
                # For multi-client, parse dest IP from IP header to map to session.
                session = self._find_session_for_return(data)
                if not session:
                    continue

                # Send return data on the best available subflow
                await self._send_return_data(session, data)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Return traffic error: {e}", exc_info=True)
                await asyncio.sleep(0.1)

    def _find_session_for_return(self, ip_packet: bytes) -> Optional[ClientSession]:
        """
        Find which client session should receive this return packet.
        For now, returns the first active session (single-client mode).
        Multi-client would parse the destination IP from the IP header.
        """
        for session in self._sessions.values():
            if session.subflows:
                return session
        return None

    async def _send_return_data(self, session: ClientSession, data: bytes):
        """Send return traffic to client through an active subflow."""
        # Pick the subflow with the most recent activity (best connected)
        best_sf = None
        best_time = 0.0
        for sf in session.subflows.values():
            if sf.writer and sf.last_seen > best_time:
                best_sf = sf
                best_time = sf.last_seen

        if not best_sf or not best_sf.writer:
            return

        try:
            header = struct.pack("!BQI", PKT_DATA, self._return_seq, len(data))
            best_sf.writer.write(header + data)
            await best_sf.writer.drain()
            best_sf.bytes_sent += len(data)
            self._return_seq += 1
            self.balancer.record_bytes(best_sf.path_id, len(data))
        except Exception as e:
            logger.warning(f"Failed to send return data on {best_sf.path_id}: {e}")

    def get_sessions(self) -> dict:
        """Get info about connected client sessions."""
        result = {}
        for cid, session in self._sessions.items():
            result[cid] = {
                "client_id": cid,
                "created_at": session.created_at,
                "subflows": {
                    pid: {
                        "path_id": sf.path_id,
                        "bytes_received": sf.bytes_received,
                        "packets_received": sf.packets_received,
                        "last_seen": sf.last_seen,
                        "remote_addr": sf.remote_addr,
                    }
                    for pid, sf in session.subflows.items()
                },
                "next_expected_seq": session.next_expected_seq,
                "buffer_size": len(session.reassembly_buffer),
            }
        return result


class ProbeResponder(asyncio.DatagramProtocol):
    """UDP probe responder — echoes probes back for RTT measurement."""

    def __init__(self, health_monitor):
        self.health = health_monitor

    def datagram_received(self, data, addr):
        if len(data) >= 16:
            seq, ts = struct.unpack("!Qd", data[:16])
            # Echo back
            self.transport.sendto(data, addr)

    def connection_made(self, transport):
        self.transport = transport
