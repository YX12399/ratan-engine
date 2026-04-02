"""
XOR Parity Forward Error Correction.

For every N data packets, generates 1 parity packet (XOR of all N).
If any single packet in the group is lost, XOR the remaining N-1 with
the parity to recover it.

Overhead: 1/N (e.g., group_size=4 means 25% overhead).
Recovery: single loss per group only.
"""

import logging
import time
from typing import Optional

logger = logging.getLogger("hyperagg.fec.xor")


def _xor_bytes(a: bytes, b: bytes) -> bytes:
    """XOR two byte strings, zero-padding the shorter one."""
    la, lb = len(a), len(b)
    if la < lb:
        a = a + b"\x00" * (lb - la)
    elif lb < la:
        b = b + b"\x00" * (la - lb)
    # Use int conversion for speed
    ia = int.from_bytes(a, "big")
    ib = int.from_bytes(b, "big")
    result = ia ^ ib
    return result.to_bytes(max(la, lb), "big")


class XORFec:
    """Stateless XOR FEC operations."""

    def __init__(self, group_size: int = 4):
        self.group_size = group_size

    def encode_group(self, packets: list[bytes]) -> bytes:
        """
        XOR all packets together to produce parity.

        Args:
            packets: Exactly group_size data packet payloads.

        Returns:
            Parity bytes (length = max payload length in group).
        """
        if len(packets) != self.group_size:
            raise ValueError(
                f"Expected {self.group_size} packets, got {len(packets)}"
            )
        parity = packets[0]
        for p in packets[1:]:
            parity = _xor_bytes(parity, p)
        return parity

    def decode_recover(
        self,
        received: dict[int, bytes],
        parity: bytes,
        missing_index: int,
    ) -> bytes:
        """
        Recover a missing packet from remaining data + parity.

        Args:
            received: {index: payload} for packets we received.
            parity: The XOR parity packet.
            missing_index: Which packet index is missing.

        Returns:
            Recovered payload.
        """
        # XOR all received packets with parity → missing packet falls out
        result = parity
        for idx, payload in received.items():
            if idx != missing_index:
                result = _xor_bytes(result, payload)
        return result

    def can_recover(self, received_indices: set[int], has_parity: bool) -> bool:
        """Check if recovery is possible (exactly 1 missing + parity available)."""
        expected = set(range(self.group_size))
        missing = expected - received_indices
        return len(missing) == 1 and has_parity


class XORFecEncoder:
    """TX-side XOR FEC encoder — accumulates packets and emits parity."""

    def __init__(self, group_size: int = 4):
        self.group_size = group_size
        self._fec = XORFec(group_size)
        self._current_group: list[bytes] = []
        self._group_id = 0

    def add_packet(self, payload: bytes) -> tuple[int, int, Optional[bytes]]:
        """
        Add a data packet payload.

        Returns:
            (fec_group_id, fec_index, parity_or_none)
            - parity_or_none is None until the group is complete
            - When group completes, returns the parity bytes
        """
        group_id = self._group_id
        fec_index = len(self._current_group)
        self._current_group.append(payload)

        parity = None
        if len(self._current_group) == self.group_size:
            parity = self._fec.encode_group(self._current_group)
            self._current_group = []
            self._group_id += 1

        return group_id, fec_index, parity

    def flush(self) -> Optional[tuple[int, bytes]]:
        """Force emit parity for partial group (e.g., on timeout)."""
        if not self._current_group:
            return None
        # Pad with empty packets to fill group
        while len(self._current_group) < self.group_size:
            self._current_group.append(b"")
        parity = self._fec.encode_group(self._current_group)
        group_id = self._group_id
        self._current_group = []
        self._group_id += 1
        return group_id, parity

    @property
    def pending_count(self) -> int:
        return len(self._current_group)


