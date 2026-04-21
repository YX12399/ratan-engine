#!/usr/bin/env bash
# iperf3 UDP driver used for hyperagg and single-path stacks.
#
# Args:
#   $1  server IP (10.80.1.2 for single-path, 10.99.0.2 TUN addr if using TUN,
#                 or 10.80.1.2 over the HyperAgg tunnel when --no-tun)
#   $2  duration seconds (default 300)
#   $3  output JSON path (iperf3 --json-stream)
#
# iperf3 -b 0 sets "offered rate = infinite", and the actual ceiling is
# whatever tc netem allows. -l 1200 is an MTU-safe UDP payload.

set -euo pipefail

SRV=${1:?"usage: $0 <server-ip> [duration] [out.json]"}
DUR=${2:-300}
OUT=${3:-/tmp/iperf_udp.json}

echo "[iperf-udp] -> $SRV for ${DUR}s -> $OUT"

iperf3 -c "$SRV" -u -b 0 -t "$DUR" -l 1200 --json > "$OUT"

# Surface a tiny summary for the orchestrator's stdout
jq -r '
  .end.sum |
  "throughput_mbps=\(.bits_per_second/1e6 | round)  " +
  "jitter_ms=\(.jitter_ms)  lost_pct=\(.lost_percent)"
' "$OUT" || true
