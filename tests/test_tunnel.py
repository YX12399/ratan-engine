"""Tests for tunnel client/server integration via loopback UDP."""
import asyncio
import os
import pytest
from hyperagg.tunnel.packet import HyperAggPacket, MAGIC, set_connection_start
from hyperagg.tunnel.crypto import PacketCrypto
from hyperagg.tunnel.client import TunnelClient, ReorderBuffer
from hyperagg.tunnel.server import TunnelServer


@pytest.fixture(autouse=True)
def init_time():
    set_connection_start()


class TestReorderBuffer:
    def test_in_order_delivery(self):
        buf = ReorderBuffer(window_size=64)
        result = buf.insert(0, b"pkt0")
        assert result == [b"pkt0"]
        result = buf.insert(1, b"pkt1")
        assert result == [b"pkt1"]

    def test_out_of_order_buffering(self):
        buf = ReorderBuffer(window_size=64)
        result = buf.insert(2, b"pkt2")
        assert result == []  # Gap: missing 0, 1
        result = buf.insert(1, b"pkt1")
        assert result == []  # Still missing 0
        result = buf.insert(0, b"pkt0")
        assert result == [b"pkt0", b"pkt1", b"pkt2"]

    def test_duplicate_detection(self):
        buf = ReorderBuffer(window_size=64)
        buf.insert(0, b"pkt0")
        assert buf.is_duplicate(0)
        result = buf.insert(0, b"pkt0_dup")
        assert result == []  # Duplicate dropped

    def test_timeout_delivery(self):
        buf = ReorderBuffer(window_size=64, timeout_ms=1)
        buf.insert(1, b"pkt1")  # Gap at 0
        import time
        time.sleep(0.01)  # Wait for timeout
        delivered = buf.check_timeouts()
        # Should skip seq 0 and deliver seq 1
        assert b"pkt1" in delivered

    def test_depth(self):
        buf = ReorderBuffer(window_size=64)
        buf.insert(5, b"pkt5")
        buf.insert(3, b"pkt3")
        assert buf.depth == 2


class TestTunnelClientInit:
    def test_client_creates_with_config(self):
        config = {
            "vps": {"host": "127.0.0.1", "tunnel_port": 9999},
            "tunnel": {"mtu": 1400, "sequence_window": 1024},
            "fec": {"mode": "xor", "xor_group_size": 4},
            "scheduler": {
                "mode": "weighted",
                "history_window": 50,
                "ewma_alpha": 0.3,
                "latency_budget_ms": 150,
                "probe_interval_ms": 100,
                "jitter_weight": 0.3,
                "loss_weight": 0.5,
                "rtt_weight": 0.2,
            },
            "qos": {"enabled": False},
        }
        client = TunnelClient(config)
        assert client._server_addr == "127.0.0.1"
        assert client._server_port == 9999

    def test_add_path(self):
        config = {
            "vps": {"host": "127.0.0.1", "tunnel_port": 9999},
            "tunnel": {"mtu": 1400, "sequence_window": 1024},
            "fec": {"mode": "none"},
            "scheduler": {
                "mode": "round_robin",
                "history_window": 50,
                "ewma_alpha": 0.3,
                "latency_budget_ms": 150,
                "probe_interval_ms": 100,
                "jitter_weight": 0.3,
                "loss_weight": 0.5,
                "rtt_weight": 0.2,
            },
            "qos": {"enabled": False},
        }
        client = TunnelClient(config)
        client.add_path(0, "lo")
        assert 0 in client._sockets


class TestTunnelServerInit:
    def test_server_creates_with_config(self):
        config = {
            "vps": {"tunnel_port": 9999},
            "server": {"host": "0.0.0.0"},
            "tunnel": {"mtu": 1400, "sequence_window": 1024},
            "fec": {"mode": "none"},
            "scheduler": {
                "mode": "round_robin",
                "history_window": 50,
                "ewma_alpha": 0.3,
                "latency_budget_ms": 150,
                "probe_interval_ms": 100,
                "jitter_weight": 0.3,
                "loss_weight": 0.5,
                "rtt_weight": 0.2,
            },
        }
        server = TunnelServer(config)
        assert server._bind_port == 9999


