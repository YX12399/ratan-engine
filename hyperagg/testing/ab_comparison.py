"""
A/B Comparison Framework — HyperAgg vs Legacy MPTCP simulation.

Runs identical network conditions through two models:
  1. HyperAgg: real FEC encode/decode + AI scheduler decisions
  2. Legacy MPTCP: simulated TCP retransmit behavior (3x RTT penalty on loss)

Produces side-by-side metrics: recovery time, packet loss, throughput.
"""

import json
import logging
import os
import random
import statistics
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
from scipy import stats as sp_stats

from hyperagg.fec.fec_engine import FecEngine
from hyperagg.fec.xor_fec import XORFec
from hyperagg.scheduler.path_monitor import PathMonitor
from hyperagg.scheduler.path_predictor import PathPredictor
from hyperagg.scheduler.path_scheduler import PathScheduler

logger = logging.getLogger("hyperagg.testing.ab")

RESULTS_DIR = Path(__file__).parent.parent.parent / "tests" / "results"


@dataclass
class PathCondition:
    """Network condition for one tick."""
    rtt_ms: float
    loss_rate: float  # 0.0 to 1.0
    is_alive: bool


@dataclass
class ABResult:
    """Result from one side of the comparison."""
    total_packets: int
    delivered_packets: int
    lost_packets: int
    recovered_by_fec: int
    effective_loss_pct: float
    avg_latency_ms: float
    p95_latency_ms: float
    max_disruption_ms: float  # Longest gap between delivered packets
    throughput_mbps: float
    recovery_events: list  # [{tick, packets_recovered, method}]


@dataclass
class ABComparison:
    """Full A/B test result."""
    name: str
    duration_ticks: int
    scenario: str
    hyperagg: ABResult
    mptcp: ABResult
    improvement: dict  # Key metrics where HyperAgg beats MPTCP


def generate_scenario(name: str = "starlink_dropout") -> list[dict]:
    """
    Generate a network scenario — returns per-tick conditions for both paths.

    Scenarios:
      starlink_dropout: 90-tick cycle with degradation/dropout/recovery
      highway_to_rural: Cellular degrades as vehicle leaves city coverage
      dual_degradation: Both paths degrade simultaneously (worst case)
    """
    ticks = []

    if name == "starlink_dropout":
        for t in range(90):
            if t < 30:
                sl = PathCondition(30 + random.gauss(0, 3), 0.01, True)
                cell = PathCondition(60 + random.gauss(0, 8), 0.03, True)
            elif t < 45:
                d = (t - 30) / 15.0
                sl = PathCondition(30 + d * 170 + random.gauss(0, 3 + d * 40), 0.01 + d * 0.14, True)
                cell = PathCondition(60 + random.gauss(0, 8), 0.03, True)
            elif t < 55:
                sl = PathCondition(500 + random.gauss(0, 100), 0.80, t < 47)
                cell = PathCondition(65 + random.gauss(0, 10), 0.03, True)
            elif t < 70:
                r = (t - 55) / 15.0
                sl = PathCondition(200 - r * 170 + random.gauss(0, 40 - r * 37), 0.15 - r * 0.14, True)
                cell = PathCondition(60 + random.gauss(0, 8), 0.03, True)
            else:
                sl = PathCondition(30 + random.gauss(0, 3), 0.01, True)
                cell = PathCondition(60 + random.gauss(0, 8), 0.03, True)
            ticks.append({"starlink": sl, "cellular": cell})

    elif name == "highway_to_rural":
        for t in range(90):
            d = t / 90.0  # 0→1 as vehicle moves to rural
            sl = PathCondition(30 + random.gauss(0, 3), 0.01, True)  # Starlink stable
            cell = PathCondition(
                60 + d * 140 + random.gauss(0, 8 + d * 20),
                0.03 + d * 0.17, d < 0.8  # Cellular dies at 80% through
            )
            ticks.append({"starlink": sl, "cellular": cell})

    elif name == "dual_degradation":
        for t in range(90):
            if t < 30:
                sl = PathCondition(30 + random.gauss(0, 3), 0.01, True)
                cell = PathCondition(60 + random.gauss(0, 8), 0.03, True)
            elif t < 60:
                d = (t - 30) / 30.0
                sl = PathCondition(30 + d * 100 + random.gauss(0, 5 + d * 20), 0.01 + d * 0.09, True)
                cell = PathCondition(60 + d * 80 + random.gauss(0, 8 + d * 15), 0.03 + d * 0.07, True)
            else:
                r = (t - 60) / 30.0
                sl = PathCondition(130 - r * 100 + random.gauss(0, 25 - r * 20), 0.10 - r * 0.09, True)
                cell = PathCondition(140 - r * 80 + random.gauss(0, 23 - r * 15), 0.10 - r * 0.07, True)
            ticks.append({"starlink": sl, "cellular": cell})

    return ticks


