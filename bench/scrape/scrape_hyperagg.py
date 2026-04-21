#!/usr/bin/env python3
"""Poll the HyperAgg client's /api/state at 1 Hz during a run and emit a
per-second JSONL trace. Separately fetch /api/packets/csv at end-of-run
for the authoritative RTT distribution.

The client dashboard API listens on 8080 by default (config.yaml
`server.port`). The scraper tolerates any subset of the state tree being
missing so it works whether the run is 'client' or 'server' mode.

Usage:
    scrape_hyperagg.py --url http://localhost:8080 \
        --out /tmp/hyperagg_trace.jsonl \
        --duration 360 \
        --packets-csv /tmp/hyperagg_packets.csv
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import aiohttp


async def poll(session: aiohttp.ClientSession, url: str, out: Path, duration: int) -> None:
    end = time.monotonic() + duration
    with out.open("w", buffering=1) as fh:
        while time.monotonic() < end:
            tick_start = time.monotonic()
            record: dict = {"ts": time.time()}
            try:
                async with session.get(f"{url}/api/state", timeout=aiohttp.ClientTimeout(total=2)) as r:
                    record["state"] = await r.json()
            except Exception as exc:
                record["error"] = repr(exc)
            fh.write(json.dumps(record) + "\n")
            # Sleep to maintain 1 Hz
            await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - tick_start)))


async def fetch_packets_csv(session: aiohttp.ClientSession, url: str, out: Path) -> None:
    try:
        async with session.get(f"{url}/api/packets/csv", timeout=aiohttp.ClientTimeout(total=10)) as r:
            text = await r.text()
        out.write_text(text)
        print(f"[scrape] wrote {out} ({len(text)} bytes)", file=sys.stderr)
    except Exception as exc:
        print(f"[scrape] WARN could not fetch packets csv: {exc}", file=sys.stderr)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--out", type=Path, default=Path("hyperagg_trace.jsonl"))
    ap.add_argument("--duration", type=int, default=360)
    ap.add_argument("--packets-csv", type=Path, default=None)
    args = ap.parse_args()

    async with aiohttp.ClientSession() as session:
        await poll(session, args.url, args.out, args.duration)
        if args.packets_csv is not None:
            await fetch_packets_csv(session, args.url, args.packets_csv)


if __name__ == "__main__":
    asyncio.run(main())
