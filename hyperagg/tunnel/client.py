"""
Tunnel Client — Edge-side UDP bonding tunnel.

Reads packets from TUN, passes them through FEC + AI scheduler,
and sends them over multiple UDP paths to the VPS simultaneously.

Integrates all 7 networking modules:
  1. AdaptiveReorderBuffer (timeout from path differential + jitter)
  2. BandwidthEstimator + TokenBucketPacer (capacity-aware sending)
  3. AdaptiveFecSizer (burst-loss-aware FEC group sizing)
  4. AIMDController + TierBandwidthManager (congestion control)
  5. OWDEstimator (asymmetric delay detection)
  6. ThroughputCalculator (effective throughput accounting)
  7. SessionManager (reconnect without state loss)
"""

import asyncio
import json
import logging
import socket
import struct
import time
from typing import Optional

from hyperagg.tunnel.packet import (
    HEADER_SIZE, HyperAggPacket, set_connection_start, get_timestamp_us,
)
from hyperagg.tunnel.crypto import PacketCrypto
from hyperagg.fec.fec_engine import FecEngine
from hyperagg.scheduler.path_monitor import PathMonitor
from hyperagg.scheduler.path_predictor import PathPredictor
from hyperagg.scheduler.path_scheduler import PathScheduler
from hyperagg.tunnel.reorder_buffer import AdaptiveReorderBuffer
from hyperagg.scheduler.bandwidth_estimator import BandwidthEstimator, TokenBucketPacer
from hyperagg.scheduler.congestion_control import AIMDController, TierBandwidthManager
from hyperagg.scheduler.owd_estimator import OWDEstimator
from hyperagg.metrics.throughput_calculator import ThroughputCalculator
from hyperagg.tunnel.session import SessionManager

logger = logging.getLogger("hyperagg.tunnel.client")


