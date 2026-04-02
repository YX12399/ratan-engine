"""Tests for HyperAgg ChaCha20-Poly1305 encryption."""
import os
import time
import pytest
from cryptography.exceptions import InvalidTag
from hyperagg.tunnel.crypto import PacketCrypto, TAG_SIZE


@pytest.fixture
def crypto():
    key = PacketCrypto.generate_key()
    return PacketCrypto(key)


class TestEncryptDecrypt:
    def test_roundtrip(self, crypto):
        header = os.urandom(28)
        plaintext = b"Test payload" * 100
        ct = crypto.encrypt(plaintext, header, global_seq=1, path_id=0)
        pt = crypto.decrypt(ct, header, global_seq=1, path_id=0)
        assert pt == plaintext

    def test_overhead_is_tag_only(self, crypto):
        plaintext = b"X" * 1400
        header = os.urandom(28)
        ct = crypto.encrypt(plaintext, header, global_seq=0, path_id=0)
        assert len(ct) == len(plaintext) + TAG_SIZE

    def test_empty_payload(self, crypto):
        header = os.urandom(28)
        ct = crypto.encrypt(b"", header, global_seq=0, path_id=0)
        pt = crypto.decrypt(ct, header, global_seq=0, path_id=0)
        assert pt == b""


class TestTamperDetection:
    def test_tampered_ciphertext(self, crypto):
        header = os.urandom(28)
        ct = crypto.encrypt(b"secret data", header, global_seq=1, path_id=0)
        tampered = bytearray(ct)
        tampered[0] ^= 0xFF
        with pytest.raises(InvalidTag):
            crypto.decrypt(bytes(tampered), header, global_seq=1, path_id=0)

    def test_tampered_header_aad(self, crypto):
        header = os.urandom(28)
        ct = crypto.encrypt(b"secret data", header, global_seq=1, path_id=0)
        bad_header = bytearray(header)
        bad_header[5] ^= 0xFF
        with pytest.raises(InvalidTag):
            crypto.decrypt(ct, bytes(bad_header), global_seq=1, path_id=0)

    def test_wrong_global_seq(self, crypto):
        header = os.urandom(28)
        ct = crypto.encrypt(b"data", header, global_seq=1, path_id=0)
        with pytest.raises(InvalidTag):
            crypto.decrypt(ct, header, global_seq=2, path_id=0)

    def test_wrong_path_id(self, crypto):
        header = os.urandom(28)
        ct = crypto.encrypt(b"data", header, global_seq=1, path_id=0)
        with pytest.raises(InvalidTag):
            crypto.decrypt(ct, header, global_seq=1, path_id=1)


class TestNonceUniqueness:
    def test_different_seq_different_nonce(self):
        n1 = PacketCrypto._derive_nonce(1, 0)
        n2 = PacketCrypto._derive_nonce(2, 0)
        assert n1 != n2

    def test_different_path_different_nonce(self):
        n1 = PacketCrypto._derive_nonce(1, 0)
        n2 = PacketCrypto._derive_nonce(1, 1)
        assert n1 != n2

    def test_nonce_length(self):
        nonce = PacketCrypto._derive_nonce(42, 3)
        assert len(nonce) == 12


class TestKeyValidation:
    def test_wrong_key_length(self):
        with pytest.raises(ValueError, match="32 bytes"):
            PacketCrypto(b"short")

    def test_generate_key_length(self):
        key = PacketCrypto.generate_key()
        assert len(key) == 32


class TestPerformance:
    def test_encrypt_decrypt_speed(self, crypto):
        header = os.urandom(28)
        plaintext = os.urandom(1400)
        t0 = time.monotonic()
        for i in range(5000):
            ct = crypto.encrypt(plaintext, header, global_seq=i, path_id=0)
            crypto.decrypt(ct, header, global_seq=i, path_id=0)
        elapsed = time.monotonic() - t0
        ops_per_sec = 5000 / elapsed
        assert ops_per_sec > 10000, f"Too slow: {ops_per_sec:.0f} ops/sec"
