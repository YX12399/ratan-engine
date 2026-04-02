"""
HyperAgg Main Entry Point — wires all components together.

Usage:
    python -m hyperagg --mode server --config config.yaml
    python -m hyperagg --mode client --config config.yaml
"""

import argparse
import asyncio
import base64
import logging
import os
import signal
import sys

import yaml

from hyperagg.tunnel.tun_device import TunDevice
from hyperagg.tunnel.client import TunnelClient
from hyperagg.tunnel.server import TunnelServer
from hyperagg.controller.network_manager import NetworkManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("hyperagg")


def load_config(path: str) -> dict:
    """Load YAML config, substituting environment variables."""
    with open(path) as f:
        raw = f.read()

    # Substitute ${VAR} patterns with env vars
    import re
    def _sub(match):
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))
    raw = re.sub(r'\$\{(\w+)\}', _sub, raw)

    return yaml.safe_load(raw)


def main():
    parser = argparse.ArgumentParser(description="HyperAgg Bonding Engine")
    parser.add_argument("--mode", choices=["server", "client"], required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--no-tun", action="store_true",
                        help="Disable TUN device (for testing without root)")
    parser.add_argument("--vps-host", default=None,
                        help="Override VPS host address")
    parser.add_argument("--interfaces", nargs="*", default=None,
                        help="WAN interfaces to use (e.g., eth1 wwan0)")

    args = parser.parse_args()

    config = load_config(args.config)

    if args.vps_host:
        config.setdefault("vps", {})["host"] = args.vps_host

    if args.mode == "server":
        asyncio.run(run_server(args, config))
    elif args.mode == "client":
        asyncio.run(run_client(args, config))


async def run_server(args, config: dict) -> None:
    """Run in server mode — listen for edge clients."""
    logger.info("Starting HyperAgg in SERVER mode")

    tun = None
    server = TunnelServer(config)

    if not args.no_tun:
        tun = TunDevice(name="hagg-srv0", mtu=config.get("tunnel", {}).get("mtu", 1400))
        server_ip = config.get("vps", {}).get("server_ip", "10.99.0.2")
        await tun.open(ip_addr=server_ip, subnet_mask=30)
        server.tun = tun
        logger.info(f"TUN device hagg-srv0 ready at {server_ip}")

    # Graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(server, tun)))

    try:
        await server.start()
    finally:
        if tun:
            await tun.close()


async def run_client(args, config: dict) -> None:
    """Run in client mode — connect to VPS."""
    logger.info("Starting HyperAgg in CLIENT mode")

    vps_host = config.get("vps", {}).get("host", "")
    if not vps_host or vps_host.startswith("$"):
        logger.error("VPS host not configured. Set HYPERAGG_VPS_HOST or edit config.yaml")
        sys.exit(1)

    tun = None
    client = TunnelClient(config)

    if not args.no_tun:
        tun = TunDevice(name="hagg0", mtu=config.get("tunnel", {}).get("mtu", 1400))
        client_ip = config.get("vps", {}).get("client_ip", "10.99.0.1")
        await tun.open(ip_addr=client_ip, subnet_mask=30)
        client.tun = tun
        logger.info(f"TUN device hagg0 ready at {client_ip}")

    # Discover and add interfaces
    nm = NetworkManager(config)

    if args.interfaces:
        wan_ifaces = args.interfaces
    else:
        configured = config.get("interfaces", {}).get("wan_interfaces", [])
        if configured:
            wan_ifaces = configured
        else:
            # Auto-detect
            discovered = nm.discover_interfaces()
            wan_ifaces = [i.name for i in discovered]
            logger.info(f"Auto-detected {len(wan_ifaces)} interfaces: {wan_ifaces}")

    for i, iface_name in enumerate(wan_ifaces):
        info = nm.discover_interface(iface_name)
        if info:
            client.add_path(i, iface_name, local_addr=info.ip_addr)
            logger.info(f"Path {i}: {iface_name} ({info.ip_addr})")
        else:
            client.add_path(i, iface_name)
            logger.warning(f"Path {i}: {iface_name} (no IP found)")

    if not client._sockets:
        logger.error("No network interfaces available")
        sys.exit(1)

    # Set up routing
    if tun and tun.is_open:
        wan_routes = []
        for iface_name in wan_ifaces:
            info = nm.discover_interface(iface_name)
            if info and info.gateway:
                wan_routes.append({
                    "ip": vps_host,
                    "gateway": info.gateway,
                    "interface": iface_name,
                })
        await tun.setup_routing(vps_ip=vps_host, wan_routes=wan_routes)

    # Graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(client, tun)))

    try:
        await client.start()
    finally:
        if tun:
            await tun.close()


async def _shutdown(tunnel, tun) -> None:
    """Graceful shutdown handler."""
    logger.info("Shutting down...")
    await tunnel.stop()
    if tun:
        await tun.close()
    # Cancel all running tasks
    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            task.cancel()


if __name__ == "__main__":
    main()
