"""
Unified FEC Engine — auto-selects between XOR and Reed-Solomon
based on current path loss rates.

Modes:
  - none: No FEC (0% overhead)
  - xor: XOR parity, 1 loss recovery per group (25% overhead at k=4)
  - reed_solomon: RS coding, multi-loss recovery (25-67% overhead)
  - replicate: Full replication on all paths (100% overhead)
  - auto: Select mode based on observed loss rate
"""

import logging
from typing import Optional

from hyperagg.fec.xor_fec import XORFecEncoder, XORFecDecoder
from hyperagg.fec.rs_fec import RSFecEncoder, RSFecDecoder

logger = logging.getLogger("hyperagg.fec.engine")


class FecEngine:
    """
    Unified FEC interface for the tunnel client/server.
    Auto-selects between XOR and RS based on current path loss rates.
    """

    def __init__(self, config: dict):
        fec_cfg = config.get("fec", {})
        self._mode_setting = fec_cfg.get("mode", "auto")
        self._xor_group_size = fec_cfg.get("xor_group_size", 4)
        self._rs_data = fec_cfg.get("rs_data_shards", 8)
        self._rs_parity = fec_cfg.get("rs_parity_shards", 2)
        self._replication_threshold = fec_cfg.get("replication_threshold", 0.15)
        self._group_timeout_ms = fec_cfg.get("group_timeout_ms", 500)

        # Auto-mode thresholds
        self._xor_max_loss = 0.02   # < 2% → XOR
        self._rs_max_loss = 0.10    # < 10% → RS
        self._rs_heavy_max = 0.15   # < 15% → RS with more parity

        # Current active mode
        self._current_mode = self._resolve_mode(self._mode_setting, 0.0)

        # Encoders
        self._xor_encoder = XORFecEncoder(self._xor_group_size)
        self._rs_encoder = RSFecEncoder(self._rs_data, self._rs_parity)
        self._rs_heavy_encoder = RSFecEncoder(
            max(4, self._rs_data - 2),
            self._rs_parity + 2,
        )

        # Decoders
        self._xor_decoder = XORFecDecoder(self._xor_group_size, self._group_timeout_ms)
        self._rs_decoder = RSFecDecoder(self._rs_data, self._rs_parity, self._group_timeout_ms)
        self._rs_heavy_decoder = RSFecDecoder(
            max(4, self._rs_data - 2),
            self._rs_parity + 2,
            self._group_timeout_ms,
        )

        # Stats
        self._total_recoveries = 0
        self._mode_changes = 0

    @property
    def current_mode(self) -> str:
        return self._current_mode

    @property
    def overhead_pct(self) -> float:
        """Current FEC overhead as a percentage."""
        if self._current_mode == "none":
            return 0.0
        elif self._current_mode == "xor":
            return 100.0 / self._xor_group_size
        elif self._current_mode == "reed_solomon":
            return 100.0 * self._rs_parity / self._rs_data
        elif self._current_mode == "reed_solomon_heavy":
            heavy_data = max(4, self._rs_data - 2)
            heavy_parity = self._rs_parity + 2
            return 100.0 * heavy_parity / heavy_data
        elif self._current_mode == "replicate":
            return 100.0
        return 0.0

    @property
    def recoveries(self) -> int:
        return self._total_recoveries

    def _resolve_mode(self, setting: str, avg_loss: float) -> str:
        """Determine FEC mode from setting and loss rate."""
        if setting != "auto":
            return setting

        if avg_loss < self._xor_max_loss:
            return "xor"
        elif avg_loss < self._rs_max_loss:
            return "reed_solomon"
        elif avg_loss < self._rs_heavy_max:
            return "reed_solomon_heavy"
        else:
            return "replicate"

    def update_mode(self, path_loss: dict[int, float]) -> None:
        """
        Update FEC mode based on current path loss rates.
        Called by the scheduler when path quality changes.
        """
        if self._mode_setting != "auto":
            return

        if not path_loss:
            return

        avg_loss = sum(path_loss.values()) / len(path_loss)
        new_mode = self._resolve_mode("auto", avg_loss)

        if new_mode != self._current_mode:
            logger.info(
                f"FEC mode change: {self._current_mode} → {new_mode} "
                f"(avg_loss={avg_loss:.1%})"
            )
            self._current_mode = new_mode
            self._mode_changes += 1

    def encode_packet(
        self, payload: bytes, traffic_tier: str = "bulk"
    ) -> list[tuple[bytes, dict]]:
        """
        Encode a data packet through FEC.

        Args:
            payload: Raw packet payload.
            traffic_tier: "realtime", "streaming", or "bulk".

        Returns:
            List of (payload, fec_metadata) tuples.
            fec_metadata: {"fec_group_id": int, "fec_index": int,
                          "fec_group_size": int, "is_parity": bool}
            Normally returns 1 item. When a group completes, also returns parity.
        """
        # Tier-based mode override
        mode = self._current_mode
        if traffic_tier == "realtime":
            mode = "replicate"
        elif traffic_tier == "streaming" and mode == "xor":
            mode = "reed_solomon"

        if mode == "none":
            return [(payload, {
                "fec_group_id": 0, "fec_index": 0,
                "fec_group_size": 0, "is_parity": False,
            })]

        if mode == "replicate":
            return [(payload, {
                "fec_group_id": 0, "fec_index": 0,
                "fec_group_size": 0, "is_parity": False,
                "replicate": True,
            })]

        if mode == "xor":
            return self._encode_xor(payload)
        elif mode == "reed_solomon":
            return self._encode_rs(payload, self._rs_encoder)
        elif mode == "reed_solomon_heavy":
            return self._encode_rs(payload, self._rs_heavy_encoder)

        return [(payload, {
            "fec_group_id": 0, "fec_index": 0,
            "fec_group_size": 0, "is_parity": False,
        })]

    def _encode_xor(self, payload: bytes) -> list[tuple[bytes, dict]]:
        """Encode through XOR FEC."""
        group_id, fec_index, parity = self._xor_encoder.add_packet(payload)
        group_size = self._xor_group_size + 1  # data + 1 parity

        result = [(payload, {
            "fec_group_id": group_id,
            "fec_index": fec_index,
            "fec_group_size": group_size,
            "is_parity": False,
        })]

        if parity is not None:
            result.append((parity, {
                "fec_group_id": group_id,
                "fec_index": self._xor_group_size,  # parity is last
                "fec_group_size": group_size,
                "is_parity": True,
            }))

        return result

    def _encode_rs(self, payload: bytes, encoder: RSFecEncoder) -> list[tuple[bytes, dict]]:
        """Encode through RS FEC."""
        group_id, fec_index, parity_list = encoder.add_packet(payload)
        total = encoder.data_shards + encoder.parity_shards

        result = [(payload, {
            "fec_group_id": group_id,
            "fec_index": fec_index,
            "fec_group_size": total,
            "is_parity": False,
        })]

        if parity_list is not None:
            for p_idx, parity in enumerate(parity_list):
                result.append((parity, {
                    "fec_group_id": group_id,
                    "fec_index": encoder.data_shards + p_idx,
                    "fec_group_size": total,
                    "is_parity": True,
                }))

        return result

    def decode_packet(
        self,
        fec_group_id: int,
        fec_index: int,
        fec_group_size: int,
        is_parity: bool,
        payload: bytes,
    ) -> list[tuple[int, bytes]]:
        """
        Process a received packet through FEC decoder.

        Returns:
            List of (fec_index, recovered_payload) for recovered packets.
            Empty list if no recovery.
        """
        if fec_group_size == 0:
            return []  # No FEC on this packet

        # Determine which decoder to use based on group_size
        if fec_group_size == self._xor_group_size + 1:
            return self._decode_xor(fec_group_id, fec_index, is_parity, payload)
        else:
            return self._decode_rs(fec_group_id, fec_index, fec_group_size, payload)

    def _decode_xor(
        self, group_id: int, fec_index: int, is_parity: bool, payload: bytes
    ) -> list[tuple[int, bytes]]:
        """Decode through XOR FEC."""
        result = self._xor_decoder.receive_packet(
            group_id, fec_index, is_parity, payload
        )
        if result is not None:
            self._total_recoveries += 1
            return [result]
        return []

    def _decode_rs(
        self, group_id: int, shard_index: int, group_size: int, payload: bytes
    ) -> list[tuple[int, bytes]]:
        """Decode through RS FEC — pick the right decoder based on group_size."""
        expected_total = self._rs_data + self._rs_parity
        heavy_data = max(4, self._rs_data - 2)
        heavy_total = heavy_data + self._rs_parity + 2

        if group_size == expected_total:
            decoder = self._rs_decoder
        elif group_size == heavy_total:
            decoder = self._rs_heavy_decoder
        else:
            logger.warning(f"Unknown RS group_size={group_size}")
            return []

        results = decoder.receive_shard(group_id, shard_index, payload)
        self._total_recoveries += len(results)
        return results

    def expire_groups(self) -> None:
        """Purge stale incomplete FEC groups from all decoders."""
        self._xor_decoder.expire_groups()
        self._rs_decoder.expire_groups()
        self._rs_heavy_decoder.expire_groups()

    def get_stats(self) -> dict:
        return {
            "current_mode": self._current_mode,
            "mode_setting": self._mode_setting,
            "overhead_pct": round(self.overhead_pct, 1),
            "total_recoveries": self._total_recoveries,
            "mode_changes": self._mode_changes,
            "xor_decoder_recoveries": self._xor_decoder.recoveries,
            "rs_decoder_recoveries": self._rs_decoder.recoveries,
        }


if __name__ == "__main__":
    print("FEC Engine Auto-Mode Test:")

    config = {
        "fec": {
            "mode": "auto",
            "xor_group_size": 4,
            "rs_data_shards": 8,
            "rs_parity_shards": 2,
        }
    }
    engine = FecEngine(config)

    # Test auto-mode at different loss levels
    engine.update_mode({0: 0.01, 1: 0.01})
    print(f"  Loss 1%: mode={engine.current_mode} (expected: xor)")
    assert engine.current_mode == "xor"

    engine.update_mode({0: 0.05, 1: 0.05})
    print(f"  Loss 5%: mode={engine.current_mode} (expected: reed_solomon)")
    assert engine.current_mode == "reed_solomon"

    engine.update_mode({0: 0.12, 1: 0.12})
    print(f"  Loss 12%: mode={engine.current_mode} (expected: reed_solomon_heavy)")
    assert engine.current_mode == "reed_solomon_heavy"

    engine.update_mode({0: 0.20, 1: 0.20})
    print(f"  Loss 20%: mode={engine.current_mode} (expected: replicate)")
    assert engine.current_mode == "replicate"

    print(f"  Mode changes: {engine._mode_changes}")
    print("FEC Engine Auto-Mode Test: PASSED")
