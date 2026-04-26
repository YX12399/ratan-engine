#!/usr/bin/env bash
# iperf3 TCP under mptcpize — the MPTCP baseline row. MPTCP is TCP-only, so
# the comparison against HyperAgg UDP is footnoted in REPORT.md. A 2-subflow
# minimum is enforced: if only one subflow forms, this driver aborts with
# a nonzero exit so run_all.sh records the row as MPTCP_NO_SUBFLOW rather
# than silently pretending bonding happened.
#
# Args:
#   $1  server IP (reachable over both 10.80.1.x and 10.80.2.x)
#   $2  duration seconds (default 300)
#   $3  output JSON path

set -euo pipefail

SRV=${1:?"usage: $0 <server-ip> [duration] [out.json]"}
DUR=${2:-300}
OUT=${3:-/tmp/iperf_mptcp.json}

command -v mptcpize >/dev/null || { echo "mptcpize not installed"; exit 3; }

echo "[iperf-mptcp] -> $SRV for ${DUR}s -> $OUT"

mptcpize run iperf3 -c "$SRV" -t "$DUR" --json > "$OUT" &
IPERF_PID=$!

# Give iperf3 a couple of seconds, then confirm subflow count via `ss -M`.
sleep 3
SUBFLOWS=$(ss -M | awk '$1=="tcp" && /ESTAB/ {n++} END {print n+0}')
if [[ $SUBFLOWS -lt 2 ]]; then
    echo "[iperf-mptcp] WARN only $SUBFLOWS subflow(s) observed — MPTCP did not bond"
    # Let the run complete so we have something to report, but mark the exit.
    wait "$IPERF_PID" || true
    exit 4
fi

wait "$IPERF_PID"

jq -r '
  .end.sum_received |
  "throughput_mbps=\(.bits_per_second/1e6 | round)"
' "$OUT" || true
