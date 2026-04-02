"""Tests for XOR parity FEC."""
import os
import time
import pytest
from hyperagg.fec.xor_fec import XORFec, XORFecEncoder, XORFecDecoder


class TestXORFecBasic:
    def test_encode_group(self):
        fec = XORFec(group_size=4)
        payloads = [os.urandom(100) for _ in range(4)]
        parity = fec.encode_group(payloads)
        assert len(parity) == 100

    def test_recover_each_position(self):
        """Recover each possible single loss position."""
        fec = XORFec(group_size=4)
        payloads = [os.urandom(1400) for _ in range(4)]
        parity = fec.encode_group(payloads)

        for drop in range(4):
            received = {i: payloads[i] for i in range(4) if i != drop}
            recovered = fec.decode_recover(received, parity, drop)
            assert recovered == payloads[drop], f"Failed to recover index {drop}"

    def test_variable_length_packets(self):
        """Test with different payload lengths in same group."""
        fec = XORFec(group_size=4)
        payloads = [
            b"short",
            b"a medium length payload here",
            os.urandom(1400),
            b"tiny",
        ]
        parity = fec.encode_group(payloads)

        for drop in range(4):
            received = {i: payloads[i] for i in range(4) if i != drop}
            recovered = fec.decode_recover(received, parity, drop)
            expected = payloads[drop]
            # Recovered may be zero-padded to max length
            assert recovered[:len(expected)] == expected

    def test_can_recover_single_loss(self):
        fec = XORFec(group_size=4)
        assert fec.can_recover({0, 1, 2}, has_parity=True)
        assert fec.can_recover({0, 1, 3}, has_parity=True)

    def test_cannot_recover_double_loss(self):
        fec = XORFec(group_size=4)
        assert not fec.can_recover({0, 1}, has_parity=True)

    def test_cannot_recover_without_parity(self):
        fec = XORFec(group_size=4)
        assert not fec.can_recover({0, 1, 2}, has_parity=False)

    def test_wrong_group_size(self):
        fec = XORFec(group_size=4)
        with pytest.raises(ValueError):
            fec.encode_group([b"a", b"b", b"c"])


class TestXORFecEncoder:
    def test_parity_emitted_on_group_complete(self):
        enc = XORFecEncoder(group_size=4)
        for i in range(3):
            gid, idx, par = enc.add_packet(os.urandom(100))
            assert gid == 0
            assert idx == i
            assert par is None

        gid, idx, par = enc.add_packet(os.urandom(100))
        assert gid == 0
        assert idx == 3
        assert par is not None

    def test_group_id_increments(self):
        enc = XORFecEncoder(group_size=2)
        enc.add_packet(b"a")
        _, _, par = enc.add_packet(b"b")
        assert par is not None

        gid, _, _ = enc.add_packet(b"c")
        assert gid == 1

    def test_flush_partial(self):
        enc = XORFecEncoder(group_size=4)
        enc.add_packet(b"a")
        enc.add_packet(b"b")
        result = enc.flush()
        assert result is not None
        assert enc.pending_count == 0


class TestXORFecDecoder:
    def test_recover_on_parity_arrival(self):
        fec = XORFec(group_size=4)
        payloads = [os.urandom(100) for _ in range(4)]
        parity = fec.encode_group(payloads)

        decoder = XORFecDecoder(group_size=4)
        # Feed all but #2
        for i in [0, 1, 3]:
            result = decoder.receive_packet(0, i, False, payloads[i])
            assert result is None

        # Feed parity → should recover #2
        result = decoder.receive_packet(0, 4, True, parity)
        assert result is not None
        recovered_idx, recovered_data = result
        assert recovered_idx == 2
        assert recovered_data[:100] == payloads[2]
        assert decoder.recoveries == 1


class TestPerformance:
    def test_xor_speed(self):
        fec = XORFec(group_size=4)
        payloads = [os.urandom(1400) for _ in range(4)]

        t0 = time.monotonic()
        for _ in range(10000):
            parity = fec.encode_group(payloads)
            received = {0: payloads[0], 1: payloads[1], 3: payloads[3]}
            fec.decode_recover(received, parity, 2)
        elapsed = time.monotonic() - t0
        groups_per_sec = 10000 / elapsed
        assert groups_per_sec > 1000, f"Too slow: {groups_per_sec:.0f} groups/sec"
