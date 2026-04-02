"""
Reed-Solomon Forward Error Correction.

RS(N, K) encodes K data shards and produces N-K parity shards.
Any K shards out of N total are sufficient to recover all data.

Uses the reedsolo library for GF(2^8) arithmetic.

Default: K=8 data, 2 parity → tolerates up to 2 simultaneous losses.
Overhead: parity_shards / data_shards (e.g., 2/8 = 25%).
"""

import logging
import time
from typing import Optional

from reedsolo import RSCodec, ReedSolomonError

logger = logging.getLogger("hyperagg.fec.rs")


class ReedSolomonFec:
    """Reed-Solomon erasure coding for packet-level FEC."""

    def __init__(self, data_shards: int = 8, parity_shards: int = 2):
        self.data_shards = data_shards
        self.parity_shards = parity_shards
        self.total_shards = data_shards + parity_shards
        self._codec = RSCodec(parity_shards)

    def encode(self, data_blocks: list[bytes]) -> list[bytes]:
        """
        Encode data blocks and produce parity blocks.

        Args:
            data_blocks: Exactly data_shards payload blocks.

        Returns:
            List of parity_shards parity blocks.
        """
        if len(data_blocks) != self.data_shards:
            raise ValueError(
                f"Expected {self.data_shards} data blocks, got {len(data_blocks)}"
            )

        # Pad all blocks to the same length
        max_len = max(len(b) for b in data_blocks)
        padded = [b + b"\x00" * (max_len - len(b)) for b in data_blocks]

        # Interleave: build columns and RS-encode each column
        # For each byte position, take that byte from each block
        parity_blocks = [bytearray(max_len) for _ in range(self.parity_shards)]

        for col in range(max_len):
            # Build column of data bytes
            column = bytearray(self.data_shards)
            for row in range(self.data_shards):
                column[row] = padded[row][col]

            # RS encode the column
            encoded = self._codec.encode(column)
            # encoded = data bytes + parity bytes
            parity_bytes = encoded[self.data_shards:]

            for p in range(self.parity_shards):
                parity_blocks[p][col] = parity_bytes[p]

        return [bytes(pb) for pb in parity_blocks]

    def decode(
        self,
        shards: dict[int, bytes],
        num_data: int,
    ) -> list[bytes]:
        """
        Recover missing data blocks using available data + parity shards.

        Args:
            shards: {shard_index: payload} for received shards.
                    Indices 0..data_shards-1 are data, data_shards..total-1 are parity.
            num_data: Number of data shards expected.

        Returns:
            List of all data_shards recovered data blocks.

        Raises:
            ReedSolomonError: If not enough shards for recovery.
        """
        if len(shards) < self.data_shards:
            raise ReedSolomonError(
                f"Need at least {self.data_shards} shards, got {len(shards)}"
            )

        # Determine max block length
        max_len = max(len(v) for v in shards.values())

        # Pad all to same length
        padded_shards = {}
        for idx, block in shards.items():
            padded_shards[idx] = block + b"\x00" * (max_len - len(block))

        # Decode column by column
        recovered_data = [bytearray(max_len) for _ in range(self.data_shards)]

        # Build erasure positions (indices of missing shards within 0..total-1)
        all_indices = set(range(self.total_shards))
        present_indices = set(shards.keys())
        erasure_positions = sorted(all_indices - present_indices)

        for col in range(max_len):
            # Build the full codeword with placeholders for missing
            codeword = bytearray(self.total_shards)
            for idx in range(self.total_shards):
                if idx in padded_shards:
                    codeword[idx] = padded_shards[idx][col]
                else:
                    codeword[idx] = 0  # placeholder

            # Decode with known erasure positions
            try:
                decoded = self._codec.decode(codeword, erase_pos=erasure_positions)
                # decoded returns (data, remaining, errata_pos) — we want data
                data_col = decoded[0]
                for row in range(self.data_shards):
                    recovered_data[row][col] = data_col[row]
            except ReedSolomonError:
                raise

        return [bytes(rd) for rd in recovered_data]

    def can_recover(self, received_count: int) -> bool:
        """Check if we have enough shards to recover."""
        return received_count >= self.data_shards


