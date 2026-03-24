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
from api.control_plane import create_app
from api.bridge import router as bridge_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/engine.log", mode="a"),
    ],
)
logger = logging.getLogger("ratan")


def main():
    parser = argparse.ArgumentParser(description="RATAN Engine")
    parser.add_argument("--mode", choices=["server", "client", "api-only"], default="server")
    parser.add_argument("--config", default="config/defaults.json")
    parser.add_argument("--api-port", type=int, default=8080)
    parser.add_argument("--data-port", type=int, default=9000)
    parser.add_argument("--probe-port", type=int, default=9001)
    parser.add_argument("--bind", default="0.0.0.0")

    # Client-mode args
    parser.add_argument("--server-addr", default="89.167.91.132")
    parser.add_argument("--client-id", default="edge-01")
    parser.add_argument("--starlink-interface", default="wlan0")
    parser.add_argument("--cellular-interface", default="wwan0")

    args = parser.parse_args()

    # Ensure log directory exists
    os.makedirs("logs", exist_ok=True)

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

    tunnel = TunnelServer(
        config, health, balancer,
        bind_addr=args.bind,
        data_port=args.data_port,
        probe_port=args.probe_port,
    )

    # Create FastAPI app
    app = create_app(config, health, balancer, metrics, tunnel)
    app.include_router(bridge_router)

    # Start engine components
    health.start()
    balancer.start()
    metrics.start_collection(health, balancer)

    logger.info(f"API on :{args.api_port} | Data on :{args.data_port} | Probes on :{args.probe_port}")

    # Run API and tunnel concurrently
    async def run_all():
        # Start tunnel server in background
        tunnel_task = asyncio.create_task(tunnel.start())

        # Start uvicorn
        uvi_config = uvicorn.Config(app, host=args.bind, port=args.api_port, log_level="info")
        server = uvicorn.Server(uvi_config)
        await server.serve()

    asyncio.run(run_all())


def run_client(args, config, health, balancer, metrics):
    """Run in client mode — tunnel client connecting to VPS."""
    logger.info("Starting RATAN Engine in CLIENT mode")

    client = TunnelClient(
        config, health, balancer,
        client_id=args.client_id,
        server_addr=args.server_addr,
        server_data_port=args.data_port,
        server_probe_port=args.probe_port,
    )

    health.start()
    balancer.start()
    metrics.start_collection(health, balancer)

    async def run_client_async():
        # Register interfaces
        await client.add_interface("starlink", args.starlink_interface)
        await client.add_interface("cellular", args.cellular_interface)

        # Start probing
        await client.start_probing()

    asyncio.run(run_client_async())


def run_api_only(args, config, health, balancer, metrics):
    """Run only the API — for development and testing."""
    logger.info("Starting RATAN Engine in API-ONLY mode")

    app = create_app(config, health, balancer, metrics)
    app.include_router(bridge_router)

    # Register simulated paths for testing
    health.register_path("starlink", "sim-starlink", "127.0.0.1", args.probe_port)
    health.register_path("cellular", "sim-cellular", "127.0.0.1", args.probe_port)

    # Simulate some health data
    import threading
    import time
    import random

    def simulate_health():
        """Generate simulated health data for dashboard development."""
        time.sleep(2)
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
