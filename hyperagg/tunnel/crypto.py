"""
HyperAgg Encryption — ChaCha20-Poly1305 AEAD.

The 28-byte packet header is used as Associated Authenticated Data (AAD):
it is authenticated but NOT encrypted, so the receiver can read path_id
and global_seq before decryption (needed for nonce derivation and routing).

The nonce is derived from (global_seq, path_id) — NOT transmitted on the wire.
This guarantees uniqueness because global_seq is monotonic and path_id
differentiates replicated copies of the same packet.

Wire overhead: 16 bytes (Poly1305 authentication tag only).
"""

import os
import struct

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.exceptions import InvalidTag


TAG_SIZE = 16
NONCE_SIZE = 12


class PacketCrypto:
    """ChaCha20-Poly1305 AEAD encryption for HyperAgg packets."""

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError(f"Key must be 32 bytes, got {len(key)}")
        self._cipher = ChaCha20Poly1305(key)

    def encrypt(self, plaintext: bytes, associated_data: bytes,
                global_seq: int, path_id: int) -> bytes:
        """
        Encrypt payload using ChaCha20-Poly1305.

        Args:
            plaintext: The raw IP packet to encrypt.
            associated_data: The 28-byte HyperAgg header (authenticated, not encrypted).
            global_seq: Global sequence number (for nonce derivation).
            path_id: Path ID (for nonce derivation).

        Returns:
            Ciphertext + 16-byte Poly1305 tag.
        """
        nonce = self._derive_nonce(global_seq, path_id)
        return self._cipher.encrypt(nonce, plaintext, associated_data)

    def decrypt(self, ciphertext_with_tag: bytes, associated_data: bytes,
                global_seq: int, path_id: int) -> bytes:
        """
        Decrypt payload and verify authentication tag.

        Args:
            ciphertext_with_tag: Ciphertext + 16-byte Poly1305 tag.
            associated_data: The 28-byte HyperAgg header.
            global_seq: Global sequence number (for nonce derivation).
            path_id: Path ID (for nonce derivation).

        Returns:
            Decrypted plaintext.

        Raises:
            cryptography.exceptions.InvalidTag: If authentication fails.
        """
        nonce = self._derive_nonce(global_seq, path_id)
        return self._cipher.decrypt(nonce, ciphertext_with_tag, associated_data)

    @staticmethod
    def _derive_nonce(global_seq: int, path_id: int) -> bytes:
        """
        Build 96-bit (12-byte) nonce from global_seq and path_id.

        Layout: global_seq (4 bytes big-endian) + path_id (2 bytes big-endian) + 6 zero bytes.

        This guarantees uniqueness:
        - global_seq increments monotonically per sender
        - path_id differentiates duplicate sends on different paths
        """
        return struct.pack("!IH", global_seq & 0xFFFFFFFF, path_id) + b"\x00" * 6

    @staticmethod
    def generate_key() -> bytes:
        """Generate a random 256-bit key."""
        return os.urandom(32)


if __name__ == "__main__":
    key = PacketCrypto.generate_key()
    crypto = PacketCrypto(key)

    header = b"\x48\x41\x47\x47" + b"\x00" * 24  # Fake 28-byte header
    plaintext = b"Test payload for encryption verification" * 10
    global_seq = 42
    path_id = 1

    # Encrypt
    ciphertext = crypto.encrypt(plaintext, header, global_seq, path_id)
    assert len(ciphertext) == len(plaintext) + TAG_SIZE

    # Decrypt
    recovered = crypto.decrypt(ciphertext, header, global_seq, path_id)
    assert recovered == plaintext

    # Tamper with ciphertext
    tampered = bytearray(ciphertext)
    tampered[0] ^= 0xFF
    try:
        crypto.decrypt(bytes(tampered), header, global_seq, path_id)
        assert False, "Should have raised InvalidTag"
    except InvalidTag:
        pass

    # Tamper with header (AAD)
    bad_header = bytearray(header)
    bad_header[5] ^= 0xFF
    try:
        crypto.decrypt(ciphertext, bytes(bad_header), global_seq, path_id)
        assert False, "Should have raised InvalidTag"
    except InvalidTag:
        pass

    print(f"Crypto test: PASSED (plaintext={len(plaintext)}B, "
          f"ciphertext={len(ciphertext)}B, overhead={TAG_SIZE}B)")
