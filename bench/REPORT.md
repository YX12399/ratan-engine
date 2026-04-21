# Phase A — HyperAgg Real-World Benchmark Report

> **Status:** TEMPLATE. Populated by running `sudo bench/run_all.sh` on a
> host with kernel ≥5.6, Docker, `tc`, `iperf3`, `mptcpize`. Every `TBD`
> below gets replaced with a number from `results.csv`.

## 1. Host / kernel info

```
uname -a:       TBD — paste `uname -a` output
tc -V:          TBD
MPTCP status:   TBD — output of `cat /proc/sys/net/mptcp/enabled` and `ip mptcp endpoint show`
Docker:         TBD — `docker --version`
Run date (UTC): TBD
Git SHA:        TBD — `git rev-parse HEAD`
```

## 2. Scenario matrix (measured numbers)

Rendered from `results.csv`. Columns marked `—` are not applicable for that
stack (reorder depth and FEC overhead are HyperAgg-internal concepts; MPTCP
and single-path have no equivalents).

### Scenario A — symmetric (50 Mbps × 2, 1% loss, 20 ms)

| stack     | driver | protocol | throughput_mbps | p50_ms | p95_ms | p99_ms | reorder_mean | reorder_peak | fec_overhead_pct | notes          |
|-----------|--------|----------|-----------------|--------|--------|--------|--------------|--------------|------------------|----------------|
| hyperagg  | iperf  | udp      | TBD             | TBD    | TBD    | TBD    | TBD          | TBD          | TBD              | TBD            |
| hyperagg  | teams  | udp      | —               | TBD    | TBD    | TBD    | TBD          | TBD          | TBD              | TBD            |
| mptcp     | iperf  | tcp †    | TBD             | —      | —      | —      | —            | —            | —                | TBD            |
| mptcp     | teams  | —        | —               | —      | —      | —      | —            | —            | —                | N/A-UDP-OVER-MPTCP |
| single    | iperf  | udp      | TBD             | TBD    | TBD    | TBD    | —            | —            | —                | path1-only     |
| single    | teams  | udp      | —               | TBD    | TBD    | TBD    | —            | —            | —                | path1-only     |

### Scenario B — asymmetric (100 stable + 10 flapping)

_(same layout, populated from results.csv)_

### Scenario C — carrier switch (kill@30, restore@60)

_(same layout, populated from results.csv)_

> † MPTCP is TCP-only. The protocol mismatch vs HyperAgg UDP is flagged
> next to every delta in §3.

## 3. Headline deltas

For each scenario:

- `throughput_delta_hyperagg_vs_mptcp  = (HA - MPTCP) / MPTCP × 100%` (TCP-vs-UDP footnote)
- `throughput_delta_hyperagg_vs_single = (HA - single) / single × 100%`
- `p95_delta_hyperagg_vs_single        = HA_p95 - single_p95` (lower is better)

| scenario | Δ throughput vs MPTCP † | Δ throughput vs single | Δ p95 latency vs single | fec_overhead obs |
|----------|-------------------------|------------------------|-------------------------|------------------|
| A        | TBD                     | TBD                    | TBD                     | TBD              |
| B        | TBD                     | TBD                    | TBD                     | TBD              |
| C        | TBD                     | TBD                    | TBD                     | TBD              |

## 4. Honest losses — rows where HyperAgg was **worse**

> Populated verbatim from `results.csv`. If HyperAgg underperformed, the
> number goes here with no editorialising.

- _TBD — e.g._
  - Scenario B, p99 latency: HyperAgg 247 ms vs single-path 188 ms
    (+59 ms). Likely FEC group wait + reorder buffer under flapping path2.
- _TBD — add as many as apply, or write "None observed in this run" if so._

## 5. Three most surprising findings

1. **TBD —** one paragraph. Name the metric, the expected value, the
   measured value, and why the gap is surprising.
2. **TBD —** same.
3. **TBD —** same.

## 6. Claim contradictions (cross-reference to `RATAN-HyperAgg-Analysis.md`)

> Read-only pointer to the existing analysis doc. Phase B rewrites it; this
> section just flags where the wire said something different.

- _TBD — e.g._ README.md:3 claims "sub-200ms network switching"; Scenario C
  shows measured switch time (until first post-restore packet delivered)
  of **TBD ms** — populate from artifacts.
- _TBD._

## 7. Architecture risk for a security / SRE reviewer

> One concrete risk surfaced by the measurement.

_TBD — e.g.: The HyperAgg server accepts unauthenticated UDP tunnel
handshakes on 0.0.0.0:9999 (no HMAC until Phase C). Any host on the same
L2 as a `bench-server` could impersonate the client and redirect traffic,
since packet_id space is the only implicit auth. Phase C's
`hyperagg/core/tunnel/auth.py` task closes this._

## 8. PR link

`https://github.com/yx12399/ratan-engine/pull/TBD`
