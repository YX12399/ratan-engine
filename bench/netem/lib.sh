# Shared helpers for netem scenarios. Source, don't exec.
#
# All shaping happens on the client-side veth of each bench bridge. The
# server side is left unshaped; asymmetric / switch effects are applied at
# the egress qdisc on the client's interface for that path.
#
# Path 1 iface (inside bench-client): eth0 on 10.80.1.0/24
# Path 2 iface (inside bench-client): eth1 on 10.80.2.0/24
# (Docker names them eth0, eth1 in the order they appear in the networks
# map. scenario scripts validate this assumption with `ip -br addr`.)

set -euo pipefail

CLIENT=bench-client
SERVER=bench-server

docker_exec() {
    docker exec "$1" "${@:2}"
}

detect_iface() {
    # Find the iface inside $CLIENT carrying the given /24 subnet.
    local subnet=$1
    docker exec "$CLIENT" sh -c \
        "ip -br -4 addr | awk -v s='$subnet' '\$3 ~ s {print \$1; exit}'"
}

reset_qdisc() {
    local iface=$1
    docker_exec "$CLIENT" tc qdisc del dev "$iface" root 2>/dev/null || true
}

apply_netem() {
    # apply_netem <iface> <rate> <delay_ms> <jitter_ms> <loss_pct>
    # Uses tbf+netem layered: tbf caps bandwidth, netem adds delay/jitter/loss.
    local iface=$1 rate=$2 delay=$3 jitter=$4 loss=$5
    reset_qdisc "$iface"
    docker_exec "$CLIENT" tc qdisc add dev "$iface" root handle 1: \
        tbf rate "${rate}" burst 32kbit latency 400ms
    docker_exec "$CLIENT" tc qdisc add dev "$iface" parent 1:1 handle 10: \
        netem delay "${delay}ms" "${jitter}ms" loss "${loss}%"
}

kill_path() {
    # Black-hole a path by adding 100% loss. Reversible with apply_netem.
    local iface=$1
    reset_qdisc "$iface"
    docker_exec "$CLIENT" tc qdisc add dev "$iface" root netem loss 100%
}

show_qdisc() {
    local iface=$1
    docker_exec "$CLIENT" tc -s qdisc show dev "$iface" || true
}
