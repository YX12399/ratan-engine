# TODO_NEXT_SESSION

Items deliberately deferred from Phase A. Each should be picked up only
AFTER all three phases (A prove, B honest docs, C split+secure) have
merged. No feature work until then.

## Queued from Phase A (bench harness)

- **A.1 — Re-run bench with TUN enabled.** The current harness invokes
  the HyperAgg client with `--no-tun`, so traffic hits `bench-server` over
  raw UDP rather than through the TUN device. `CAP_NET_ADMIN` inside the
  compose containers should allow TUN; repeat scenarios A/B/C with
  `--no-tun` removed and compare results.csv rows side-by-side.
- **A.2 — Real Starlink + cellular field measurements.** Single-host netem
  does not reproduce real carrier jitter fingerprints. Capture a few
  weeks of field data from an actual Beelink edge + Starlink dish + 5G
  dongle pair; re-emit the same results.csv schema.
- **A.3 — MPTCP UDP-shim experiment (optional).** If a TCP-vs-UDP
  comparison is deemed insufficient, evaluate a KCP or QUIC tunnel over
  MPTCP so the comparison is same-transport. Weigh this against the
  shim-artifact risk noted in `bench/REPORT.md`.
- **A.4 — CI integration.** Run a nightly one-scenario bench on a
  dedicated runner and post the delta vs the committed `results.csv` as a
  commit status. Intentionally deferred — no new CI until Phase C lands.

## Queued from Phase A (future phases, do not start yet)

- **Phase B — rewrite `RATAN-HyperAgg-Analysis.md`** using only numbers
  from `bench/results.csv`. Delete the "Where HyperAgg now EXCEEDS both
  OMR and HyperPath" section unless every row has a measurement; any
  unmeasured row becomes "Not measured" or is removed. Add
  `hyperagg/DESIGN.md` covering, for each module: algorithm, one
  alternative rejected and why, one known failure mode, and the
  real-world condition that validates the choice.
- **Phase C — split + secure.** `git mv` existing modules into
  `hyperagg/core/` (tunnel, fec, scheduler, telemetry, metrics) and
  `hyperagg/control/` (dashboard, agent, controller, testing, ai). Add
  `hyperagg/core/tunnel/auth.py` with HMAC-SHA256 shared-secret
  handshake; unauthenticated clients rejected with an audit log line;
  secret in `/etc/hyperagg/secret` (0600, root). Document rotation in
  `docs/SECURITY.md`. All 198 existing tests must still pass; add ≥5
  auth tests (valid, invalid, replay, expired, missing).

## Not Phase A/B/C — deliberately frozen

- No new QoS classes.
- No dashboard redesign.
- No new AI features.
- No JLR story-event changes.
- No UI "polish."