class TunnelClient:
    """Edge-side UDP bonding tunnel client."""

    def __init__(self, config: dict):
        self._config = config
        vps_cfg = config.get("vps", {})
        tunnel_cfg = config.get("tunnel", {})
        self._server_addr = vps_cfg.get("host", "127.0.0.1")
        self._server_port = vps_cfg.get("tunnel_port", 9999)

        # Encryption
        key_str = vps_cfg.get("encryption_key", "")
        if key_str and not key_str.startswith("$"):
            import base64
            self._crypto = PacketCrypto(base64.b64decode(key_str))
            logger.info("Encryption enabled (ChaCha20-Poly1305)")
        else:
            self._crypto = None
            logger.warning("Encryption DISABLED — packets sent in plaintext")

        # FEC
        self._fec = FecEngine(config)

        # Scheduler
        self._monitor = PathMonitor(config)
        self._predictor = PathPredictor(config)
        self._scheduler = PathScheduler(config, self._monitor, self._predictor)

        # Module 1: Adaptive reorder buffer
        self._reorder = AdaptiveReorderBuffer(
            window_size=tunnel_cfg.get("sequence_window", 1024),
            initial_timeout_ms=tunnel_cfg.get("reorder_timeout_ms", 100),
        )

        # Module 2: Bandwidth estimation + pacing
        self._bw_estimator = BandwidthEstimator(ewma_alpha=0.2, window_sec=1.0)
        self._pacer = TokenBucketPacer()

        # Module 4: Per-path congestion control + tier management
        self._congestion: dict[int, AIMDController] = {}
        self._tier_mgr = TierBandwidthManager()

        # Module 5: One-way delay estimation
        self._owd = OWDEstimator(ewma_alpha=0.1)

        # Module 6: Effective throughput accounting
        self._throughput = ThroughputCalculator(window_sec=5.0)

        # Module 7: Session resumption
        self._session_mgr = SessionManager(
            state_dir=tunnel_cfg.get("session_dir", "/tmp/hyperagg_sessions"),
            max_gap=tunnel_cfg.get("max_session_gap", 10000),
        )

        # Sequence counters
        self._global_seq = 0
        self._path_seqs: dict[int, int] = {}

        # UDP sockets per path
        self._sockets: dict[int, socket.socket] = {}
        self._path_names: dict[int, str] = {}

        # TUN device and packet logger (set externally)
        self.tun = None
        self.packet_logger = None

        # State
        self._running = False
        self._connected_paths: set[int] = set()  # Paths that got handshake ACK
        self._keepalive_interval_ms = tunnel_cfg.get("keepalive_interval_ms", 1000)
        self._probe_interval_ms = config.get("scheduler", {}).get("probe_interval_ms", 100)
        self._tier_reset_time = time.monotonic()

        # Metrics
        self.packets_sent_per_path: dict[int, int] = {}
        self.packets_received = 0
        self.fec_recoveries = 0
        self.duplicates_dropped = 0

    def add_path(self, path_id: int, interface: str, local_addr: str = "") -> None:
        """Register a WAN path and create a bound UDP socket."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)

        # Bind to interface if possible
        try:
            sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_BINDTODEVICE,
                interface.encode(),
            )
        except (OSError, PermissionError) as e:
            logger.warning(f"SO_BINDTODEVICE failed for {interface}: {e}")

        if local_addr:
            sock.bind((local_addr, 0))

        # Increase socket buffers
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2 * 1024 * 1024)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
        except OSError:
            pass

        self._sockets[path_id] = sock
        self._path_names[path_id] = interface
        self._path_seqs[path_id] = 0
        self.packets_sent_per_path[path_id] = 0
        self._monitor.register_path(path_id)

        # Module 2+4: init pacer and congestion control for this path
        self._pacer.set_rate(path_id, 10_000_000)  # 10 MB/s default until estimated
        self._congestion[path_id] = AIMDController(estimated_bw=10_000_000)

        logger.info(f"Path {path_id} ({interface}) registered")

    async def start(self) -> None:
        """Start all tunnel loops — handshake with ACK before data flow."""
        self._running = True
        set_connection_start()

        # Module 7: try to resume previous session, or create new
        resume_data = self._session_mgr.get_resume_data()
        if resume_data:
            resumed = self._session_mgr.try_resume(
                resume_data["session_id"], resume_data["last_global_seq"]
            )
            if resumed:
                self._global_seq = resumed.global_seq
                if resumed.path_seqs:
                    self._path_seqs = {int(k): v for k, v in resumed.path_seqs.items()}
                logger.info(f"Session resumed: seq={self._global_seq}")
            else:
                self._session_mgr.create_session()
        else:
            self._session_mgr.create_session()

        # Handshake with retry — wait for server ACK before starting data flow
        connected = await self._handshake_with_retry()
        if not connected:
            logger.error(
                f"FAILED: No response from VPS at {self._server_addr}:{self._server_port}\n"
                f"  Check: 1) VPS is running  2) UDP port {self._server_port} is open  "
                f"3) Firewall allows traffic"
            )
            # Continue anyway — keepalives will keep trying
            logger.info("Starting in degraded mode — will retry via keepalives")

        tasks = [
            self._keepalive_loop(),
            self._rx_loop(),
            self._reorder_timeout_loop(),
        ]

        if self.tun and self.tun.is_open:
            tasks.append(self._tx_loop())

        self._scheduler.update_predictions()
        logger.info("Tunnel client started")

        await asyncio.gather(*tasks, return_exceptions=True)

    async def _handshake_with_retry(self, max_retries: int = 3,
                                     timeout_sec: float = 3.0) -> bool:
        """Send handshake and wait for ACK with retry."""
        for attempt in range(max_retries):
            await self._send_handshake()
            logger.info(f"Handshake sent (attempt {attempt + 1}/{max_retries}), "
                        f"waiting {timeout_sec}s for ACK...")

            # Wait for ACK on any socket
            ack_received = await self._wait_for_ack(timeout_sec)
            if ack_received:
                return True

            if attempt < max_retries - 1:
                backoff = (attempt + 1) * 2
                logger.warning(f"No ACK received, retrying in {backoff}s...")
                await asyncio.sleep(backoff)

        return False

    async def _wait_for_ack(self, timeout_sec: float) -> bool:
        """Wait for handshake ACK from server on any path."""
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            for path_id, sock in self._sockets.items():
                try:
                    data, addr = sock.recvfrom(2048)
                    pkt = HyperAggPacket.deserialize(data)
                    if pkt.is_control:
                        try:
                            msg = json.loads(pkt.payload.decode())
                            if msg.get("type") == "handshake_ack":
                                logger.info(
                                    f"Handshake ACK received on path {path_id} "
                                    f"from {addr} (server_ip={msg.get('server_ip')})"
                                )
                                self._connected_paths.add(path_id)
                                return True
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass
                except BlockingIOError:
                    pass  # No data yet, try next socket
                except OSError:
                    pass
            await asyncio.sleep(0.1)
        return False

    async def stop(self) -> None:
        self._running = False
        for sock in self._sockets.values():
            sock.close()

    async def _send_handshake(self) -> None:
        """Send handshake control packet on all paths."""
        handshake = json.dumps({
            "type": "handshake",
            "paths": {
                str(pid): name for pid, name in self._path_names.items()
            },
            "fec_mode": self._fec.current_mode,
        }).encode()

        for path_id, sock in self._sockets.items():
            pkt = HyperAggPacket.create_control(handshake, path_id, 0)
            wire = pkt.serialize()
            try:
                sock.sendto(wire, (self._server_addr, self._server_port))
                logger.info(f"Handshake sent on path {path_id}")
            except OSError as e:
                logger.error(f"Handshake failed on path {path_id}: {e}")

    async def _tx_loop(self) -> None:
        """TX pipeline: TUN → classify → FEC → schedule → encrypt → UDP send."""
        while self._running:
            try:
                ip_packet = await self.tun.read_packet()
                await self._send_packet(ip_packet)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"TX loop error: {e}")
                await asyncio.sleep(0.001)

    async def _send_packet(self, payload: bytes) -> None:
        """Process one outbound IP packet through the full pipeline."""
        # Classify traffic tier
        tier = self._classify_traffic(payload)

        # FEC encode (may produce data + parity packets)
        fec_results = self._fec.encode_packet(payload, tier)

        for fec_payload, fec_meta in fec_results:
            # Schedule: which path(s)?
            decision = self._scheduler.schedule(self._global_seq, tier)

            for assignment in decision.assignments:
                if not assignment.send:
                    continue
                pid = assignment.path_id
                if pid not in self._sockets:
                    continue

                path_seq = self._path_seqs.get(pid, 0)

                # Build packet
                if fec_meta.get("is_parity"):
                    pkt = HyperAggPacket.create_fec_parity(
                        parity_data=fec_payload,
                        path_id=pid,
                        seq=path_seq,
                        global_seq=self._global_seq,
                        fec_group_id=fec_meta["fec_group_id"],
                        fec_index=fec_meta["fec_index"],
                        fec_group_size=fec_meta["fec_group_size"],
                    )
                else:
                    pkt = HyperAggPacket.create_data(
                        payload=fec_payload,
                        path_id=pid,
                        seq=path_seq,
                        global_seq=self._global_seq,
                        fec_group_id=fec_meta.get("fec_group_id", 0),
                        fec_index=fec_meta.get("fec_index", 0),
                        fec_group_size=fec_meta.get("fec_group_size", 0),
                    )

                # Encrypt — AAD must use the FINAL payload_len (encrypted size)
                # so receiver's deserialized header matches what we authenticated.
                if self._crypto:
                    from hyperagg.tunnel.crypto import TAG_SIZE
                    # Pre-compute encrypted length and set it BEFORE generating AAD
                    pkt.payload_len = len(pkt.payload) + TAG_SIZE
                    aad = pkt.header_bytes()
                    encrypted = self._crypto.encrypt(
                        pkt.payload, aad,
                        pkt.global_seq, pkt.path_id,
                    )
                    pkt.payload = encrypted

                # Module 4: check tier budget (never drop realtime)
                if tier != "realtime" and not self._tier_mgr.can_send(tier, len(fec_payload)):
                    continue  # Budget exhausted for this tier

                # Module 2: check pacer
                wire = pkt.serialize()
                if not self._pacer.can_send(pid, len(wire)):
                    # Try to find another path from the decision
                    sent_on_alt = False
                    for alt in decision.assignments:
                        if alt.path_id != pid and alt.send and alt.path_id in self._sockets:
                            if self._pacer.can_send(alt.path_id, len(wire)):
                                pid = alt.path_id
                                sent_on_alt = True
                                break
                    if not sent_on_alt and not self._pacer.can_send(pid, len(wire)):
                        continue  # All paths congested, skip

                # Send
                try:
                    self._sockets[pid].sendto(
                        wire, (self._server_addr, self._server_port)
                    )
                    self._pacer.consume(pid, len(wire))
                    self._path_seqs[pid] = path_seq + 1
                    self.packets_sent_per_path[pid] = (
                        self.packets_sent_per_path.get(pid, 0) + 1
                    )
                    self._monitor.record_throughput(pid, len(wire))
                    self._bw_estimator.record_ack(pid, len(wire))
                    self._tier_mgr.record_send(tier, len(fec_payload))

                    # Module 6: throughput accounting
                    is_dup = fec_meta.get("replicate", False) and len(decision.assignments) > 1
                    self._throughput.record_sent(
                        len(wire),
                        is_fec_parity=fec_meta.get("is_parity", False),
                        is_duplicate=is_dup,
                    )

                    if self.packet_logger:
                        ptype = "fec_parity" if fec_meta.get("is_parity") else "data"
                        self.packet_logger.log(
                            pkt.global_seq, pid, ptype, len(payload),
                            fec_group_id=fec_meta.get("fec_group_id", 0),
                            fec_index=fec_meta.get("fec_index", 0),
                            scheduler_reason=decision.reason,
                        )
                except OSError as e:
                    logger.warning(f"Send failed on path {pid}: {e}")

            # Module 7: periodic session state save
            if self._global_seq % 100 == 0:
                states = self._monitor.get_all_path_states()
                self._session_mgr.update(
                    global_seq=self._global_seq,
                    path_seqs=dict(self._path_seqs),
                    scheduler_mode=self._scheduler._mode,
                    path_rtt_history={
                        pid: list(s.rtt_history)
                        for pid, s in states.items()
                    },
                )

            # Module 4: reset tier window every 1 second
            now = time.monotonic()
            if now - self._tier_reset_time >= 1.0:
                self._tier_mgr.reset_window()
                self._tier_reset_time = now

            self._global_seq += 1

    async def _rx_loop(self) -> None:
        """RX pipeline: receive from all UDP sockets, reassemble, write to TUN."""
        loop = asyncio.get_event_loop()

        # Register all sockets for reading
        for path_id, sock in self._sockets.items():
            loop.add_reader(
                sock.fileno(),
                self._on_udp_readable,
                path_id,
                sock,
            )

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(0.01)

        # Clean up readers
        for sock in self._sockets.values():
            try:
                loop.remove_reader(sock.fileno())
            except Exception:
                pass

    def _on_udp_readable(self, path_id: int, sock: socket.socket) -> None:
        """Callback when a UDP socket has data."""
        try:
            data, addr = sock.recvfrom(2048)
        except (OSError, BlockingIOError):
            return

        try:
            pkt = HyperAggPacket.deserialize(data)
        except ValueError as e:
            logger.debug(f"Invalid packet from {addr}: {e}")
            return

        # Handle keepalive response
        if pkt.is_keepalive:
            self._handle_keepalive_response(path_id, pkt)
            return

        # Handle control (including late handshake ACKs)
        if pkt.is_control:
            try:
                msg = json.loads(pkt.payload.decode())
                if msg.get("type") == "handshake_ack":
                    self._connected_paths.add(path_id)
                    logger.info(f"Handshake ACK on path {path_id} (late)")
            except Exception:
                pass
            return

        # Module 2: record bandwidth from received data
        self._bw_estimator.record_ack(path_id, len(data))

        # Module 3: record successful receipt for adaptive FEC
        self._fec.record_received()

        # Module 4: successful receipt = no loss signal
        if path_id in self._congestion:
            self._congestion[path_id].on_success()

        # Dedup
        if self._reorder.is_duplicate(pkt.global_seq):
            self.duplicates_dropped += 1
            return

        # Check for sequence gaps → loss signal for AIMD + adaptive FEC
        if hasattr(self._reorder, '_next_seq') and pkt.global_seq > self._reorder._next_seq + 1:
            gap = pkt.global_seq - self._reorder._next_seq - 1
            for _ in range(min(gap, 5)):
                self._fec.record_lost()
                if path_id in self._congestion:
                    self._congestion[path_id].on_loss()

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

        # FEC decode (might recover missing packets)
        # For data packets: feed decrypted payload for FEC grouping
        # For parity packets: feed raw payload (parity is not encrypted separately)
        if pkt.fec_group_size > 0:
            recovered = self._fec.decode_packet(
                pkt.fec_group_id, pkt.fec_index, pkt.fec_group_size,
                pkt.is_fec_parity, payload,
            )
            for rec_idx, rec_payload in recovered:
                self.fec_recoveries += 1
                if self.tun and self.tun.is_open:
                    self.tun.write_packet_sync(rec_payload)
                if self.packet_logger:
                    self.packet_logger.log(
                        pkt.global_seq, path_id, "recovered",
                        len(rec_payload), fec_group_id=pkt.fec_group_id,
                    )

        # If this is a parity-only packet, don't deliver to TUN
        if pkt.is_fec_parity:
            return

        # Reorder and deliver
        delivered = self._reorder.insert(pkt.global_seq, payload)
        for pld in delivered:
            self.packets_received += 1
            if self.tun and self.tun.is_open:
                self.tun.write_packet_sync(pld)

    def _handle_keepalive_response(self, path_id: int, pkt: HyperAggPacket) -> None:
        """Process keepalive echo — calculate RTT + OWD from T1/T2/T3/T4."""
        t4_us = get_timestamp_us()  # Client receive time
        t1_us = pkt.timestamp_us     # Client send time (echoed back by server)

        # RTT from T1/T4
        if t4_us >= t1_us:
            rtt_us = t4_us - t1_us
        else:
            rtt_us = (0xFFFFFFFF - t1_us) + t4_us
        rtt_ms = rtt_us / 1000.0

        if rtt_ms < 10000:
            self._monitor.record_rtt(path_id, rtt_ms)

            # OWD: parse T2/T3 from keepalive payload (if server sent them)
            if len(pkt.payload) >= 8:
                import struct
                t2_us, t3_us = struct.unpack("!II", pkt.payload[:8])
                # Convert all to seconds for OWD estimator
                # Note: these are all uint32 microseconds since connection start
                # OWD estimator uses relative timestamps, so units don't matter
                # as long as they're consistent
                self._owd.record_timestamps(
                    path_id,
                    t1_us / 1e6, t2_us / 1e6, t3_us / 1e6, t4_us / 1e6,
                )
            else:
                # Fallback: use RTT with asymmetry assumption
                up_ratio = 0.33 if path_id == 0 else 0.45  # Starlink vs cellular
                self._owd.record_rtt_with_asymmetry(path_id, rtt_ms, up_ratio)
        else:
            self._monitor.record_loss(path_id)

    async def _keepalive_loop(self) -> None:
        """Send keepalive probes on each path periodically."""
        while self._running:
            for path_id, sock in self._sockets.items():
                seq = self._path_seqs.get(path_id, 0)
                pkt = HyperAggPacket.create_keepalive(path_id, seq)
                wire = pkt.serialize()
                try:
                    sock.sendto(wire, (self._server_addr, self._server_port))
                except OSError:
                    self._monitor.record_loss(path_id)

            await asyncio.sleep(self._keepalive_interval_ms / 1000.0)

    async def _reorder_timeout_loop(self) -> None:
        """Periodically check reorder buffer, update adaptive modules."""
        while self._running:
            delivered = self._reorder.check_timeouts()
            for payload in delivered:
                self.packets_received += 1
                if self.tun and self.tun.is_open:
                    self.tun.write_packet_sync(payload)

            # Expire stale FEC groups
            self._fec.expire_groups()

            # Update FEC mode based on current loss
            states = self._monitor.get_all_path_states()
            path_loss = {pid: s.loss_pct for pid, s in states.items()}
            self._fec.update_mode(path_loss)

            # Module 1: update adaptive reorder timeout from path differentials
            path_rtts = {pid: s.avg_rtt_ms for pid, s in states.items() if s.is_alive}
            path_jitters = {pid: s.jitter_ms for pid, s in states.items() if s.is_alive}
            # Prefer OWD download differential if available (Module 5)
            owd_data = self._owd.get_all_owd()
            if owd_data and len(owd_data) >= 2:
                path_downs = {int(pid): info["owd_down_ms"] for pid, info in owd_data.items()}
                self._reorder.update_timeout(path_downs, path_jitters)
            elif path_rtts:
                self._reorder.update_timeout(path_rtts, path_jitters)

            # Module 2: update pacer rates from bandwidth estimator
            for pid in states:
                bw = self._bw_estimator.get_estimated_bw(pid)
                if bw > 0:
                    self._pacer.set_rate(pid, bw)

            # Module 4: update AIMD from bandwidth estimator
            for pid, cc in self._congestion.items():
                bw = self._bw_estimator.get_estimated_bw(pid)
                if bw > 0:
                    cc.update_estimated_bw(bw)
            total_bw = sum(self._bw_estimator.get_estimated_bw(pid) for pid in states)
            if total_bw > 0:
                self._tier_mgr.update_capacity(total_bw)

            await asyncio.sleep(0.05)  # 50ms

    def _classify_traffic(self, ip_packet: bytes) -> str:
        """Classify an IP packet into a QoS tier by inspecting headers."""
        qos_cfg = self._config.get("qos", {})
        if not qos_cfg.get("enabled", False):
            return "bulk"

        if len(ip_packet) < 20:
            return "bulk"

        # Parse IP header
        protocol = ip_packet[9]
        if protocol == 17:  # UDP
            if len(ip_packet) >= 28:
                dst_port = struct.unpack("!H", ip_packet[22:24])[0]
                for tier_name, tier_cfg in qos_cfg.get("tiers", {}).items():
                    ports_str = tier_cfg.get("ports", "")
                    if self._port_in_range(dst_port, ports_str):
                        return tier_name
        elif protocol == 6:  # TCP
            if len(ip_packet) >= 24:
                ihl = (ip_packet[0] & 0x0F) * 4
                if len(ip_packet) >= ihl + 4:
                    dst_port = struct.unpack("!H", ip_packet[ihl + 2:ihl + 4])[0]
                    for tier_name, tier_cfg in qos_cfg.get("tiers", {}).items():
                        ports_str = tier_cfg.get("ports", "")
                        if self._port_in_range(dst_port, ports_str):
                            return tier_name

        return "bulk"

    @staticmethod
    def _port_in_range(port: int, range_str: str) -> bool:
        """Check if port is in a range like '3478-3481' or '5201'."""
        if not range_str:
            return False
        try:
            if "-" in range_str:
                low, high = range_str.split("-")
                return int(low) <= port <= int(high)
            else:
                return port == int(range_str)
        except (ValueError, TypeError):
            return False

    def get_metrics(self) -> dict:
        """Get all client metrics for dashboard, including all 7 modules."""
        states = self._monitor.get_all_path_states()
        return {
            "packets_sent_per_path": dict(self.packets_sent_per_path),
            "packets_received": self.packets_received,
            "fec_recoveries": self.fec_recoveries,
            "duplicates_dropped": self.duplicates_dropped,
            "reorder_buffer_depth": self._reorder.depth,
            "per_path_rtt_ms": {pid: s.avg_rtt_ms for pid, s in states.items()},
            "per_path_loss_pct": {pid: s.loss_pct for pid, s in states.items()},
            "per_path_jitter_ms": {pid: s.jitter_ms for pid, s in states.items()},
            "fec_mode": self._fec.current_mode,
            "fec_overhead_pct": self._fec.overhead_pct,
            "scheduler_mode": self._scheduler._mode,
            "scheduler_stats": {
                "decisions_total": self._scheduler.get_stats().decisions_total,
                "avg_decision_time_us": self._scheduler.get_stats().avg_decision_time_us,
            },
            # Module stats
            "reorder": self._reorder.get_stats(),
            "bandwidth": {pid: self._bw_estimator.get_estimated_bw_mbps(pid) for pid in states},
            "pacer": self._pacer.get_stats(),
            "congestion": {pid: cc.get_stats() for pid, cc in self._congestion.items()},
            "owd": self._owd.get_stats(),
            "throughput": self._throughput.compute(),
            "tier_manager": self._tier_mgr.get_stats(),
            "session": self._session_mgr.get_stats(),
            "adaptive_fec": self._fec.get_adaptive_stats(),
        }


if __name__ == "__main__":
    print("Tunnel Client module loaded successfully.")
    print("Use: python -m hyperagg --mode client --config config.yaml")
