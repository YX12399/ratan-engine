#!/usr/bin/env bash
# Scenario A — symmetric bonded: two 50 Mbps links, 1% loss, 20 ms delay with
# 5 ms jitter each. Both paths identical; aggregation should approach 100 Mbps.
#
# Usage: bench/netem/scenario_a_symmetric.sh [duration_sec]
# The script applies shaping, blocks for $duration, then resets qdiscs.
# $duration defaults to 360 (5 min iperf + 60 s teams flow, with headroom).

set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")")
source "$HERE/lib.sh"

DURATION=${1:-360}

P1=$(detect_iface 10.80.1)
P2=$(detect_iface 10.80.2)
[[ -n "$P1" && -n "$P2" ]] || { echo "could not detect bench-client ifaces"; exit 2; }

echo "[scenario A] path1=$P1 path2=$P2 dur=${DURATION}s"

apply_netem "$P1" 50mbit 20 5 1
apply_netem "$P2" 50mbit 20 5 1

show_qdisc "$P1"
show_qdisc "$P2"

trap 'echo "[scenario A] resetting qdiscs"; reset_qdisc "$P1"; reset_qdisc "$P2"' EXIT

sleep "$DURATION"
