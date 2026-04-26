#!/usr/bin/env python3
"""Synthetic Teams-like UDP flow: 48-byte payload @ 50 pps bidirectional.

Runs as two roles:
  server — echoes every received packet back to the sender
  client — sends 48B datagrams @ 50 pps, records rx timestamps, emits JSONL

The client measures application-level RTT end-to-end: the sender stamps
the send time in the payload (monotonic_ns, big-endian), the server bounces
the packet verbatim, and the client diff's rx-time minus the stamp. Output
is one JSONL record per returned packet; scrape/percentiles.py turns that
into p50/p95/p99. No third-party deps; stdlib only.

Usage:
    teams_flow.py server --bind 0.0.0.0:5000
    teams_flow.py client --peer 10.99.0.2:5000 --duration 60 --out flow.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import struct
import sys
import time
from pathlib import Path

PAYLOAD_LEN = 48
STAMP_FMT = "!Q"  # 8-byte monotonic_ns
STAMP_LEN = struct.calcsize(STAMP_FMT)
FILLER = b"\x00" * (PAYLOAD_LEN - STAMP_LEN)


async def server(bind: str) -> None:
    host, port = bind.rsplit(":", 1)
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.bind((host, int(port)))
    print(f"[teams-server] listening on {host}:{port}", file=sys.stderr)
    while True:
        data, addr = await loop.sock_recvfrom(sock, 2048)
        await loop.sock_sendto(sock, data, addr)


async def client(peer: str, duration: int, out_path: Path) -> None:
    host, port = peer.rsplit(":", 1)
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.connect((host, int(port)))

    sent = 0
    received = 0
    deadline = time.monotonic() + duration
    interval = 1.0 / 50  # 50 pps

    out_fh = out_path.open("w", buffering=1)  # line-buffered
    try:
        async def receiver() -> None:
            nonlocal received
            while True:
                data = await loop.sock_recv(sock, 2048)
                if len(data) < STAMP_LEN:
                    continue
                rx_ns = time.monotonic_ns()
                (tx_ns,) = struct.unpack(STAMP_FMT, data[:STAMP_LEN])
                rtt_ms = (rx_ns - tx_ns) / 1e6
                out_fh.write(json.dumps({"rtt_ms": rtt_ms, "tx_ns": tx_ns, "rx_ns": rx_ns}) + "\n")
                received += 1

        rx_task = asyncio.create_task(receiver())

        seq = 0
        while time.monotonic() < deadline:
            stamp = struct.pack(STAMP_FMT, time.monotonic_ns())
            await loop.sock_sendall(sock, stamp + FILLER)
            sent += 1
            seq += 1
            await asyncio.sleep(interval)

        # Drain any in-flight returns for up to 2 s.
        try:
            await asyncio.wait_for(asyncio.sleep(2), timeout=2)
        except asyncio.TimeoutError:
            pass
        rx_task.cancel()
    finally:
        out_fh.close()
        sock.close()

    loss_pct = 0.0 if sent == 0 else 100.0 * (sent - received) / sent
    summary = {"sent": sent, "received": received, "loss_pct": round(loss_pct, 3)}
    print(json.dumps(summary))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="role", required=True)

    sp_srv = sub.add_parser("server")
    sp_srv.add_argument("--bind", default="0.0.0.0:5000")

    sp_cli = sub.add_parser("client")
    sp_cli.add_argument("--peer", required=True)
    sp_cli.add_argument("--duration", type=int, default=60)
    sp_cli.add_argument("--out", type=Path, default=Path("flow.jsonl"))

    args = ap.parse_args()
    if args.role == "server":
        asyncio.run(server(args.bind))
    else:
        asyncio.run(client(args.peer, args.duration, args.out))


if __name__ == "__main__":
    main()
