#!/usr/bin/env python3
"""Compute p50/p95/p99 RTT and summary stats from either:
  - a teams_flow JSONL file (one record per rx packet, field `rtt_ms`), or
  - the hyperagg packets CSV (exported from /api/packets/csv, rtt_ms column).

Also derives from a scrape_hyperagg.py JSONL trace:
  - mean and peak reorder_buffer_depth
  - mean fec_overhead_pct
  - final cumulative throughput_mbps (effective_throughput_mbps from
    state.throughput.compute())

Output: single JSON object on stdout, suitable for run_all.sh to jq into
the results.csv row.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path


def percentile(values: list[float], p: float) -> float:
    if not values:
        return math.nan
    values = sorted(values)
    k = (len(values) - 1) * p
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return values[int(k)]
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def rtt_from_jsonl(path: Path) -> list[float]:
    rtts: list[float] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "rtt_ms" in rec:
            rtts.append(float(rec["rtt_ms"]))
    return rtts


def rtt_from_csv(path: Path) -> list[float]:
    rtts: list[float] = []
    with path.open() as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            v = row.get("rtt_ms")
            if v in (None, "", "null"):
                continue
            try:
                rtts.append(float(v))
            except ValueError:
                continue
    return rtts


def stats_from_trace(trace: Path) -> dict:
    """Pull reorder depth + FEC overhead + throughput from a scrape trace."""
    depths: list[float] = []
    overheads: list[float] = []
    throughput_final: float | None = None
    for line in trace.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        st = rec.get("state") or {}
        reorder = st.get("reorder") or {}
        if "depth" in reorder:
            depths.append(float(reorder["depth"]))
        fec = st.get("fec") or {}
        if "overhead_pct" in fec:
            overheads.append(float(fec["overhead_pct"]))
        tp = st.get("throughput") or {}
        if "effective_throughput_mbps" in tp:
            throughput_final = float(tp["effective_throughput_mbps"])
    return {
        "reorder_depth_mean": round(statistics.fmean(depths), 2) if depths else None,
        "reorder_depth_peak": max(depths) if depths else None,
        "fec_overhead_pct_mean": round(statistics.fmean(overheads), 2) if overheads else None,
        "effective_throughput_mbps": throughput_final,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--jsonl", type=Path, help="teams_flow output JSONL")
    src.add_argument("--csv", type=Path, help="hyperagg packets CSV")
    ap.add_argument("--trace", type=Path, default=None,
                    help="scrape_hyperagg.py JSONL for module-level stats")
    args = ap.parse_args()

    rtts = rtt_from_jsonl(args.jsonl) if args.jsonl else rtt_from_csv(args.csv)
    out: dict = {
        "samples": len(rtts),
        "rtt_p50_ms": round(percentile(rtts, 0.50), 2) if rtts else None,
        "rtt_p95_ms": round(percentile(rtts, 0.95), 2) if rtts else None,
        "rtt_p99_ms": round(percentile(rtts, 0.99), 2) if rtts else None,
    }
    if args.trace and args.trace.exists():
        out.update(stats_from_trace(args.trace))
    print(json.dumps(out))


if __name__ == "__main__":
    main()
