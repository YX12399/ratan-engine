#!/usr/bin/env bash
# Scenario B — asymmetric Starlink-like: path1 is a stable 100 Mbps, path2
# is a flapping 10 Mbps link (every 10 s: 10 Mbps OK, then 10 Mbps with 15%
# loss + 50 ms jitter for 10 s). Scheduler should prefer path1 under stress.

set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")")
source "$HERE/lib.sh"

DURATION=${1:-360}

P1=$(detect_iface 10.80.1)
P2=$(detect_iface 10.80.2)
[[ -n "$P1" && -n "$P2" ]] || { echo "could not detect bench-client ifaces"; exit 2; }

echo "[scenario B] path1=$P1 (stable 100M) path2=$P2 (flapping 10M) dur=${DURATION}s"

apply_netem "$P1" 100mbit 10 2 0
apply_netem "$P2" 10mbit 40 5 0

cleanup() {
    echo "[scenario B] resetting qdiscs"
    reset_qdisc "$P1"
    reset_qdisc "$P2"
}
trap cleanup EXIT

# Flap path2 every 10 s for the duration of the run.
end=$(( $(date +%s) + DURATION ))
flapped=0
while [[ $(date +%s) -lt $end ]]; do
    sleep 10
    if [[ $flapped -eq 0 ]]; then
        apply_netem "$P2" 10mbit 90 30 15
        flapped=1
        echo "[scenario B] path2 DEGRADED"
    else
        apply_netem "$P2" 10mbit 40 5 0
        flapped=0
        echo "[scenario B] path2 RECOVERED"
    fi
done