def run_hyperagg_sim(scenario: list[dict], config: dict) -> ABResult:
    """Run a scenario through the REAL HyperAgg engine."""
    monitor = PathMonitor(config)
    monitor.register_path(0)
    monitor.register_path(1)
    predictor = PathPredictor(config)
    scheduler = PathScheduler(config, monitor, predictor)
    fec = FecEngine(config)

    total = 0
    delivered = 0
    lost = 0
    fec_recovered = 0
    latencies = []
    delivery_times = []
    recovery_events = []
    global_seq = 0

    for tick_idx, tick in enumerate(scenario):
        sl = tick["starlink"]
        cell = tick["cellular"]

        # Feed real measurements
        if sl.is_alive and random.random() > sl.loss_rate:
            monitor.record_rtt(0, max(1, sl.rtt_ms))
        else:
            monitor.record_loss(0)
        if cell.is_alive and random.random() > cell.loss_rate:
            monitor.record_rtt(1, max(1, cell.rtt_ms))
        else:
            monitor.record_loss(1)

        scheduler.update_predictions()
        states = monitor.get_all_path_states()
        fec.update_mode({pid: s.loss_pct for pid, s in states.items()})

        # Process 100 packets per tick
        tick_recovered = 0
        for _ in range(100):
            total += 1
            payload = os.urandom(1380)
            fec_results = fec.encode_packet(payload, "bulk")

            for fec_payload, fec_meta in fec_results:
                decision = scheduler.schedule(global_seq, "bulk")
                for assignment in decision.assignments:
                    if not assignment.send:
                        continue
                    pid = assignment.path_id
                    cond = sl if pid == 0 else cell
                    pkt_lost = random.random() < cond.loss_rate or not cond.is_alive

                    if not pkt_lost:
                        latencies.append(cond.rtt_ms)
                        delivery_times.append(tick_idx)
                        delivered += 1

                        # FEC decode
                        if fec_meta.get("fec_group_size", 0) > 0:
                            recovered = fec.decode_packet(
                                fec_meta.get("fec_group_id", 0),
                                fec_meta.get("fec_index", 0),
                                fec_meta.get("fec_group_size", 0),
                                fec_meta.get("is_parity", False),
                                fec_payload,
                            )
                            fec_recovered += len(recovered)
                            tick_recovered += len(recovered)
                    else:
                        lost += 1

            global_seq += 1

        if tick_recovered > 0:
            recovery_events.append({
                "tick": tick_idx,
                "packets_recovered": tick_recovered,
                "method": fec.current_mode,
            })

        fec.expire_groups()

    # Compute max disruption
    max_gap = 0
    if len(delivery_times) > 1:
        gaps = [delivery_times[i + 1] - delivery_times[i] for i in range(len(delivery_times) - 1)]
        max_gap = max(gaps) * 1000  # Convert ticks to ms (1 tick = 1s)

    return ABResult(
        total_packets=total,
        delivered_packets=delivered,
        lost_packets=lost,
        recovered_by_fec=fec_recovered,
        effective_loss_pct=round(lost / max(total, 1) * 100, 2),
        avg_latency_ms=round(statistics.mean(latencies), 1) if latencies else 0,
        p95_latency_ms=round(sorted(latencies)[int(len(latencies) * 0.95)], 1) if len(latencies) > 1 else 0,
        max_disruption_ms=max_gap,
        throughput_mbps=round(delivered * 1380 * 8 / (len(scenario) * 1_000_000), 1),
        recovery_events=recovery_events,
    )


def run_mptcp_sim(scenario: list[dict]) -> ABResult:
    """
    Simulate legacy MPTCP behavior for the same scenario.

    MPTCP characteristics:
    - Connection-level routing (not per-packet)
    - On loss: TCP retransmit adds 3x RTT penalty
    - Path switching: detected by mwan3 every 5 seconds
    - During switch: 2-10 second disruption
    """
    total = 0
    delivered = 0
    lost = 0
    latencies = []
    delivery_times = []
    active_path = 0  # MPTCP sticks to one primary path
    switch_cooldown = 0
    mwan3_poll_counter = 0

    for tick_idx, tick in enumerate(scenario):
        sl = tick["starlink"]
        cell = tick["cellular"]
        conds = {0: sl, 1: cell}

        # mwan3 polls every 5 ticks (5 seconds)
        mwan3_poll_counter += 1
        if mwan3_poll_counter >= 5:
            mwan3_poll_counter = 0
            # Check if active path is down
            active_cond = conds[active_path]
            if not active_cond.is_alive or active_cond.loss_rate > 0.3:
                # Switch path — 2-10 second disruption
                old_path = active_path
                active_path = 1 - active_path
                switch_cooldown = random.randint(2, 10)  # Disruption ticks
                logger.debug(f"MPTCP: switching {old_path}->{active_path}, cooldown={switch_cooldown}s")

        # During switch cooldown, all packets are lost (TCP retransmit cascade)
        if switch_cooldown > 0:
            switch_cooldown -= 1
            for _ in range(100):
                total += 1
                lost += 1
            continue

        active_cond = conds[active_path]

        for _ in range(100):
            total += 1

            if not active_cond.is_alive or random.random() < active_cond.loss_rate:
                # Lost — TCP retransmit adds 3x RTT penalty
                lost += 1
                latency = active_cond.rtt_ms * 3  # Retransmit penalty
                latencies.append(latency)
            else:
                delivered += 1
                latencies.append(active_cond.rtt_ms)
                delivery_times.append(tick_idx)

    max_gap = 0
    if len(delivery_times) > 1:
        gaps = [delivery_times[i + 1] - delivery_times[i] for i in range(len(delivery_times) - 1)]
        max_gap = max(gaps) * 1000

    return ABResult(
        total_packets=total,
        delivered_packets=delivered,
        lost_packets=lost,
        recovered_by_fec=0,  # MPTCP has no FEC
        effective_loss_pct=round(lost / max(total, 1) * 100, 2),
        avg_latency_ms=round(statistics.mean(latencies), 1) if latencies else 0,
        p95_latency_ms=round(sorted(latencies)[int(len(latencies) * 0.95)], 1) if len(latencies) > 1 else 0,
        max_disruption_ms=max_gap,
        throughput_mbps=round(delivered * 1380 * 8 / (len(scenario) * 1_000_000), 1),
        recovery_events=[],
    )


