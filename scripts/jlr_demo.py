#!/usr/bin/env python3
"""
JLR Demo Script — Manchester Driving Scenario.

Simulates a vehicle driving through varying network conditions:
  1. Highway (0-30s): Starlink + cellular both strong
  2. Bridge/tunnel (30-50s): Starlink blocked, cellular-only
  3. Rural area (50-75s): Cellular degrades, Starlink recovers
  4. Return to highway (75-90s): Both paths strong again

Runs the scenario through real HyperAgg FEC + AI scheduler and
simulated MPTCP, producing a comparison report.

Usage:
  python scripts/jlr_demo.py                      # Quick 90-tick demo
  python scripts/jlr_demo.py --dashboard           # Also start dashboard at :8080
  python scripts/jlr_demo.py --output report.json   # Save report to file
"""

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hyperagg.testing.ab_comparison import (
    PathCondition, run_hyperagg_sim, run_mptcp_sim, ABComparison,
)
from dataclasses import asdict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
)
logger = logging.getLogger("jlr_demo")

# Manchester driving scenario
SCENARIO_NAME = "Manchester → M62 → Pennines"
SCENARIO_DESC = """
Vehicle route: Tata Communications Manchester office → M62 motorway → Pennines

Phase 1 (Highway, 0-30s): Strong Starlink + strong cellular.
  Both paths operational, AI scheduler distributes traffic optimally.

Phase 2 (Bridge/Tunnel, 30-50s): Vehicle passes under bridge structure.
  Starlink blocked (line-of-sight lost), cellular-only.
  HyperAgg: FEC covers the gap, scheduler shifts instantly.
  MPTCP: 2-10 second disruption as TCP retransmits cascade.

Phase 3 (Rural, 50-75s): Into the Pennines, cellular towers sparse.
  Starlink recovers, cellular degrades (high RTT, packet loss).
  HyperAgg: AI scheduler re-integrates Starlink, shifts away from cellular.
  MPTCP: mwan3 takes 5-10 seconds to detect cellular degradation.

Phase 4 (Highway return, 75-90s): Back on M62 approaching Leeds.
  Both paths strong again. Full aggregation resumes.
"""


def generate_manchester_scenario() -> list[dict]:
    """Generate the Manchester driving scenario with realistic conditions."""
    ticks = []

    for t in range(90):
        if t < 30:
            # Highway: both paths excellent
            sl = PathCondition(
                rtt_ms=max(5, 25 + random.gauss(0, 2)),
                loss_rate=0.005,
                is_alive=True,
            )
            cell = PathCondition(
                rtt_ms=max(10, 45 + random.gauss(0, 5)),
                loss_rate=0.02,
                is_alive=True,
            )

        elif t < 50:
            # Bridge/tunnel: Starlink blocked, cellular only
            bridge_depth = min(1.0, (t - 30) / 5.0)  # Ramps to full block
            if t >= 35:
                bridge_depth = 1.0
            if t >= 45:
                bridge_depth = max(0, 1.0 - (t - 45) / 5.0)  # Ramps back

            sl = PathCondition(
                rtt_ms=max(5, 25 + bridge_depth * 500 + random.gauss(0, 2 + bridge_depth * 100)),
                loss_rate=min(0.95, 0.005 + bridge_depth * 0.9),
                is_alive=bridge_depth < 0.8,
            )
            cell = PathCondition(
                rtt_ms=max(10, 50 + random.gauss(0, 8)),
                loss_rate=0.03,
                is_alive=True,
            )

        elif t < 75:
            # Rural Pennines: cellular degrades, Starlink strong
            rural = (t - 50) / 25.0  # 0→1 as we go deeper into hills
            sl = PathCondition(
                rtt_ms=max(5, 28 + random.gauss(0, 3)),
                loss_rate=0.01,
                is_alive=True,
            )
            cell = PathCondition(
                rtt_ms=max(10, 50 + rural * 120 + random.gauss(0, 8 + rural * 25)),
                loss_rate=min(0.5, 0.02 + rural * 0.18),
                is_alive=rural < 0.85,  # Cellular drops out in deep rural
            )

        else:
            # Highway return: both strong
            sl = PathCondition(
                rtt_ms=max(5, 26 + random.gauss(0, 2)),
                loss_rate=0.005,
                is_alive=True,
            )
            cell = PathCondition(
                rtt_ms=max(10, 48 + random.gauss(0, 6)),
                loss_rate=0.02,
                is_alive=True,
            )

        ticks.append({"starlink": sl, "cellular": cell})

    return ticks


