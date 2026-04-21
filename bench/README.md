# Phase A — Real-World Benchmark Harness

This directory is **new code only**; it does not modify any file under
`hyperagg/`. The goal is to produce measurement — not simulation — for every
performance claim in the repo, so that `RATAN-HyperAgg-Analysis.md` (Phase B)
and the upcoming refactor (Phase C) are grounded in numbers from the wire.

## What this produces

- `results.csv` — 18 rows (3 scenarios × 3 stacks × 2 drivers) of the form
  `scenario, stack, driver, throughput_mbps, rtt_p50_ms, rtt_p95_ms,
  rtt_p99_ms, reorder_depth_mean, reorder_depth_peak, fec_overhead_pct, ...`
- `REPORT.md` — reviewer-facing write-up, including the three most surprising
  findings, the honest-losses section (any metric where HyperAgg was worse
  than MPTCP or single-path, verbatim), and claim contradictions against
  the existing analysis doc.
- `artifacts/<scenario>_<stack>/` — raw iperf3 JSON, teams_flow JSONL,
  packet CSV, and 1-Hz state trace for every run.

## Prerequisites (checked by `preflight.sh`)

- Linux kernel ≥5.6 with MPTCP (`net.mptcp.enabled=1`)
- Docker + compose v2
- `tc`, `iperf3`, `mptcpize`, `jq`, `python3`
- Root (for `tc netem` and `/dev/net/tun`)

If MPTCP is unavailable, the MPTCP rows are recorded as `MPTCP_UNAVAILABLE`
rather than faked.

## One-shot run

```bash
sudo bench/run_all.sh
```

Full matrix takes ~1 hour. Single scenario:

```bash
sudo bench/run_all.sh --scenario a
sudo bench/run_all.sh --stacks hyperagg,single --scenario c
```

`--fresh` truncates `results.csv` before appending.

## Stack details

| stack     | how it runs                                                                                          | protocol |
|-----------|------------------------------------------------------------------------------------------------------|----------|
| hyperagg  | `python -m hyperagg --mode client --vps-host 10.80.1.2 --no-tun`  (targets `bench-server` container) | UDP      |
| mptcp     | `mptcpize run iperf3 -c 10.80.1.2` with `ip mptcp endpoint add` on both NICs                         | **TCP**  |
| single    | Same UDP iperf as hyperagg, but with `eth1` (path2) forced down inside `bench-client`               | UDP      |

**Why the MPTCP row is TCP:** MPTCP is TCP-only; iperf3 cannot drive UDP
over MPTCP. The comparison is still informative, but not apples-to-apples.
`REPORT.md` footnotes this next to every MPTCP-vs-HyperAgg delta.

## Scenarios

| Key | Path 1                      | Path 2                                       |
|-----|-----------------------------|----------------------------------------------|
| A   | 50 Mbps, 1% loss, 20 ms±5   | 50 Mbps, 1% loss, 20 ms±5                    |
| B   | 100 Mbps, 10 ms, 0%         | 10 Mbps, 40 ms, periodically 15% loss + 50ms jitter |
| C   | 50 Mbps, 20 ms              | 50 Mbps, 20 ms → 100% loss at 30s → back at 60s |

`tc netem` applied on the client-side veth of each bench bridge; server side
is unshaped.

## Drivers

- `drivers/iperf_udp.sh` — 5-min iperf3 UDP (`-u -b 0 -l 1200`). Throughput
  and loss% come from iperf3 `--json` output.
- `drivers/iperf_mptcp.sh` — 5-min iperf3 TCP under `mptcpize run`. Aborts
  and records `MPTCP_NO_SUBFLOW` if only a single subflow forms after 3 s.
- `drivers/teams_flow.py` — 60 s 48-byte @ 50 pps bidirectional UDP.
  Emits per-packet rx JSONL; `scrape/percentiles.py` produces p50/p95/p99.

## Metric sources

| Metric                 | Where it comes from                                                               |
|------------------------|-----------------------------------------------------------------------------------|
| throughput_mbps        | iperf3 JSON (`.end.sum.bits_per_second` / `.end.sum_received.bits_per_second`)   |
| rtt_p50/p95/p99_ms     | teams_flow JSONL (all stacks) + `/api/packets/csv` (hyperagg only)              |
| reorder_depth_*        | `/api/state` `.reorder.depth` — HyperAgg only; `—` elsewhere                    |
| fec_overhead_pct       | `/api/state` `.fec.overhead_pct` — HyperAgg only; `—` elsewhere                 |
| module_notes           | `.scheduler.mode`, `.fec.current_mode` captured once per run                    |

See `hyperagg/dashboard/api.py:135` (`/api/state`) and
`hyperagg/dashboard/api.py:181` (`/api/packets/csv`) for the exact shapes.

## Honest losses section

Phase B rewrites `RATAN-HyperAgg-Analysis.md` using only these numbers.
Phase A does **not** modify the analysis doc. If a measured row here
contradicts a claim there, it is noted verbatim in `REPORT.md` §6 without
any rewording. Any row where HyperAgg is **worse** than MPTCP or
single-path goes into `REPORT.md` §4 "Honest losses" — again, verbatim.

## Known limitations (baked into REPORT.md)

- Single-host netem emulation is not a substitute for real Starlink/LTE
  jitter fingerprints. Lab result, not field validation.
- `--no-tun` means the HyperAgg server currently hands packets off at
  the UDP layer rather than through a TUN interface. A Phase A.1 follow-up
  (logged in `TODO_NEXT_SESSION.md`) should repeat the run with TUN
  enabled under `CAP_NET_ADMIN`.
- MPTCP TCP vs HyperAgg UDP is a protocol-mismatched comparison — all
  MPTCP-vs-HyperAgg deltas in the report carry that footnote.
