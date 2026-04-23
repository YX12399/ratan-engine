#!/usr/bin/env bash
# Phase A orchestrator.
#
# For each (scenario, stack, driver), this script:
#   1. Prepares the stack (spin up containers or mptcp endpoints or single-path)
#   2. Kicks off the scenario script (applies tc netem)
#   3. Runs the driver (iperf3 + teams_flow)
#   4. Scrapes metrics and appends one row to bench/results.csv
#
# Designed to be re-run. Appends to results.csv (never truncates unless
# --fresh is passed). Each row is self-identifying via run_ts + scenario +
# stack + driver.
#
# Usage:
#   bench/run_all.sh                        # all 3 scenarios, all 3 stacks
#   bench/run_all.sh --scenario a           # one scenario
#   bench/run_all.sh --stacks hyperagg,single   # skip mptcp
#   bench/run_all.sh --fresh                # truncate results.csv first
#
# This runs 3 × 3 × 2 = 18 rows when full. Allow ~6 min per stack per
# scenario; full run is ~1 hour.

set -euo pipefail

HERE=$(dirname "$(readlink -f "$0")")
REPO=$(dirname "$HERE")
RESULTS=$HERE/results.csv
ARTIFACTS=$HERE/artifacts
mkdir -p "$ARTIFACTS"

CSV_SCHEMA_VERSION=1
CSV_HEADER='schema_version,run_ts,scenario,stack,driver,protocol,duration_s,throughput_mbps,rtt_p50_ms,rtt_p95_ms,rtt_p99_ms,reorder_depth_mean,reorder_depth_peak,fec_overhead_pct,iperf_loss_pct,module_notes'

SCENARIOS=(a b c)
STACKS=(hyperagg mptcp single)
FRESH=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --scenario) SCENARIOS=("$2"); shift 2;;
        --stacks) IFS=',' read -ra STACKS <<< "$2"; shift 2;;
        --fresh) FRESH=1; shift;;
        *) echo "unknown arg: $1"; exit 2;;
    esac
done

if [[ $FRESH -eq 1 || ! -f "$RESULTS" ]]; then
    echo "$CSV_HEADER" > "$RESULTS"
fi

# Run preflight; record exit code to drive mptcp availability.
set +e
"$HERE/preflight.sh"
PF=$?
set -e
if [[ $PF -eq 1 ]]; then
    echo "preflight failed — aborting"
    exit 1
fi
MPTCP_OK=1
[[ $PF -eq 2 ]] && MPTCP_OK=0

bring_up() {
    echo "== docker compose up =="
    (cd "$HERE" && docker compose up -d --build)
    # Wait for /api/state to answer (client doesn't run in compose; server does)
    for i in {1..30}; do
        docker exec bench-server curl -sf http://localhost:8080/api/health >/dev/null 2>&1 && break
        sleep 1
    done
}

bring_down() {
    (cd "$HERE" && docker compose down -v) || true
}
trap bring_down EXIT

