"""
HyperAgg Packet Format — 28-byte wire header for UDP bonding tunnel.

Every IP packet captured from the TUN device is encapsulated in a HyperAgg
packet before being sent over the UDP tunnel. The header carries sequencing,
FEC metadata, and timing information needed for reassembly.

Header layout (28 bytes):
  Bytes 0-3:   Magic (0x48414747 = "HAGG")
  Byte  4:     Version (1)
  Byte  5:     Flags
  Bytes 6-7:   Path ID (uint16)
  Bytes 8-11:  Per-path sequence number (uint32)
  Bytes 12-15: Global sequence number (uint32)
  Bytes 16-19: FEC group ID (uint32)
  Byte  20:    FEC index within group
  Byte  21:    FEC group size (data + parity)
  Bytes 22-23: Payload length (uint16)
  Bytes 24-27: Timestamp (uint32, microseconds since connection start)
"""

import struct
import time
from dataclasses import dataclass

# Constants
MAGIC = 0x48414747  # "HAGG"
VERSION = 1
HEADER_SIZE = 28
HEADER_FORMAT = "!IBBHIIIBBHI"

# Flags
FLAG_FEC_PARITY = 0x01
FLAG_REPLICATED = 0x02
FLAG_KEEPALIVE = 0x04
FLAG_CONTROL = 0x08

# Connection start time (set when tunnel initializes)
_connection_start_ns: int = 0


def set_connection_start() -> None:
    """Set the connection start time for timestamp calculation."""
    global _connection_start_ns
    _connection_start_ns = time.monotonic_ns()


def get_timestamp_us() -> int:
    """Get microseconds since connection start, wrapped to uint32."""
    if _connection_start_ns == 0:
        set_connection_start()
    elapsed_us = (time.monotonic_ns() - _connection_start_ns) // 1000
    return elapsed_us & 0xFFFFFFFF


