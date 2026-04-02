"""Tests for Reed-Solomon FEC."""
import os
import time
import random
import pytest
from hyperagg.fec.rs_fec import ReedSolomonFec, RSFecEncoder, RSFecDecoder
from reedsolo import ReedSolomonError


class TestReedSolomonFec:
    def test_encode_produces_correct_parity_count(self):
        fec = ReedSolomonFec(data_shards=8, parity_shards=2)
        payloads = [os.urandom(1400) for _ in range(8)]
        parity = fec.encode(payloads)
        assert len(parity) == 2
        assert len(parity[0]) == 1400

    def test_recover_single_loss(self):
        fec = ReedSolomonFec(data_shards=4, parity_shards=2)
        payloads = [os.urandom(100) for _ in range(4)]
        parity = fec.encode(payloads)

        # Drop shard 1
        shards = {0: payloads[0], 2: payloads[2], 3: payloads[3],
                  4: parity[0], 5: parity[1]}
        recovered = fec.decode(shards, num_data=4)
        for i in range(4):
            assert recovered[i] == payloads[i]

    def test_recover_double_loss(self):
        fec = ReedSolomonFec(data_shards=4, parity_shards=2)
        payloads = [os.urandom(100) for _ in range(4)]
        parity = fec.encode(payloads)

        # Drop shards 0 and 3
        shards = {1: payloads[1], 2: payloads[2],
                  4: parity[0], 5: parity[1]}
        recovered = fec.decode(shards, num_data=4)
        for i in range(4):
            assert recovered[i] == payloads[i]

    def test_triple_loss_fails(self):
        fec = ReedSolomonFec(data_shards=4, parity_shards=2)
        payloads = [os.urandom(100) for _ in range(4)]
        parity = fec.encode(payloads)

        # Drop 3 shards — too many
        shards = {2: payloads[2], 4: parity[0], 5: parity[1]}
        with pytest.raises(ReedSolomonError):
            fec.decode(shards, num_data=4)

    def test_can_recover_check(self):
        fec = ReedSolomonFec(data_shards=8, parity_shards=2)
        assert fec.can_recover(8)
        assert fec.can_recover(9)
        assert not fec.can_recover(7)

    def test_variable_length_payloads(self):
        fec = ReedSolomonFec(data_shards=4, parity_shards=2)
        payloads = [os.urandom(random.randint(50, 200)) for _ in range(4)]
        parity = fec.encode(payloads)

        # Drop shard 2
        shards = {0: payloads[0], 1: payloads[1], 3: payloads[3],
                  4: parity[0], 5: parity[1]}
        recovered = fec.decode(shards, num_data=4)
        # Check recovered data matches (may be zero-padded)
        max_len = max(len(p) for p in payloads)
        for i in range(4):
            padded_original = payloads[i] + b"\x00" * (max_len - len(payloads[i]))
            assert recovered[i] == padded_original


class TestRSEncoder:
    def test_parity_emitted_on_group_complete(self):
        enc = RSFecEncoder(data_shards=4, parity_shards=2)
        for i in range(3):
            _, _, par = enc.add_packet(os.urandom(100))
            assert par is None

        _, _, par = enc.add_packet(os.urandom(100))
        assert par is not None
        assert len(par) == 2


class TestRSDecoder:
    def test_recover_missing_shard(self):
        fec = ReedSolomonFec(data_shards=4, parity_shards=2)
        payloads = [os.urandom(100) for _ in range(4)]
        parity = fec.encode(payloads)

        decoder = RSFecDecoder(data_shards=4, parity_shards=2)

        # Feed all except shard 1
        for i in [0, 2, 3]:
            result = decoder.receive_shard(0, i, payloads[i])
            assert result == []

        # Feed parity
        for i, p in enumerate(parity):
            result = decoder.receive_shard(0, 4 + i, p)

        # Last parity should trigger recovery
        assert decoder.recoveries >= 1


class TestPerformance:
    def test_rs_speed_small(self):
        """RS(4,2) with 1400B payloads."""
        fec = ReedSolomonFec(data_shards=4, parity_shards=2)
        payloads = [os.urandom(1400) for _ in range(4)]

        t0 = time.monotonic()
        for _ in range(100):
            parity = fec.encode(payloads)
            shards = {0: payloads[0], 2: payloads[2], 3: payloads[3],
                      4: parity[0], 5: parity[1]}
            fec.decode(shards, num_data=4)
        elapsed = time.monotonic() - t0
        assert elapsed < 30, f"RS(4,2) 100 cycles took {elapsed:.1f}s"