class RSFecEncoder:
    """TX-side RS encoder — accumulates data packets and emits parity."""

    def __init__(self, data_shards: int = 8, parity_shards: int = 2):
        self.data_shards = data_shards
        self.parity_shards = parity_shards
        self._fec = ReedSolomonFec(data_shards, parity_shards)
        self._current_group: list[bytes] = []
        self._group_id = 0

    def add_packet(self, payload: bytes) -> tuple[int, int, Optional[list[bytes]]]:
        """
        Add a data packet payload.

        Returns:
            (fec_group_id, fec_index, parity_list_or_none)
        """
        group_id = self._group_id
        fec_index = len(self._current_group)
        self._current_group.append(payload)

        parity = None
        if len(self._current_group) == self.data_shards:
            parity = self._fec.encode(self._current_group)
            self._current_group = []
            self._group_id += 1

        return group_id, fec_index, parity

    def flush(self) -> Optional[tuple[int, list[bytes]]]:
        """Force emit parity for partial group."""
        if not self._current_group:
            return None
        while len(self._current_group) < self.data_shards:
            self._current_group.append(b"")
        parity = self._fec.encode(self._current_group)
        group_id = self._group_id
        self._current_group = []
        self._group_id += 1
        return group_id, parity

    @property
    def pending_count(self) -> int:
        return len(self._current_group)


class RSFecDecoder:
    """RX-side RS decoder — buffers shards and recovers losses."""

    def __init__(self, data_shards: int = 8, parity_shards: int = 2,
                 timeout_ms: float = 500.0):
        self.data_shards = data_shards
        self.parity_shards = parity_shards
        self.total_shards = data_shards + parity_shards
        self.timeout_ms = timeout_ms
        self._fec = ReedSolomonFec(data_shards, parity_shards)
        # group_id -> {shard_index: payload}
        self._groups: dict[int, dict[int, bytes]] = {}
        self._timestamps: dict[int, float] = {}
        self._delivered: set[int] = set()  # group_ids already delivered
        self.recoveries = 0

    def receive_shard(
        self,
        fec_group_id: int,
        shard_index: int,
        payload: bytes,
    ) -> list[tuple[int, bytes]]:
        """
        Process a received shard (data or parity).

        Returns:
            List of (data_shard_index, recovered_payload) for any recovered shards.
            Empty list if no recovery yet.
        """
        if fec_group_id in self._delivered:
            return []

        now = time.monotonic()
        if fec_group_id not in self._timestamps:
            self._timestamps[fec_group_id] = now

        if fec_group_id not in self._groups:
            self._groups[fec_group_id] = {}
        self._groups[fec_group_id][shard_index] = payload

        return self._try_recover(fec_group_id)

    def _try_recover(self, group_id: int) -> list[tuple[int, bytes]]:
        """Attempt RS recovery if we have enough shards."""
        shards = self._groups.get(group_id, {})
        if not self._fec.can_recover(len(shards)):
            return []

        # Check if any data shards are missing
        data_present = {i for i in shards if i < self.data_shards}
        if len(data_present) == self.data_shards:
            # All data present, no recovery needed — just clean up
            self._delivered.add(group_id)
            self._cleanup_group(group_id)
            return []

        try:
            recovered = self._fec.decode(shards, self.data_shards)
        except Exception as e:
            logger.warning(f"RS decode failed for group {group_id}: {e}")
            return []

        # Return only the shards that were missing
        missing_indices = set(range(self.data_shards)) - data_present
        result = []
        for idx in sorted(missing_indices):
            result.append((idx, recovered[idx]))
            self.recoveries += 1

        self._delivered.add(group_id)
        self._cleanup_group(group_id)
        return result

    def expire_groups(self) -> list[int]:
        """Purge stale incomplete groups."""
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
        self._timestamps.pop(group_id, None)
        # Keep _delivered set bounded
        if len(self._delivered) > 10000:
            # Remove oldest half
            to_remove = sorted(self._delivered)[:5000]
            self._delivered -= set(to_remove)


if __name__ == "__main__":
    import os
    print("Reed-Solomon FEC Test:")

    # Create 8 test payloads
    payloads = [os.urandom(1400) for _ in range(8)]

    fec = ReedSolomonFec(data_shards=8, parity_shards=2)

    # Encode
    t0 = time.monotonic()
    parity_blocks = fec.encode(payloads)
    encode_ms = (time.monotonic() - t0) * 1000
    print(f"  Encode: {encode_ms:.2f}ms for 8 data + 2 parity (1400B each)")

    assert len(parity_blocks) == 2
    assert len(parity_blocks[0]) == 1400

    # Drop 2 random data shards
    import random
    drop_indices = random.sample(range(8), 2)
    print(f"  Dropping data shards: {drop_indices}")

    shards = {}
    for i in range(8):
        if i not in drop_indices:
            shards[i] = payloads[i]
    # Add both parity shards
    for i, pb in enumerate(parity_blocks):
        shards[8 + i] = pb

    # Decode
    t0 = time.monotonic()
    recovered = fec.decode(shards, num_data=8)
    decode_ms = (time.monotonic() - t0) * 1000
    print(f"  Decode: {decode_ms:.2f}ms")

    # Verify
    for i in range(8):
        assert recovered[i] == payloads[i], f"Shard {i} mismatch!"

    print(f"  All 8 shards recovered correctly!")
    print("Reed-Solomon FEC Test: PASSED")
