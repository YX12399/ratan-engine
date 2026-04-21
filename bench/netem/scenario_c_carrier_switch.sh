#!/usr/bin/env bash
# Scenario C — carrier switch: both paths 50 Mbps / 20 ms; at t=30s path2 is
# killed (100% loss), at t=60s path2 is restored. Measures scheduler
# failover time and whether the reorder buffer survives the transition.
#
# Note: DURATION defaults to 360 but the kill/restore window happens in the
# first 60 s; the remainder runs with both paths healthy. This lets iperf3
# runs that start later see the post-recovery state.

set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")")
source "$HERE/lib.sh"

DURATION=${1:-360}

P1=$(detect_iface 10.80.1)
P2=$(detect_iface 10.80.2)
[[ -n "$P1" && -n "$P2" ]] || { echo "could not detect bench-client ifaces"; exit 2; }

echo "[scenario C] path1=$P1 path2=$P2 dur=${DURATION}s (kill@30, restore@60)"

apply_netem "$P1" 50mbit 20 3 0
apply_netem "$P2" 50mbit 20 3 0

cleanup() {
    echo "[scenario C] resetting qdiscs"
    reset_qdisc "$P1"
    reset_qdisc "$P2"
}
trap cleanup EXIT

sleep 30
echo "[scenario C] killing path2 at t=30"
kill_path "$P2"

sleep 30
echo "[scenario C] restoring path2 at t=60"
apply_netem "$P2" 50mbit 20 3 0

remaining=$(( DURATION - 60 ))
[[ $remaining -gt 0 ]] && sleep "$remaining"
