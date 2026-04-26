#!/usr/bin/env bash
# Capability preflight — refuse to run the bench on a host that can't produce
# meaningful numbers. Exit codes:
#   0 ok to run all three stacks
#   1 host is unusable (hard fail — tc / docker / iperf3 missing)
#   2 usable for hyperagg + single, but MPTCP is unavailable; run_all.sh
#     will record mptcp rows as MPTCP_UNAVAILABLE rather than fake them.

set -euo pipefail

fail=0
warn=0

need() {
    command -v "$1" >/dev/null 2>&1 || { echo "missing: $1"; fail=1; }
}

echo "=== preflight ==="
uname -a
echo

need docker
need tc
need iperf3
need python3
need jq

# Kernel >= 5.6 for MPTCP
kver=$(uname -r | awk -F. '{printf "%d.%d", $1, $2}')
awk -v v="$kver" 'BEGIN { if (v+0 < 5.6) exit 1 }' \
    || { echo "kernel $kver is <5.6 — MPTCP not available"; warn=1; }

# MPTCP sysctl
if [[ -f /proc/sys/net/mptcp/enabled ]]; then
    val=$(cat /proc/sys/net/mptcp/enabled)
    [[ "$val" == "1" ]] || { echo "net.mptcp.enabled=$val (want 1)"; warn=1; }
else
    echo "net.mptcp.enabled sysctl not present"
    warn=1
fi

command -v mptcpize >/dev/null 2>&1 || { echo "mptcpize not installed"; warn=1; }

# netem qdisc
modprobe sch_netem 2>/dev/null || true
if ! lsmod | grep -q '^sch_netem'; then
    # maybe built-in; try to apply once on lo
    tc qdisc add dev lo root netem loss 0% 2>/dev/null \
        && tc qdisc del dev lo root 2>/dev/null \
        || { echo "sch_netem unavailable"; fail=1; }
fi

# Docker daemon reachable
docker info >/dev/null 2>&1 || { echo "docker daemon not reachable"; fail=1; }

echo
if [[ $fail -ne 0 ]]; then
    echo "PREFLIGHT: FAIL — missing required tools or kernel support"
    exit 1
elif [[ $warn -ne 0 ]]; then
    echo "PREFLIGHT: PARTIAL — MPTCP unavailable; hyperagg + single only"
    exit 2
else
    echo "PREFLIGHT: OK — all three stacks runnable"
    exit 0
fi