def run_demo(output_path: str = None) -> dict:
    """Run the full JLR demo and return results."""
    config = {
        "fec": {"mode": "auto", "xor_group_size": 4, "rs_data_shards": 8, "rs_parity_shards": 2},
        "scheduler": {
            "mode": "ai", "history_window": 50, "ewma_alpha": 0.3,
            "latency_budget_ms": 150, "probe_interval_ms": 100,
            "jitter_weight": 0.3, "loss_weight": 0.5, "rtt_weight": 0.2,
        },
    }

    print(f"\n{'='*70}")
    print(f"  RATAN HyperAgg — JLR Demo: {SCENARIO_NAME}")
    print(f"{'='*70}")
    print(SCENARIO_DESC)

    scenario = generate_manchester_scenario()

    print("Running HyperAgg (real FEC + AI scheduler)...")
    t0 = time.monotonic()
    hyperagg = run_hyperagg_sim(scenario, config)
    ha_time = time.monotonic() - t0

    print("Running legacy MPTCP simulation...")
    t0 = time.monotonic()
    mptcp = run_mptcp_sim(scenario)
    mptcp_time = time.monotonic() - t0

    improvement = {
        "loss_reduction_pct": round(mptcp.effective_loss_pct - hyperagg.effective_loss_pct, 2),
        "latency_improvement_ms": round(mptcp.avg_latency_ms - hyperagg.avg_latency_ms, 1),
        "disruption_improvement_ms": round(mptcp.max_disruption_ms - hyperagg.max_disruption_ms, 0),
        "throughput_gain_mbps": round(hyperagg.throughput_mbps - mptcp.throughput_mbps, 1),
        "fec_recoveries": hyperagg.recovered_by_fec,
    }

    # Print results
    print(f"\n{'='*70}")
    print(f"  RESULTS: HyperAgg vs MPTCP")
    print(f"{'='*70}")
    print(f"\n{'Metric':<35} {'HyperAgg':>12} {'MPTCP':>12} {'Winner':>10}")
    print(f"{'-'*71}")

    def row(label, ha, mp, unit="", lower_better=False):
        winner = "HyperAgg" if (ha < mp if lower_better else ha > mp) else "MPTCP"
        if ha == mp:
            winner = "Tie"
        print(f"{label:<35} {ha:>11}{unit} {mp:>11}{unit}  {winner:>8}")

    row("Packets delivered", hyperagg.delivered_packets, mptcp.delivered_packets)
    row("Packets lost", hyperagg.lost_packets, mptcp.lost_packets, lower_better=True)
    row("FEC recoveries", hyperagg.recovered_by_fec, mptcp.recovered_by_fec)
    row("Effective loss", hyperagg.effective_loss_pct, mptcp.effective_loss_pct, "%", lower_better=True)
    row("Avg latency", hyperagg.avg_latency_ms, mptcp.avg_latency_ms, "ms", lower_better=True)
    row("P95 latency", hyperagg.p95_latency_ms, mptcp.p95_latency_ms, "ms", lower_better=True)
    row("Max disruption", hyperagg.max_disruption_ms, mptcp.max_disruption_ms, "ms", lower_better=True)
    row("Throughput", hyperagg.throughput_mbps, mptcp.throughput_mbps, " Mbps")

    print(f"\n  KEY TAKEAWAY:")
    if improvement["disruption_improvement_ms"] > 0:
        print(f"  HyperAgg reduces max disruption by {improvement['disruption_improvement_ms']:.0f}ms")
        print(f"  ({mptcp.max_disruption_ms:.0f}ms MPTCP → {hyperagg.max_disruption_ms:.0f}ms HyperAgg)")
    print(f"  FEC recovered {hyperagg.recovered_by_fec} packets that MPTCP would have lost")
    print(f"  {improvement['throughput_gain_mbps']} Mbps higher throughput during degradation")
    print()

    result = asdict(ABComparison(
        name=SCENARIO_NAME,
        duration_ticks=len(scenario),
        scenario="manchester_driving",
        hyperagg=hyperagg,
        mptcp=mptcp,
        improvement=improvement,
    ))

    if output_path:
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)
        print(f"  Full report saved to: {output_path}")

    return result


def main():
    parser = argparse.ArgumentParser(description="JLR Demo — Manchester Driving Scenario")
    parser.add_argument("--output", "-o", default=None, help="Save report to JSON file")
    parser.add_argument("--dashboard", action="store_true", help="Also start dashboard at :8080")
    args = parser.parse_args()

    if args.dashboard:
        print("Starting HyperAgg in demo mode with dashboard...")
        os.system(f"python -m hyperagg --mode demo --api-port 8080 &")
        time.sleep(3)
        print("Dashboard running at http://localhost:8080")
        print()

    output = args.output or f"tests/results/jlr_demo_{int(time.time())}.json"
    run_demo(output)


if __name__ == "__main__":
    main()