class TestTrafficClassification:
    def test_udp_realtime_ports(self):
        config = {
            "vps": {"host": "127.0.0.1", "tunnel_port": 9999},
            "tunnel": {"mtu": 1400, "sequence_window": 1024},
            "fec": {"mode": "none"},
            "scheduler": {
                "mode": "round_robin",
                "history_window": 50,
                "ewma_alpha": 0.3,
                "latency_budget_ms": 150,
                "probe_interval_ms": 100,
                "jitter_weight": 0.3,
                "loss_weight": 0.5,
                "rtt_weight": 0.2,
            },
            "qos": {
                "enabled": True,
                "tiers": {
                    "realtime": {"ports": "3478-3481", "fec_mode": "replicate"},
                    "bulk": {"ports": "", "fec_mode": "xor"},
                },
            },
        }
        client = TunnelClient(config)

        # Build a fake UDP packet to port 3479 (realtime)
        import struct
        ip_header = bytearray(20)
        ip_header[0] = 0x45  # IPv4, IHL=5
        ip_header[9] = 17    # UDP protocol
        udp_header = struct.pack("!HH", 12345, 3479)  # src_port, dst_port
        fake_packet = bytes(ip_header) + udp_header + b"\x00" * 4

        tier = client._classify_traffic(fake_packet)
        assert tier == "realtime"

    def test_unmatched_port_is_bulk(self):
        config = {
            "vps": {"host": "127.0.0.1", "tunnel_port": 9999},
            "tunnel": {"mtu": 1400, "sequence_window": 1024},
            "fec": {"mode": "none"},
            "scheduler": {
                "mode": "round_robin",
                "history_window": 50,
                "ewma_alpha": 0.3,
                "latency_budget_ms": 150,
                "probe_interval_ms": 100,
                "jitter_weight": 0.3,
                "loss_weight": 0.5,
                "rtt_weight": 0.2,
            },
            "qos": {
                "enabled": True,
                "tiers": {"realtime": {"ports": "3478-3481"}},
            },
        }
        client = TunnelClient(config)
        import struct
        ip_header = bytearray(20)
        ip_header[0] = 0x45
        ip_header[9] = 17
        udp_header = struct.pack("!HH", 12345, 8080)
        fake_packet = bytes(ip_header) + udp_header + b"\x00" * 4
        tier = client._classify_traffic(fake_packet)
        assert tier == "bulk"


class TestLoopbackIntegration:
    """Integration test using loopback UDP (no TUN required)."""

    @pytest.mark.asyncio
    async def test_keepalive_exchange(self):
        """Test keepalive packet exchange over loopback."""
        import socket

        # Create server socket
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server_sock.setblocking(False)
        server_sock.bind(("127.0.0.1", 0))
        server_port = server_sock.getsockname()[1]

        # Create client socket
        client_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client_sock.setblocking(False)

        try:
            # Client sends keepalive
            ka = HyperAggPacket.create_keepalive(path_id=0, seq=1)
            wire = ka.serialize()
            client_sock.sendto(wire, ("127.0.0.1", server_port))

            # Server receives
            await asyncio.sleep(0.01)
            data, addr = server_sock.recvfrom(2048)
            pkt = HyperAggPacket.deserialize(data)
            assert pkt.is_keepalive
            assert pkt.path_id == 0

            # Server echoes back
            response = HyperAggPacket.create_keepalive(0, 1)
            response.timestamp_us = pkt.timestamp_us
            server_sock.sendto(response.serialize(), addr)

        finally:
            server_sock.close()
            client_sock.close()

    @pytest.mark.asyncio
    async def test_encrypted_data_exchange(self):
        """Test encrypted data packet exchange over loopback."""
        import socket

        key = PacketCrypto.generate_key()
        crypto = PacketCrypto(key)

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server_sock.setblocking(False)
        server_sock.bind(("127.0.0.1", 0))
        server_port = server_sock.getsockname()[1]

        client_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client_sock.setblocking(False)

        try:
            # Build and encrypt a data packet
            # The AAD (header) must have the ENCRYPTED payload_len
            # so both sides see the same header bytes.
            payload = b"Hello from HyperAgg!"
            from hyperagg.tunnel.crypto import TAG_SIZE
            encrypted_len = len(payload) + TAG_SIZE

            pkt = HyperAggPacket.create_data(
                payload=b"\x00" * encrypted_len,  # placeholder for correct payload_len
                path_id=0, seq=0, global_seq=42,
            )
            # Now header_bytes() has payload_len = encrypted_len
            header_aad = pkt.header_bytes()
            encrypted = crypto.encrypt(payload, header_aad, pkt.global_seq, pkt.path_id)
            pkt.payload = encrypted

            # Send
            client_sock.sendto(pkt.serialize(), ("127.0.0.1", server_port))

            # Receive and decrypt
            await asyncio.sleep(0.01)
            data, addr = server_sock.recvfrom(2048)
            received = HyperAggPacket.deserialize(data)
            decrypted = crypto.decrypt(
                received.payload, received.header_bytes(),
                received.global_seq, received.path_id,
            )
            assert decrypted == payload

        finally:
            server_sock.close()
            client_sock.close()