def run_comparison(
    name: str = "Starlink Dropout",
    scenario_name: str = "starlink_dropout",
    config: dict = None,
) -> ABComparison:
    """Run a full A/B comparison and return results."""
    if config is None:
        config = {
            "fec": {"mode": "auto", "xor_group_size": 4, "rs_data_shards": 8, "rs_parity_shards": 2},
            "scheduler": {
                "mode": "ai", "history_window": 50, "ewma_alpha": 0.3,
                "latency_budget_ms": 150, "probe_interval_ms": 100,
                "jitter_weight": 0.3, "loss_weight": 0.5, "rtt_weight": 0.2,
            },
        }

    scenario = generate_scenario(scenario_name)

    logger.info(f"Running A/B comparison: {name} ({len(scenario)} ticks)")
    logger.info("Running HyperAgg simulation...")
    hyperagg = run_hyperagg_sim(scenario, config)

    logger.info("Running MPTCP simulation...")
    mptcp = run_mptcp_sim(scenario)

    # Compute improvement metrics
    improvement = {
        "loss_reduction_pct": round(mptcp.effective_loss_pct - hyperagg.effective_loss_pct, 2),
        "latency_improvement_ms": round(mptcp.avg_latency_ms - hyperagg.avg_latency_ms, 1),
        "disruption_improvement_ms": round(mptcp.max_disruption_ms - hyperagg.max_disruption_ms, 0),
        "throughput_gain_mbps": round(hyperagg.throughput_mbps - mptcp.throughput_mbps, 1),
        "fec_recoveries": hyperagg.recovered_by_fec,
    }

    result = ABComparison(
        name=name,
        duration_ticks=len(scenario),
        scenario=scenario_name,
        hyperagg=hyperagg,
        mptcp=mptcp,
        improvement=improvement,
    )

    # Save to disk
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"ab_{scenario_name}_{int(time.time())}.json"
    path.write_text(json.dumps(asdict(result), indent=2))
    logger.info(f"Results saved to {path}")

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    for scenario in ["starlink_dropout", "highway_to_rural", "dual_degradation"]:
        result = run_comparison(scenario.replace("_", " ").title(), scenario)

        print(f"\n{'='*60}")
        print(f"Scenario: {result.name}")
        print(f"{'='*60}")
        print(f"{'Metric':<30} {'HyperAgg':>12} {'MPTCP':>12} {'Delta':>12}")
        print(f"{'-'*66}")
        print(f"{'Packets delivered':<30} {result.hyperagg.delivered_packets:>12,} {result.mptcp.delivered_packets:>12,}")
        print(f"{'Packets lost':<30} {result.hyperagg.lost_packets:>12,} {result.mptcp.lost_packets:>12,}")
        print(f"{'FEC recoveries':<30} {result.hyperagg.recovered_by_fec:>12,} {result.mptcp.recovered_by_fec:>12,}")
        print(f"{'Effective loss %':<30} {result.hyperagg.effective_loss_pct:>11.1f}% {result.mptcp.effective_loss_pct:>11.1f}%")
        print(f"{'Avg latency (ms)':<30} {result.hyperagg.avg_latency_ms:>12.1f} {result.mptcp.avg_latency_ms:>12.1f}")
        print(f"{'P95 latency (ms)':<30} {result.hyperagg.p95_latency_ms:>12.1f} {result.mptcp.p95_latency_ms:>12.1f}")
        print(f"{'Max disruption (ms)':<30} {result.hyperagg.max_disruption_ms:>12.0f} {result.mptcp.max_disruption_ms:>12.0f}")
        print(f"{'Throughput (Mbps)':<30} {result.hyperagg.throughput_mbps:>12.1f} {result.mptcp.throughput_mbps:>12.1f}")
        print(f"\nHyperAgg advantage:")
        for k, v in result.improvement.items():
            print(f"  {k}: {v}")
