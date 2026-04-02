"""
Packet Logger — per-packet telemetry for performance analysis.

Logs every scheduling decision, FEC event, and path measurement
to an in-memory ring buffer with optional CSV export.
This is the data source for the dashboard's live packet stream.
"""

import csv
import io
import time
from collections import deque
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass(slots=True)
class PacketLogEntry:
    """One row in the packet log."""
    timestamp: float
    global_seq: int
    path_id: int
    packet_type: str     # "data", "fec_parity", "keepalive", "recovered"
    payload_len: int
    rtt_ms: float        # 0 if not measured on this packet
    fec_group_id: int
    fec_index: int
    scheduler_reason: str
    delivered: bool      # True if successfully delivered to TUN


class PacketLogger:
    """Ring-buffer packet log with CSV export."""

    def __init__(self, max_entries: int = 50000):
        self._log: deque[PacketLogEntry] = deque(maxlen=max_entries)
        self._total_logged = 0

    def log(
        self,
        global_seq: int,
        path_id: int,
        packet_type: str = "data",
        payload_len: int = 0,
        rtt_ms: float = 0.0,
        fec_group_id: int = 0,
        fec_index: int = 0,
        scheduler_reason: str = "",
        delivered: bool = True,
    ) -> None:
        """Log a packet event."""
        entry = PacketLogEntry(
            timestamp=time.time(),
            global_seq=global_seq,
            path_id=path_id,
            packet_type=packet_type,
            payload_len=payload_len,
            rtt_ms=rtt_ms,
            fec_group_id=fec_group_id,
            fec_index=fec_index,
            scheduler_reason=scheduler_reason,
            delivered=delivered,
        )
        self._log.append(entry)
        self._total_logged += 1

    def get_recent(self, n: int = 100) -> list[dict]:
        """Get last N entries as dicts (for WebSocket/JSON)."""
        entries = list(self._log)[-n:]
        return [asdict(e) for e in entries]

    def get_stats(self) -> dict:
        """Aggregate stats from the log."""
        if not self._log:
            return {"total_logged": 0}

        recent = list(self._log)
        data_pkts = [e for e in recent if e.packet_type == "data"]
        fec_pkts = [e for e in recent if e.packet_type == "fec_parity"]
        recovered = [e for e in recent if e.packet_type == "recovered"]

        # Per-path counts
        path_counts: dict[int, int] = {}
        for e in data_pkts:
            path_counts[e.path_id] = path_counts.get(e.path_id, 0) + 1

        # Throughput calculation (last 1 second)
        now = time.time()
        recent_1s = [e for e in recent if now - e.timestamp < 1.0]
        bytes_1s = sum(e.payload_len for e in recent_1s if e.packet_type == "data")
        pps = len([e for e in recent_1s if e.packet_type == "data"])

        return {
            "total_logged": self._total_logged,
            "buffer_size": len(self._log),
            "data_packets": len(data_pkts),
            "fec_packets": len(fec_pkts),
            "recovered_packets": len(recovered),
            "packets_per_path": path_counts,
            "current_pps": pps,
            "current_throughput_mbps": round(bytes_1s * 8 / 1_000_000, 2),
        }

    def export_csv(self) -> str:
        """Export entire log as CSV string."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "timestamp", "global_seq", "path_id", "packet_type",
            "payload_len", "rtt_ms", "fec_group_id", "fec_index",
            "scheduler_reason", "delivered",
        ])
        for entry in self._log:
            writer.writerow([
                f"{entry.timestamp:.6f}", entry.global_seq, entry.path_id,
                entry.packet_type, entry.payload_len, f"{entry.rtt_ms:.2f}",
                entry.fec_group_id, entry.fec_index,
                entry.scheduler_reason, entry.delivered,
            ])
        return output.getvalue()