record_row() {
    # record_row <scenario> <stack> <driver> <row_json>
    local scenario=$1 stack=$2 driver=$3 json=$4
    local ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    # json has: protocol, duration_s, throughput_mbps, rtt_p50_ms, rtt_p95_ms,
    #           rtt_p99_ms, reorder_depth_mean, reorder_depth_peak,
    #           fec_overhead_pct, iperf_loss_pct, module_notes
    local row
    row=$(printf '%s' "$json" | jq -r --arg sv "$CSV_SCHEMA_VERSION" \
        --arg ts "$ts" --arg s "$scenario" \
        --arg k "$stack" --arg d "$driver" '
        [$sv, $ts, $s, $k, $d,
         (.protocol // ""),
         (.duration_s // ""),
         (.throughput_mbps // ""),
         (.rtt_p50_ms // ""),
         (.rtt_p95_ms // ""),
         (.rtt_p99_ms // ""),
         (.reorder_depth_mean // ""),
         (.reorder_depth_peak // ""),
         (.fec_overhead_pct // ""),
         (.iperf_loss_pct // ""),
         (.module_notes // "")] | @csv')
    echo "$row" >> "$RESULTS"
    echo "[row] $scenario/$stack/$driver: $row"
}

run_hyperagg() {
    # Start hyperagg client inside bench-client, hitting the server container.
    local scenario=$1
    local label=$ARTIFACTS/${scenario}_hyperagg
    mkdir -p "$label"

    docker exec -d bench-client python -m hyperagg --mode client \
        --vps-host 10.80.1.2 --no-tun --api-port 8080 \
        --log-level INFO --log-file /tmp/hyperagg-client.log

    # Scraper runs on the HOST (client's 8080 not exposed, so exec in)
    docker exec -d bench-client python /app/bench/scrape/scrape_hyperagg.py \
        --url http://localhost:8080 \
        --out /tmp/trace.jsonl \
        --duration 360 \
        --packets-csv /tmp/packets.csv

    # iperf3 server is on bench-server (started by the image's iperf3 daemon;
    # in real run_all we'd launch explicitly). For now launch a fresh one.
    docker exec -d bench-server iperf3 -s -D

    # iperf3 UDP driver: from bench-client, target the server via the
    # HyperAgg TUN endpoint. With --no-tun we hit the server IP directly on
    # 10.80.1.2, which bypasses the tunnel — note this in module_notes so
    # reviewers don't mistake it for real bonded traffic. A follow-up
    # TODO_NEXT_SESSION item repeats this with TUN enabled.
    docker exec bench-client bash /app/bench/drivers/iperf_udp.sh \
        10.80.1.2 300 /tmp/iperf.json
    docker cp bench-client:/tmp/iperf.json "$label/iperf.json"
    local tput
    tput=$(jq '.end.sum.bits_per_second / 1e6 | round' "$label/iperf.json")
    local loss
    loss=$(jq '.end.sum.lost_percent' "$label/iperf.json")

    # Teams flow
    docker exec -d bench-server python /app/bench/drivers/teams_flow.py server \
        --bind 0.0.0.0:5000
    sleep 1
    docker exec bench-client python /app/bench/drivers/teams_flow.py client \
        --peer 10.80.1.2:5000 --duration 60 --out /tmp/flow.jsonl
    docker cp bench-client:/tmp/flow.jsonl "$label/flow.jsonl"

    # Scraper finishes ~300 s after start; wait for trace file to stabilise
    sleep 2
    docker cp bench-client:/tmp/trace.jsonl "$label/trace.jsonl" || true
    docker cp bench-client:/tmp/packets.csv "$label/packets.csv" || true

    # Compute percentile rows for both drivers
    local iperf_stats flow_stats
    iperf_stats=$(python3 "$HERE/scrape/percentiles.py" \
        --csv "$label/packets.csv" \
        --trace "$label/trace.jsonl" 2>/dev/null || echo '{}')
    flow_stats=$(python3 "$HERE/scrape/percentiles.py" \
        --jsonl "$label/flow.jsonl" \
        --trace "$label/trace.jsonl" 2>/dev/null || echo '{}')

    record_row "$scenario" hyperagg iperf \
        "$(jq -n --argjson s "$iperf_stats" --arg t "$tput" --arg l "$loss" '{
            protocol: "udp",
            duration_s: 300,
            throughput_mbps: ($t | tonumber? // null),
            rtt_p50_ms: $s.rtt_p50_ms,
            rtt_p95_ms: $s.rtt_p95_ms,
            rtt_p99_ms: $s.rtt_p99_ms,
            reorder_depth_mean: $s.reorder_depth_mean,
            reorder_depth_peak: $s.reorder_depth_peak,
            fec_overhead_pct: $s.fec_overhead_pct_mean,
            iperf_loss_pct: ($l | tonumber? // null),
            module_notes: "scheduler=ai;fec=auto;reorder=adaptive;notun=1"
        }')"
    record_row "$scenario" hyperagg teams \
        "$(jq -n --argjson s "$flow_stats" '{
            protocol: "udp",
            duration_s: 60,
            throughput_mbps: null,
            rtt_p50_ms: $s.rtt_p50_ms,
            rtt_p95_ms: $s.rtt_p95_ms,
            rtt_p99_ms: $s.rtt_p99_ms,
            reorder_depth_mean: $s.reorder_depth_mean,
            reorder_depth_peak: $s.reorder_depth_peak,
            fec_overhead_pct: $s.fec_overhead_pct_mean,
            iperf_loss_pct: null,
            module_notes: "teams-48B-50pps;notun=1"
        }')"

    docker exec bench-client pkill -f "python -m hyperagg" || true
    docker exec bench-server pkill -f "iperf3 -s" || true
    docker exec bench-server pkill -f "teams_flow.py server" || true
}

run_mptcp() {
    local scenario=$1
    if [[ $MPTCP_OK -eq 0 ]]; then
        record_row "$scenario" mptcp iperf \
            '{"protocol":"tcp","duration_s":300,"module_notes":"MPTCP_UNAVAILABLE"}'
        record_row "$scenario" mptcp teams \
            '{"protocol":"tcp","duration_s":60,"module_notes":"MPTCP_UNAVAILABLE"}'
        return
    fi
    local label=$ARTIFACTS/${scenario}_mptcp
    mkdir -p "$label"

    # Add both client interfaces as MPTCP endpoints so subflows can form.
    docker exec bench-client ip mptcp endpoint flush || true
    docker exec bench-client ip mptcp endpoint add 10.80.1.3 dev eth0 subflow || true
    docker exec bench-client ip mptcp endpoint add 10.80.2.3 dev eth1 subflow || true

    docker exec -d bench-server iperf3 -s -D

    set +e
    docker exec bench-client bash /app/bench/drivers/iperf_mptcp.sh \
        10.80.1.2 300 /tmp/iperf.json
    local rc=$?
    set -e
    docker cp bench-client:/tmp/iperf.json "$label/iperf.json" || true

    local notes="subflows>=2"
    [[ $rc -eq 4 ]] && notes="MPTCP_NO_SUBFLOW"
    [[ $rc -ne 0 && $rc -ne 4 ]] && notes="MPTCP_ERROR_rc=$rc"

    local tput
    tput=$(jq '.end.sum_received.bits_per_second / 1e6 | round' "$label/iperf.json" 2>/dev/null || echo null)

    record_row "$scenario" mptcp iperf \
        "$(jq -n --arg t "$tput" --arg n "$notes" '{
            protocol: "tcp",
            duration_s: 300,
            throughput_mbps: ($t | tonumber? // null),
            module_notes: $n
        }')"

    # Teams flow over MPTCP doesn't apply (UDP); record a sentinel row.
    record_row "$scenario" mptcp teams \
        '{"protocol":"tcp","duration_s":60,"module_notes":"N/A-UDP-OVER-MPTCP"}'

    docker exec bench-server pkill -f "iperf3 -s" || true
}

run_single() {
    local scenario=$1
    local label=$ARTIFACTS/${scenario}_single
    mkdir -p "$label"

    # Bring eth1 (path2) down inside the client to force single-path
    docker exec bench-client ip link set eth1 down || true

    docker exec -d bench-server iperf3 -s -D
    docker exec bench-client bash /app/bench/drivers/iperf_udp.sh \
        10.80.1.2 300 /tmp/iperf.json
    docker cp bench-client:/tmp/iperf.json "$label/iperf.json"

    docker exec -d bench-server python /app/bench/drivers/teams_flow.py server \
        --bind 0.0.0.0:5000
    sleep 1
    docker exec bench-client python /app/bench/drivers/teams_flow.py client \
        --peer 10.80.1.2:5000 --duration 60 --out /tmp/flow.jsonl
    docker cp bench-client:/tmp/flow.jsonl "$label/flow.jsonl"

    local tput loss
    tput=$(jq '.end.sum.bits_per_second / 1e6 | round' "$label/iperf.json")
    loss=$(jq '.end.sum.lost_percent' "$label/iperf.json")

    local flow_stats
    flow_stats=$(python3 "$HERE/scrape/percentiles.py" --jsonl "$label/flow.jsonl" 2>/dev/null || echo '{}')

    record_row "$scenario" single iperf \
        "$(jq -n --arg t "$tput" --arg l "$loss" '{
            protocol:"udp", duration_s:300,
            throughput_mbps:($t|tonumber?//null),
            iperf_loss_pct:($l|tonumber?//null),
            module_notes:"path1-only"
        }')"
    record_row "$scenario" single teams \
        "$(jq -n --argjson s "$flow_stats" '{
            protocol:"udp", duration_s:60,
            rtt_p50_ms:$s.rtt_p50_ms, rtt_p95_ms:$s.rtt_p95_ms, rtt_p99_ms:$s.rtt_p99_ms,
            module_notes:"teams-48B-50pps;path1-only"
        }')"

    docker exec bench-client ip link set eth1 up || true
    docker exec bench-server pkill -f "iperf3 -s" || true
    docker exec bench-server pkill -f "teams_flow.py server" || true
}

bring_up

for sc in "${SCENARIOS[@]}"; do
    echo "============================="
    echo " SCENARIO $sc"
    echo "============================="
    # Start the scenario script as a background qdisc driver that lives
    # for the whole stack-set (360 s covers iperf 300 + teams 60).
    "$HERE/netem/scenario_${sc}_"*.sh 420 &
    NETEM_PID=$!
    sleep 2  # let netem take effect

    for st in "${STACKS[@]}"; do
        case "$st" in
            hyperagg) run_hyperagg "$sc";;
            mptcp)    run_mptcp "$sc";;
            single)   run_single "$sc";;
            *) echo "unknown stack: $st"; exit 2;;
        esac
    done

    kill "$NETEM_PID" 2>/dev/null || true
    wait "$NETEM_PID" 2>/dev/null || true
done

echo "All done. Results: $RESULTS"
echo "Artifacts: $ARTIFACTS"