class XORFecDecoder:
    """RX-side XOR FEC decoder — buffers packets and recovers losses."""

    def __init__(self, group_size: int = 4, timeout_ms: float = 500.0):
        self.group_size = group_size
        self.timeout_ms = timeout_ms
        self._fec = XORFec(group_size)
        # group_id -> {fec_index: payload}
        self._groups: dict[int, dict[int, bytes]] = {}
        # group_id -> parity payload
        self._parities: dict[int, bytes] = {}
        # group_id -> first-seen timestamp
        self._timestamps: dict[int, float] = {}
        # group_id -> set of original payload lengths
        self._payload_lens: dict[int, dict[int, int]] = {}

        self.recoveries = 0

    def receive_packet(
        self,
        fec_group_id: int,
        fec_index: int,
        is_parity: bool,
        payload: bytes,
        original_len: int = 0,
    ) -> Optional[tuple[int, bytes]]:
        """
        Process a received packet (data or parity).

        Returns:
            (fec_index, recovered_payload) if a lost packet was recovered, else None.
        """
        now = time.monotonic()

        if fec_group_id not in self._timestamps:
            self._timestamps[fec_group_id] = now

        if is_parity:
            self._parities[fec_group_id] = payload
        else:
            if fec_group_id not in self._groups:
                self._groups[fec_group_id] = {}
                self._payload_lens[fec_group_id] = {}
            self._groups[fec_group_id][fec_index] = payload
            self._payload_lens[fec_group_id][fec_index] = original_len or len(payload)

        # Try recovery
        return self._try_recover(fec_group_id)

    def _try_recover(self, group_id: int) -> Optional[tuple[int, bytes]]:
        """Attempt recovery if exactly one data packet is missing and parity exists."""
        received = self._groups.get(group_id, {})
        parity = self._parities.get(group_id)

        received_indices = set(received.keys())

        if not self._fec.can_recover(received_indices, parity is not None):
            return None

        expected = set(range(self.group_size))
        missing = expected - received_indices
        missing_index = missing.pop()

        recovered = self._fec.decode_recover(received, parity, missing_index)
        self.recoveries += 1

        # Clean up completed group
        self._cleanup_group(group_id)

        return missing_index, recovered

    def expire_groups(self) -> list[int]:
        """Purge stale incomplete groups. Returns list of expired group IDs."""
        now = time.monotonic()
        expired = []
        cutoff = now - (self.timeout_ms / 1000.0)

        for group_id, ts in list(self._timestamps.items()):
            if ts < cutoff:
                expired.append(group_id)
                self._cleanup_group(group_id)

        return expired

    def _cleanup_group(self, group_id: int) -> None:
        self._groups.pop(group_id, None)
        self._parities.pop(group_id, None)
        self._timestamps.pop(group_id, None)
        self._payload_lens.pop(group_id, None)


if __name__ == "__main__":
    print("XOR FEC Test:")

    # Create 4 test payloads of varying length
    payloads = [
        b"Hello from packet 0!",
        b"Packet 1 is a bit longer than the others...",
        b"Pkt2",
        b"And packet 3 has medium length content",
    ]

    encoder = XORFecEncoder(group_size=4)
    decoder = XORFecDecoder(group_size=4)

    parity = None
    for i, p in enumerate(payloads):
        gid, fidx, par = encoder.add_packet(p)
        assert gid == 0
        assert fidx == i
        if par is not None:
            parity = par
            print(f"  Parity generated: {len(parity)} bytes")

    assert parity is not None, "Parity should have been generated"

    # Simulate loss of packet #2
    lost_idx = 2
    for i, p in enumerate(payloads):
        if i == lost_idx:
            continue  # "lost"
        decoder.receive_packet(0, i, False, p)

    # Feed parity
    result = decoder.receive_packet(0, 4, True, parity)
    assert result is not None, "Should have recovered"
    recovered_idx, recovered_data = result
    assert recovered_idx == lost_idx
    # Recovered data may be zero-padded; trim to original length
    assert recovered_data[:len(payloads[lost_idx])] == payloads[lost_idx]

    print(f"  Lost packet #{lost_idx} recovered: "
          f"'{recovered_data[:len(payloads[lost_idx])].decode()}'")
    print(f"  Total recoveries: {decoder.recoveries}")
    print("XOR FEC Test: PASSED")
