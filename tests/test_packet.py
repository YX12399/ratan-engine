"""Tests for HyperAgg packet format."""
import struct
import pytest
from hyperagg.tunnel.packet import (
    HEADER_SIZE, HEADER_FORMAT, MAGIC, VERSION,
    FLAG_FEC_PARITY, FLAG_REPLICATED, FLAG_KEEPALIVE, FLAG_CONTROL,
    HyperAggPacket, set_connection_start,
)


@pytest.fixture(autouse=True)
def init_time():
    set_connection_start()


class TestHeaderFormat:
    def test_header_size_is_28(self):
        assert struct.calcsize(HEADER_FORMAT) == 28
        assert HEADER_SIZE == 28

    def test_magic_value(self):
        assert MAGIC == 0x48414747  # "HAGG"


class TestDataPacket:
    def test_roundtrip(self):
        payload = b"Hello, HyperAgg!" * 50
        pkt = HyperAggPacket.create_data(
            payload=payload, path_id=1, seq=100, global_seq=42,
            fec_group_id=5, fec_index=2, fec_group_size=5,
        )
        wire = pkt.serialize()
        restored = HyperAggPacket.deserialize(wire)

        assert restored.magic == MAGIC
        assert restored.version == VERSION
        assert restored.flags == 0
        assert restored.path_id == 1
        assert restored.seq == 100
        assert restored.global_seq == 42
        assert restored.fec_group_id == 5
        assert restored.fec_index == 2
        assert restored.fec_group_size == 5
        assert restored.payload_len == len(payload)
        assert restored.payload == payload

    def test_wire_size(self):
        payload = b"X" * 1400
        pkt = HyperAggPacket.create_data(payload=payload, path_id=0, seq=0, global_seq=0)
        wire = pkt.serialize()
        assert len(wire) == HEADER_SIZE + 1400

    def test_empty_payload(self):
        pkt = HyperAggPacket.create_data(payload=b"", path_id=0, seq=0, global_seq=0)
        wire = pkt.serialize()
        assert len(wire) == HEADER_SIZE
        restored = HyperAggPacket.deserialize(wire)
        assert restored.payload == b""

    def test_max_path_id(self):
        pkt = HyperAggPacket.create_data(
            payload=b"test", path_id=0xFFFF, seq=0, global_seq=0,
        )
        wire = pkt.serialize()
        restored = HyperAggPacket.deserialize(wire)
        assert restored.path_id == 0xFFFF

    def test_max_sequence_numbers(self):
        pkt = HyperAggPacket.create_data(
            payload=b"test", path_id=0,
            seq=0xFFFFFFFF, global_seq=0xFFFFFFFF,
        )
        wire = pkt.serialize()
        restored = HyperAggPacket.deserialize(wire)
        assert restored.seq == 0xFFFFFFFF
        assert restored.global_seq == 0xFFFFFFFF


class TestKeepalivePacket:
    def test_roundtrip(self):
        pkt = HyperAggPacket.create_keepalive(path_id=1, seq=50)
        wire = pkt.serialize()
        restored = HyperAggPacket.deserialize(wire)

        assert restored.is_keepalive
        assert not restored.is_fec_parity
        assert not restored.is_control
        assert restored.path_id == 1
        assert restored.seq == 50
        assert restored.payload == b""
        assert len(wire) == HEADER_SIZE


class TestFecParityPacket:
    def test_roundtrip(self):
        parity = b"\xAA\xBB\xCC" * 100
        pkt = HyperAggPacket.create_fec_parity(
            parity_data=parity, path_id=0, seq=10, global_seq=99,
            fec_group_id=3, fec_index=4, fec_group_size=5,
        )
        wire = pkt.serialize()
        restored = HyperAggPacket.deserialize(wire)

        assert restored.is_fec_parity
        assert not restored.is_keepalive
        assert restored.fec_group_id == 3
        assert restored.fec_index == 4
        assert restored.fec_group_size == 5
        assert restored.payload == parity


class TestControlPacket:
    def test_roundtrip(self):
        ctrl_data = b'{"type": "handshake"}'
        pkt = HyperAggPacket.create_control(ctrl_data, path_id=0, seq=1)
        wire = pkt.serialize()
        restored = HyperAggPacket.deserialize(wire)

        assert restored.is_control
        assert restored.payload == ctrl_data


class TestDeserializeErrors:
    def test_too_short(self):
        with pytest.raises(ValueError, match="too short"):
            HyperAggPacket.deserialize(b"\x00" * 10)

    def test_bad_magic(self):
        bad = struct.pack(HEADER_FORMAT, 0xDEADBEEF, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        with pytest.raises(ValueError, match="Invalid magic"):
            HyperAggPacket.deserialize(bad)

    def test_truncated_payload(self):
        pkt = HyperAggPacket.create_data(b"Hello", path_id=0, seq=0, global_seq=0)
        wire = pkt.serialize()
        # Truncate 2 bytes from payload
        with pytest.raises(ValueError, match="truncated"):
            HyperAggPacket.deserialize(wire[:-2])


class TestFlags:
    def test_flag_properties(self):
        pkt = HyperAggPacket(
            magic=MAGIC, version=VERSION,
            flags=FLAG_FEC_PARITY | FLAG_REPLICATED,
            path_id=0, seq=0, global_seq=0,
            fec_group_id=0, fec_index=0, fec_group_size=0,
            payload_len=0, timestamp_us=0, payload=b"",
        )
        assert pkt.is_fec_parity
        assert pkt.is_replicated
        assert not pkt.is_keepalive
        assert not pkt.is_control


class TestPerformance:
    def test_pack_unpack_speed(self):
        import time
        payload = b"X" * 1400
        pkt = HyperAggPacket.create_data(payload=payload, path_id=0, seq=0, global_seq=0)

        t0 = time.monotonic()
        for i in range(10000):
            wire = pkt.serialize()
            HyperAggPacket.deserialize(wire)
        elapsed = time.monotonic() - t0

        ops_per_sec = 10000 / elapsed
        assert ops_per_sec > 50000, f"Too slow: {ops_per_sec:.0f} ops/sec"