@dataclass(slots=True)
class HyperAggPacket:
    """Complete HyperAgg tunnel packet = 28-byte header + payload."""

    magic: int
    version: int
    flags: int
    path_id: int
    seq: int
    global_seq: int
    fec_group_id: int
    fec_index: int
    fec_group_size: int
    payload_len: int
    timestamp_us: int
    payload: bytes

    @property
    def is_fec_parity(self) -> bool:
        return bool(self.flags & FLAG_FEC_PARITY)

    @property
    def is_replicated(self) -> bool:
        return bool(self.flags & FLAG_REPLICATED)

    @property
    def is_keepalive(self) -> bool:
        return bool(self.flags & FLAG_KEEPALIVE)

    @property
    def is_control(self) -> bool:
        return bool(self.flags & FLAG_CONTROL)

    def serialize(self) -> bytes:
        """Pack header + payload into wire format."""
        header = struct.pack(
            HEADER_FORMAT,
            self.magic,
            self.version,
            self.flags,
            self.path_id,
            self.seq,
            self.global_seq,
            self.fec_group_id,
            self.fec_index,
            self.fec_group_size,
            self.payload_len,
            self.timestamp_us,
        )
        return header + self.payload

    def header_bytes(self) -> bytes:
        """Return just the 28-byte header (used as AAD for encryption)."""
        return struct.pack(
            HEADER_FORMAT,
            self.magic,
            self.version,
            self.flags,
            self.path_id,
            self.seq,
            self.global_seq,
            self.fec_group_id,
            self.fec_index,
            self.fec_group_size,
            self.payload_len,
            self.timestamp_us,
        )

    @staticmethod
    def deserialize(data: bytes) -> "HyperAggPacket":
        """Parse a HyperAggPacket from wire format."""
        if len(data) < HEADER_SIZE:
            raise ValueError(f"Packet too short: {len(data)} < {HEADER_SIZE}")

        fields = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
        magic, version, flags, path_id, seq, global_seq, fec_group_id, \
            fec_index, fec_group_size, payload_len, timestamp_us = fields

        if magic != MAGIC:
            raise ValueError(f"Invalid magic: 0x{magic:08X}, expected 0x{MAGIC:08X}")

        payload = data[HEADER_SIZE:HEADER_SIZE + payload_len]
        if len(payload) != payload_len:
            raise ValueError(
                f"Payload truncated: got {len(payload)}, expected {payload_len}"
            )

        return HyperAggPacket(
            magic=magic,
            version=version,
            flags=flags,
            path_id=path_id,
            seq=seq,
            global_seq=global_seq,
            fec_group_id=fec_group_id,
            fec_index=fec_index,
            fec_group_size=fec_group_size,
            payload_len=payload_len,
            timestamp_us=timestamp_us,
            payload=payload,
        )

    @staticmethod
    def create_data(
        payload: bytes,
        path_id: int,
        seq: int,
        global_seq: int,
        fec_group_id: int = 0,
        fec_index: int = 0,
        fec_group_size: int = 0,
    ) -> "HyperAggPacket":
        """Create a data packet."""
        return HyperAggPacket(
            magic=MAGIC,
            version=VERSION,
            flags=0,
            path_id=path_id,
            seq=seq,
            global_seq=global_seq,
            fec_group_id=fec_group_id,
            fec_index=fec_index,
            fec_group_size=fec_group_size,
            payload_len=len(payload),
            timestamp_us=get_timestamp_us(),
            payload=payload,
        )

    @staticmethod
    def create_keepalive(path_id: int, seq: int) -> "HyperAggPacket":
        """Create a keepalive packet (no payload)."""
        return HyperAggPacket(
            magic=MAGIC,
            version=VERSION,
            flags=FLAG_KEEPALIVE,
            path_id=path_id,
            seq=seq,
            global_seq=0,
            fec_group_id=0,
            fec_index=0,
            fec_group_size=0,
            payload_len=0,
            timestamp_us=get_timestamp_us(),
            payload=b"",
        )

    @staticmethod
    def create_fec_parity(
        parity_data: bytes,
        path_id: int,
        seq: int,
        global_seq: int,
        fec_group_id: int,
        fec_index: int,
        fec_group_size: int,
    ) -> "HyperAggPacket":
        """Create an FEC parity packet."""
        return HyperAggPacket(
            magic=MAGIC,
            version=VERSION,
            flags=FLAG_FEC_PARITY,
            path_id=path_id,
            seq=seq,
            global_seq=global_seq,
            fec_group_id=fec_group_id,
            fec_index=fec_index,
            fec_group_size=fec_group_size,
            payload_len=len(parity_data),
            timestamp_us=get_timestamp_us(),
            payload=parity_data,
        )

    @staticmethod
    def create_control(
        control_data: bytes,
        path_id: int,
        seq: int,
    ) -> "HyperAggPacket":
        """Create a control packet (handshake, config sync)."""
        return HyperAggPacket(
            magic=MAGIC,
            version=VERSION,
            flags=FLAG_CONTROL,
            path_id=path_id,
            seq=seq,
            global_seq=0,
            fec_group_id=0,
            fec_index=0,
            fec_group_size=0,
            payload_len=len(control_data),
            timestamp_us=get_timestamp_us(),
            payload=control_data,
        )


if __name__ == "__main__":
    set_connection_start()
    test_payload = b"Hello HyperAgg!" * 10
    pkt = HyperAggPacket.create_data(
        payload=test_payload, path_id=0, seq=1, global_seq=42,
        fec_group_id=1, fec_index=0, fec_group_size=5,
    )
    wire = pkt.serialize()
    restored = HyperAggPacket.deserialize(wire)
    assert restored.magic == MAGIC
    assert restored.version == VERSION
    assert restored.path_id == 0
    assert restored.seq == 1
    assert restored.global_seq == 42
    assert restored.fec_group_id == 1
    assert restored.fec_index == 0
    assert restored.fec_group_size == 5
    assert restored.payload == test_payload
    assert len(wire) == HEADER_SIZE + len(test_payload)

    ka = HyperAggPacket.create_keepalive(path_id=1, seq=99)
    ka_wire = ka.serialize()
    ka_back = HyperAggPacket.deserialize(ka_wire)
    assert ka_back.is_keepalive
    assert ka_back.payload == b""

    print(f"Packet format test: PASSED (header={HEADER_SIZE}B, "
          f"data wire={len(wire)}B, keepalive wire={len(ka_wire)}B)")
