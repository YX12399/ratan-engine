"""
RATAN Engine — Main entry point.
Starts the tunnel server, health monitor, path balancer, metrics collector,
and control plane API.

Usage:
    python main.py                    # Start server mode (VPS)
    python main.py --mode client      # Start client mode (edge node)
    python main.py --mode api-only    # Start only the API (for development)
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import uvicorn
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from engine.config_store import ConfigStore
from engine.health_monitor import HealthMonitor
from engine.path_balancer import PathBalancer
from engine.metrics_collector import MetricsCollector
from engine.tunnel_server import TunnelServer
from engine.tunnel_client import TunnelClient
from engine.tun_device import TunDevice
from engine.nat_manager import NatManager
from engine.interface_manager import InterfaceManager
from engine.ai.claude_optimizer import ClaudeOptimizer
from api.control_plane import create_app
from api.bridge import router as bridge_router
from serve_dashboard import add_dashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.environ.get("RATAN_LOG_FILE", "logs/engine.log"), mode="a"),
    ],
)
logger = logging.getLogger("ratan")


def main():
    parser = argparse.ArgumentParser(description="RATAN Engine")
    parser.add_argument("--mode", choices=["server", "client", "api-only"], default="server")
    parser.add_argument("--config", default=os.environ.get("RATAN_CONFIG_PATH", "config/defaults.json"))
    parser.add_argument("--api-port", type=int, default=int(os.environ.get("RATAN_API_PORT", "8080")))
    parser.add_argument("--data-port", type=int, default=int(os.environ.get("RATAN_DATA_PORT", "9000")))
    parser.add_argument("--probe-port", type=int, default=int(os.environ.get("RATAN_PROBE_PORT", "9001")))
    parser.add_argument("--bind", default=os.environ.get("RATAN_BIND_ADDR", "0.0.0.0"))

    # Client-mode args
    parser.add_argument("--server-addr", default=os.environ.get("RATAN_SERVER_ADDR", ""))
    parser.add_argument("--client-id", default=os.environ.get("RATAN_CLIENT_ID", "edge-01"))
    parser.add_argument("--starlink-interface", default=os.environ.get("RATAN_STARLINK_IFACE", "wlan0"))
    parser.add_argument("--cellular-interface", default=os.environ.get("RATAN_CELLULAR_IFACE", "wwan0"))

    # Data plane args
    parser.add_argument("--no-tun", action="store_true", help="Disable TUN device (for testing without root)")

    args = parser.parse_args()

    # Ensure log directory exists
    log_file = os.environ.get("RATAN_LOG_FILE", "logs/engine.log")
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)

    # Initialize core components
    config = ConfigStore(args.config)
    health = HealthMonitor(config)
    balancer = PathBalancer(config, health)
    metrics = MetricsCollector(config)

    if args.mode == "server":
        run_server(args, config, health, balancer, metrics)
    elif args.mode == "client":
        run_client(args, config, health, balancer, metrics)
    elif args.mode == "api-only":
        run_api_only(args, config, health, balancer, metrics)


def run_server(args, config, health, balancer, metrics):
    """Run in server mode — tunnel server + API."""
    logger.info("Starting RATAN Engine in SERVER mode")

    tun = None
    nat = NatManager()

    async def run_all():
        nonlocal tun

        # Set up TUN device for data plane
        if not args.no_tun:
            dp = config.get("data_plane", {})
            tun = TunDevice(
                name=dp.get("tun_name_server", "ratan-srv0"),
                mtu=dp.get("tun_mtu", 1400),
            )
            await tun.open(
                ip_addr=dp.get("server_ip", "10.42.0.1"),
                subnet_mask=int(dp.get("internal_subnet", "10.42.0.0/24").split("/")[1]),
            )
            if dp.get("enable_nat", True):
                nat.setup_server_nat(
                    tun.name,
                    dp.get("internal_subnet", "10.42.0.0/24"),
                )
            logger.info("Data plane ready — TUN + NAT configured")

        tunnel = TunnelServer(
            config, health, balancer,
            bind_addr=args.bind,
            data_port=args.data_port,
            probe_port=args.probe_port,
            tun_device=tun,
        )

        # AI optimizer
        optimizer = ClaudeOptimizer(config, health, balancer, metrics)

        # Create FastAPI app
        app = create_app(config, health, balancer, metrics, tunnel, ai_optimizer=optimizer)
        app.include_router(bridge_router)
        add_dashboard(app)

        # Start engine components
        health.start()
        balancer.start()
        metrics.start_collection(health, balancer)

        logger.info(f"API on :{args.api_port} | Data on :{args.data_port} | Probes on :{args.probe_port}")

        try:
            # Start tunnel server in background
            tunnel_task = asyncio.create_task(tunnel.start())

            # Start AI optimizer
            await optimizer.start()

            # Start uvicorn
            uvi_config = uvicorn.Config(app, host=args.bind, port=args.api_port, log_level="info")
            server = uvicorn.Server(uvi_config)
            await server.serve()
        finally:
            await optimizer.stop()
            if tun:
                await tun.close()
            nat.cleanup()

    asyncio.run(run_all())


def run_client(args, config, health, balancer, metrics):
    """Run in client mode — tunnel client connecting to VPS."""
    logger.info("Starting RATAN Engine in CLIENT mode")

    tun = None
    nat = NatManager()
    iface_mgr = InterfaceManager(config)

    async def run_client_async():
        nonlocal tun

        # Set up TUN device for data plane
        if not args.no_tun:
            dp = config.get("data_plane", {})
            tun = TunDevice(
                name=dp.get("tun_name_client", "ratan0"),
                mtu=dp.get("tun_mtu", 1400),
            )
            await tun.open(
                ip_addr=dp.get("client_ip", "10.42.0.2"),
                subnet_mask=int(dp.get("internal_subnet", "10.42.0.0/24").split("/")[1]),
            )
            logger.info("Client TUN device ready")

        client = TunnelClient(
            config, health, balancer,
            client_id=args.client_id,
            server_addr=args.server_addr,
            server_data_port=args.data_port,
            server_probe_port=args.probe_port,
            tun_device=tun,
        )

        health.start()
        balancer.start()
        metrics.start_collection(health, balancer)

        # Discover interfaces and set up policy routing
        interfaces = {
            "starlink": args.starlink_interface,
            "cellular": args.cellular_interface,
        }

        for path_id, iface_name in interfaces.items():
            info = iface_mgr.discover_interface(path_id, iface_name)
            if info:
                iface_mgr.setup_policy_routing(path_id)
                source_addr = iface_mgr.get_source_addr(path_id)
                await client.add_interface(path_id, iface_name, local_addr=source_addr)
            else:
                logger.warning(f"Interface {iface_name} not available, registering without binding")
                await client.add_interface(path_id, iface_name)

        # Set up client routes (TUN as default, preserve VPS route)
        if tun and not args.no_tun:
            # Find the gateway for VPS from whichever interface can reach it
            for path_id, info_dict in iface_mgr.get_all_interfaces().items():
                gw = info_dict.get("gateway", "")
                if gw:
                    nat.setup_client_routes(tun.name, args.server_addr, gw)
                    break

        # Start interface monitoring
        iface_mgr.start_monitoring(health)

        try:
            # Start all: traffic loop + probe loops + receive loops
            if tun and tun.is_open:
                await client.start_all()
            else:
                await client.start_probing()
        finally:
            if tun:
                await tun.close()
            nat.cleanup()
            iface_mgr.cleanup()

    asyncio.run(run_client_async())


def run_api_only(args, config, health, balancer, metrics):
    """Run only the API — for development and testing."""
    logger.info("Starting RATAN Engine in API-ONLY mode")

    app = create_app(config, health, balancer, metrics)
    app.include_router(bridge_router)
    add_dashboard(app)

    # Register simulated paths for testing
    health.register_path("starlink", "sim-starlink", "127.0.0.1", args.probe_port)
    health.register_path("cellular", "sim-cellular", "127.0.0.1", args.probe_port)

    # Simulate some health data
    import threading
    import time
    import random

    def simulate_health():
        """Generate simulated health data for dashboard development."""
        time.sleep(config.get("test_orchestration.startup_delay_sec", 2))
        while True:
            # Starlink — generally good, occasional jitter
            sl_rtt = random.gauss(25, 5)
            sl_success = random.random() > 0.02
            health.record_probe("starlink", sl_rtt if sl_success else None, sl_success)

            # Cellular — higher RTT, more variable
            cell_rtt = random.gauss(60, 15)
            cell_success = random.random() > 0.05
            health.record_probe("cellular", cell_rtt if cell_success else None, cell_success)

            time.sleep(config.get("path_health.probe_interval_ms", 50) / 1000)

    health.start()
    balancer.start()
    metrics.start_collection(health, balancer)

    sim_thread = threading.Thread(target=simulate_health, daemon=True)
    sim_thread.start()

    uvicorn.run(app, host=args.bind, port=args.api_port, log_level="info")


if __name__ == "__main__":
    main()
