"""
HyperAgg Main Entry Point — wires all components together.

Modes:
    server    — Run on VPS (Hetzner). Listens for bonded traffic from edge.
    client    — Run on edge node (laptop/Beelink/RPi). Bonds Starlink + cellular.
    demo      — Run dashboard with simulated data (no TUN, no root, no hardware).
                Perfect for UI development and stakeholder demos on any laptop.

Realistic laptop deployment workflow:
    1. Connect Starlink via Ethernet (e.g., eth1 or enp0s20f0u1)
    2. Connect cellular via USB modem (e.g., usb0, wwan0, or enx...)
    3. Run: sudo python -m hyperagg --mode client --vps-host <IP>
       (auto-detects interfaces, or specify: --interfaces eth1 usb0)
    4. Dashboard opens at http://localhost:8080

    On VPS:
    1. Run: sudo python -m hyperagg --mode server
    2. Dashboard at http://<vps-ip>:8080
"""

import argparse
import asyncio
import logging
import os
import random
import re
import signal
import sys
import time

import uvicorn
import yaml

logger = logging.getLogger("hyperagg")

# Path name labels (maps path index to human name for the dashboard)
DEFAULT_PATH_LABELS = {0: "Starlink", 1: "Cellular", 2: "WiFi", 3: "Ethernet"}


def load_config(path: str) -> dict:
    """Load YAML config, substituting ${VAR} with environment variables."""
    with open(path) as f:
        raw = f.read()

    def _sub(match):
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))

    raw = re.sub(r"\$\{(\w+)\}", _sub, raw)
    return yaml.safe_load(raw)


def main():
    parser = argparse.ArgumentParser(
        description="RATAN HyperAgg — Packet-Level Bonding Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # VPS server:
  sudo python -m hyperagg --mode server

  # Edge client (auto-detect interfaces):
  sudo python -m hyperagg --mode client --vps-host 203.0.113.50

  # Edge client (specify interfaces):
  sudo python -m hyperagg --mode client --vps-host 203.0.113.50 --interfaces eth1 usb0

  # Beelink as router (laptop connects to Beelink's eth0/WiFi, Beelink bonds Starlink+cellular):
  sudo python -m hyperagg --mode client --vps-host 203.0.113.50 --interfaces eth1 usb0 --lan-interface eth0

  # Demo mode (no hardware, no root):
  python -m hyperagg --mode demo

  # Demo mode with custom port:
  python -m hyperagg --mode demo --api-port 9090
""",
    )
    parser.add_argument(
        "--mode",
        choices=["server", "client", "demo"],
        required=True,
        help="server=VPS, client=edge node, demo=simulated dashboard",
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--api-port", type=int, default=8080, help="Dashboard/API port")
    parser.add_argument(
        "--no-tun",
        action="store_true",
        help="Disable TUN device (test tunnel without routing)",
    )
    parser.add_argument("--vps-host", default=None, help="VPS IP address")
    parser.add_argument(
        "--interfaces",
        nargs="*",
        default=None,
        help="WAN interface names (e.g., eth1 usb0). Auto-detected if omitted.",
    )
    parser.add_argument(
        "--lan-interface",
        default=None,
        help="LAN interface for Beelink router mode (e.g., eth0). "
        "Laptops connected to this interface route through the tunnel.",
    )
    parser.add_argument(
        "--lan-ip",
        default="192.168.50.1",
        help="IP for LAN interface in router mode (default: 192.168.50.1)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    config = load_config(args.config)
    if args.vps_host:
        config.setdefault("vps", {})["host"] = args.vps_host

    if args.mode == "server":
        asyncio.run(run_server(args, config))
    elif args.mode == "client":
        asyncio.run(run_client(args, config))
    elif args.mode == "demo":
        asyncio.run(run_demo(args, config))


# ── SERVER MODE ──────────────────────────────────────────────────────


async def run_server(args, config: dict) -> None:
    """Run on VPS — listen for edge clients, forward to internet."""
    from hyperagg.tunnel.tun_device import TunDevice
    from hyperagg.tunnel.server import TunnelServer
    from hyperagg.controller.sdn_controller import SDNController
    from hyperagg.dashboard.api import create_dashboard_app
    from hyperagg.telemetry.packet_logger import PacketLogger

    logger.info("Starting HyperAgg in SERVER mode")

    tun = None
    pkt_log = PacketLogger()
    server = TunnelServer(config)
    server.packet_logger = pkt_log
    sdn = SDNController(config, mode="server")
    sdn.set_tunnel(server)

    if not args.no_tun:
        tun = TunDevice(
            name="hagg-srv0", mtu=config.get("tunnel", {}).get("mtu", 1400)
        )
        server_ip = config.get("vps", {}).get("server_ip", "10.99.0.2")
        await tun.open(ip_addr=server_ip, subnet_mask=30)
        server.tun = tun
        logger.info(f"TUN hagg-srv0 at {server_ip}")

    # Dashboard
    app = create_dashboard_app(sdn, pkt_log)

    # Graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda: asyncio.create_task(_shutdown(server, tun, sdn))
        )

    logger.info(f"Dashboard at http://0.0.0.0:{args.api_port}")

    try:
        # Run server + SDN controller + dashboard concurrently
        uvi_config = uvicorn.Config(
            app, host="0.0.0.0", port=args.api_port, log_level="warning"
        )
        uvi_server = uvicorn.Server(uvi_config)

        await asyncio.gather(
            server.start(),
            sdn.start(),
            uvi_server.serve(),
            return_exceptions=True,
        )
    finally:
        if tun:
            await tun.close()


# ── CLIENT MODE ──────────────────────────────────────────────────────


async def run_client(args, config: dict) -> None:
    """Run on edge node (laptop) — bond Starlink + cellular to VPS."""
    from hyperagg.tunnel.tun_device import TunDevice
    from hyperagg.tunnel.client import TunnelClient
    from hyperagg.controller.sdn_controller import SDNController
    from hyperagg.controller.network_manager import NetworkManager
    from hyperagg.dashboard.api import create_dashboard_app
    from hyperagg.telemetry.packet_logger import PacketLogger

    logger.info("Starting HyperAgg in CLIENT mode")

    vps_host = config.get("vps", {}).get("host", "")
    if not vps_host or vps_host.startswith("$"):
        logger.error(
            "VPS host not set. Use --vps-host <IP> or set HYPERAGG_VPS_HOST env var."
        )
        sys.exit(1)

    tun = None
    pkt_log = PacketLogger()
    client = TunnelClient(config)
    client.packet_logger = pkt_log
    sdn = SDNController(config, mode="client")
    sdn.set_tunnel(client)

    if not args.no_tun:
        tun = TunDevice(
            name="hagg0", mtu=config.get("tunnel", {}).get("mtu", 1400)
        )
        client_ip = config.get("vps", {}).get("client_ip", "10.99.0.1")
        await tun.open(ip_addr=client_ip, subnet_mask=30)
        client.tun = tun
        logger.info(f"TUN hagg0 at {client_ip}")

    # Discover and add interfaces
    nm = NetworkManager(config)

    if args.interfaces:
        wan_ifaces = args.interfaces
    else:
        configured = config.get("interfaces", {}).get("wan_interfaces", [])
        if configured:
            wan_ifaces = configured
        else:
            discovered = nm.discover_interfaces()
            wan_ifaces = [i.name for i in discovered]
            logger.info(f"Auto-detected {len(wan_ifaces)} interfaces: {wan_ifaces}")

    if not wan_ifaces:
        logger.error(
            "No WAN interfaces found. Connect Starlink (ethernet) and/or "
            "cellular (USB modem) and retry, or specify with --interfaces."
        )
        sys.exit(1)

    for i, iface_name in enumerate(wan_ifaces):
        info = nm.discover_interface(iface_name)
        label = DEFAULT_PATH_LABELS.get(i, f"Path-{i}")
        if info:
            client.add_path(i, iface_name, local_addr=info.ip_addr)
            logger.info(f"Path {i} ({label}): {iface_name} @ {info.ip_addr}")
        else:
            client.add_path(i, iface_name)
            logger.warning(f"Path {i} ({label}): {iface_name} (no IP yet)")

    # Set up routing to avoid tunnel loops
    if tun and tun.is_open:
        wan_routes = []
        for iface_name in wan_ifaces:
            info = nm.discover_interface(iface_name)
            if info and info.gateway:
                wan_routes.append(
                    {
                        "ip": vps_host,
                        "gateway": info.gateway,
                        "interface": iface_name,
                    }
                )
        await tun.setup_routing(vps_ip=vps_host, wan_routes=wan_routes)

    # Set up LAN gateway (Beelink router mode)
    if args.lan_interface and tun and tun.is_open:
        nm.setup_lan_gateway(
            lan_interface=args.lan_interface,
            lan_ip=args.lan_ip,
            tun_name=tun.name,
        )
        logger.info(
            f"Router mode: laptops on {args.lan_interface} ({args.lan_ip}) "
            f"route through HyperAgg tunnel"
        )

    # Dashboard
    app = create_dashboard_app(sdn, pkt_log)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda: asyncio.create_task(_shutdown(client, tun, sdn))
        )

    dashboard_ip = args.lan_ip if args.lan_interface else "localhost"
    logger.info(f"Dashboard at http://{dashboard_ip}:{args.api_port}")

    try:
        uvi_config = uvicorn.Config(
            app, host="0.0.0.0", port=args.api_port, log_level="warning"
        )
        uvi_server = uvicorn.Server(uvi_config)

        await asyncio.gather(
            client.start(),
            sdn.start(),
            uvi_server.serve(),
            return_exceptions=True,
        )
    finally:
        if tun:
            await tun.close()


# ── DEMO MODE ────────────────────────────────────────────────────────


async def run_demo(args, config: dict) -> None:
    """
    Run dashboard with simulated data — no TUN, no root, no hardware.

    Simulates two paths (Starlink + cellular) with realistic behavior:
    - Starlink: ~30ms RTT, occasional jitter spikes, 1-2% loss
    - Cellular: ~60ms RTT, higher jitter, 3-5% loss
    - Periodic events: path degradation, FEC recovery, mode changes

    Use this for:
    - Dashboard UI development
    - Stakeholder demos on any laptop
    - Testing the full API without hardware
    """
    from hyperagg.controller.sdn_controller import SDNController
    from hyperagg.dashboard.api import create_dashboard_app
    from hyperagg.telemetry.packet_logger import PacketLogger

    logger.info("Starting HyperAgg in DEMO mode (simulated data)")
    logger.info("No TUN device, no root required, no hardware needed")

    sdn = DemoSDNController(config)
    pkt_log = PacketLogger()

    # Start simulated data feed
    sim_task = asyncio.create_task(
        _simulate_data(sdn, pkt_log)
    )

    app = create_dashboard_app(sdn, pkt_log)

    logger.info(f"Dashboard at http://localhost:{args.api_port}")

    uvi_config = uvicorn.Config(
        app, host="0.0.0.0", port=args.api_port, log_level="warning"
    )
    uvi_server = uvicorn.Server(uvi_config)

    try:
        await uvi_server.serve()
    finally:
        sim_task.cancel()


class DemoSDNController:
    """Simulated SDN controller for demo mode."""

    def __init__(self, config: dict):
        self._config = config
        self._start_time = time.monotonic()
        self._events: list[dict] = []
        self._state = {
            "mode": "client",
            "uptime_sec": 0,
            "running": True,
            "paths": {},
            "metrics": {
                "packets_sent_per_path": {0: 0, 1: 0},
                "packets_received": 0,
                "fec_recoveries": 0,
                "duplicates_dropped": 0,
                "reorder_buffer_depth": 0,
                "fec_mode": "xor",
                "fec_overhead_pct": 25.0,
                "scheduler_mode": "ai",
            },
            "scheduler": {
                "mode": "ai",
                "best_path": 0,
                "stats": {
                    "decisions_total": 0,
                    "avg_decision_time_us": 2.4,
                    "decisions_by_reason": {},
                },
            },
            "fec": {
                "current_mode": "xor",
                "mode_setting": "auto",
                "overhead_pct": 25.0,
                "total_recoveries": 0,
                "mode_changes": 0,
            },
            "qos": {
                "realtime": {"ports": "3478-3481", "fec_mode": "replicate"},
                "streaming": {"ports": "5000-5010", "fec_mode": "reed_solomon"},
                "bulk": {"ports": "", "fec_mode": "xor"},
            },
        }

    def get_system_state(self) -> dict:
        self._state["uptime_sec"] = round(time.monotonic() - self._start_time, 1)
        return dict(self._state)

    def get_events(self, last_n: int = 50) -> list[dict]:
        return self._events[-last_n:]

    def set_scheduler_mode(self, mode: str) -> None:
        self._state["scheduler"]["mode"] = mode
        self._state["metrics"]["scheduler_mode"] = mode
        self._emit_event("config", f"Scheduler mode set to: {mode}")

    def set_fec_mode(self, mode: str) -> None:
        self._state["fec"]["current_mode"] = mode
        self._state["metrics"]["fec_mode"] = mode
        self._emit_event("config", f"FEC mode set to: {mode}")

    def force_path(self, path_id: int) -> None:
        self._emit_event("config", f"Forced traffic to path {path_id}")

    def release_path(self) -> None:
        self._emit_event("config", "Released path forcing")

    def _emit_event(self, category: str, message: str) -> None:
        self._events.append(
            {"timestamp": time.time(), "category": category, "message": message}
        )
        if len(self._events) > 500:
            self._events = self._events[-250:]


async def _simulate_data(sdn: DemoSDNController, pkt_log) -> None:
    """
    Generate a realistic demo scenario that tells the HyperAgg story:

    Timeline (repeating every ~90 seconds):
      0-30s:  Steady aggregation — both paths healthy, AI scheduling
      30-45s: Starlink degradation — RTT climbs, loss increases, AI shifts to cellular
      45-55s: Starlink dropout — FEC recovers lost packets, zero app-visible disruption
      55-70s: Recovery — Starlink returns, AI gradually re-integrates it
      70-90s: Steady state again — both paths bonded

    This is the "money shot" for JLR: the dashboard shows instant failover
    while MPTCP would show a 2-10 second disruption.
    """
    from scipy import stats as sp_stats

    seq = 0
    pkt_sent = {0: 0, 1: 0}
    fec_recoveries = 0
    fec_mode = "xor"
    mode_changes = 0
    tick = 0
    decisions_by_reason: dict[str, int] = {}

    sdn._emit_event("system", "HyperAgg started — aggregating on 2 paths")
    sdn._emit_event("system", "AI scheduler active, FEC auto-mode enabled")

    while True:
        try:
            # Determine scenario phase (90-second cycle)
            phase_tick = tick % 90

            if phase_tick < 30:
                # Phase 1: Steady aggregation
                sl_base_rtt, sl_jitter_std, sl_loss_rate = 30, 3, 0.01
                cell_base_rtt, cell_jitter_std, cell_loss_rate = 60, 8, 0.03
                sl_alive = True
                sl_trend = "stable"

                if phase_tick == 0 and tick > 0:
                    sdn._emit_event("system", "Cycle restart — both paths healthy")
                    fec_mode = "xor"

            elif phase_tick < 45:
                # Phase 2: Starlink degrading
                degrade = (phase_tick - 30) / 15.0  # 0.0 → 1.0
                sl_base_rtt = 30 + degrade * 170  # 30ms → 200ms
                sl_jitter_std = 3 + degrade * 40
                sl_loss_rate = 0.01 + degrade * 0.14  # 1% → 15%
                cell_base_rtt, cell_jitter_std, cell_loss_rate = 60, 8, 0.03
                sl_alive = True
                sl_trend = "degrading"

                if phase_tick == 30:
                    sdn._emit_event("path", "Starlink path degrading — RTT climbing")
                    sdn._emit_event("scheduler", "AI shifting traffic to cellular")
                if phase_tick == 38 and fec_mode != "reed_solomon":
                    fec_mode = "reed_solomon"
                    mode_changes += 1
                    sdn._emit_event("fec", "FEC upgraded: xor -> reed_solomon (loss > 5%)")

            elif phase_tick < 55:
                # Phase 3: Starlink dropout — THIS IS THE MONEY SHOT
                sl_base_rtt, sl_jitter_std, sl_loss_rate = 500, 100, 0.80
                cell_base_rtt, cell_jitter_std, cell_loss_rate = 65, 10, 0.03
                sl_alive = phase_tick < 47  # Goes fully dead at tick 47
                sl_trend = "degrading"

                if phase_tick == 45:
                    sdn._emit_event("path", "STARLINK DOWN — path unresponsive")
                    sdn._emit_event("fec", "FEC recovering lost packets from parity")
                    sdn._emit_event("scheduler", "All traffic on cellular — zero app disruption")
                    fec_mode = "replicate"
                    mode_changes += 1

            elif phase_tick < 70:
                # Phase 4: Starlink recovery
                recover = (phase_tick - 55) / 15.0  # 0.0 → 1.0
                sl_base_rtt = 200 - recover * 170  # 200ms → 30ms
                sl_jitter_std = 40 - recover * 37
                sl_loss_rate = 0.15 - recover * 0.14
                cell_base_rtt, cell_jitter_std, cell_loss_rate = 60, 8, 0.03
                sl_alive = True
                sl_trend = "improving"

                if phase_tick == 55:
                    sdn._emit_event("path", "Starlink recovering — probes responding")
                    sdn._emit_event("scheduler", "AI gradually re-integrating Starlink")
                if phase_tick == 62 and fec_mode != "xor":
                    fec_mode = "xor"
                    mode_changes += 1
                    sdn._emit_event("fec", "FEC downgraded: reed_solomon -> xor (loss < 2%)")

            else:
                # Phase 5: Back to steady
                sl_base_rtt, sl_jitter_std, sl_loss_rate = 30, 3, 0.01
                cell_base_rtt, cell_jitter_std, cell_loss_rate = 60, 8, 0.03
                sl_alive = True
                sl_trend = "stable"

            # Generate actual values with noise
            sl_rtt = max(5, sl_base_rtt + random.gauss(0, sl_jitter_std))
            sl_jitter = abs(random.gauss(0, sl_jitter_std))
            cell_rtt = max(10, cell_base_rtt + random.gauss(0, cell_jitter_std))
            cell_jitter = abs(random.gauss(0, cell_jitter_std))

            # Quality scores
            def _score(rtt, loss, jitter):
                return max(0, min(1, 1.0 - (rtt / 150 * 0.2 + loss * 0.5 + jitter / 75 * 0.3)))

            sl_score = _score(sl_rtt, sl_loss_rate, sl_jitter) if sl_alive else 0.0
            cell_score = _score(cell_rtt, cell_loss_rate, cell_jitter)

            # Delivery probability
            sl_delivery = float(sp_stats.norm.cdf(
                150, loc=sl_rtt, scale=max(0.1, sl_jitter)
            )) * (1 - sl_loss_rate) if sl_alive else 0.0
            cell_delivery = float(sp_stats.norm.cdf(
                150, loc=cell_rtt, scale=max(0.1, cell_jitter)
            )) * (1 - cell_loss_rate)

            # Packet distribution based on scores
            total_score = sl_score + cell_score
            sl_share = sl_score / total_score if total_score > 0 else 0.0
            pkts_this_tick = random.randint(80, 120)
            sl_pkts = int(pkts_this_tick * sl_share) if sl_alive else 0
            cell_pkts = pkts_this_tick - sl_pkts
            pkt_sent[0] += sl_pkts
            pkt_sent[1] += cell_pkts

            # FEC recovery simulation (more during degradation)
            fec_recover_chance = sl_loss_rate * 0.5 if sl_alive else 0.1
            if random.random() < fec_recover_chance:
                fec_recoveries += 1
                pkt_log.log(seq, 0, "recovered", 1380, 0, seq // 5, 0, "FEC recovery")
                if random.random() < 0.3:
                    sdn._emit_event("fec", f"Recovered packet #{seq} from {fec_mode} parity")

            # Scheduler reason
            if not sl_alive or sl_delivery < 0.1:
                reason = "single-path"
            elif sl_delivery > 0.95 and (sl_delivery - cell_delivery) > 0.3:
                reason = "single-path"
            elif sl_delivery > 0.7:
                reason = "split-fec"
            elif sl_delivery > 0.5 and cell_delivery > 0.5:
                reason = "stripe"
            else:
                reason = "degraded"

            decisions_by_reason[reason] = decisions_by_reason.get(reason, 0) + pkts_this_tick

            # Effective loss after FEC (should be ~0 when FEC is working)
            effective_loss = max(0, (sl_loss_rate * sl_share + cell_loss_rate * (1 - sl_share))
                                - (fec_recover_chance * 0.9))

            # Recommended actions
            if sl_delivery > 0.95:
                sl_action = "prefer"
            elif sl_delivery > 0.7:
                sl_action = "use"
            elif sl_delivery > 0.3:
                sl_action = "avoid"
            else:
                sl_action = "abandon"

            cell_action = "prefer" if cell_delivery > 0.95 else "use"

            # Update state
            sdn._state["paths"] = {
                0: {
                    "avg_rtt_ms": round(sl_rtt, 1),
                    "min_rtt_ms": round(max(5, sl_rtt - sl_jitter_std), 1),
                    "max_rtt_ms": round(sl_rtt + sl_jitter_std * 1.5, 1),
                    "jitter_ms": round(sl_jitter, 1),
                    "loss_pct": round(sl_loss_rate * 100, 2),
                    "throughput_mbps": round(sl_pkts * 1380 * 8 / 1_000_000, 1),
                    "is_alive": sl_alive,
                    "quality_score": round(sl_score, 3),
                    "prediction": {
                        "delivery_probability": round(sl_delivery, 3),
                        "trend": sl_trend,
                        "recommended_action": sl_action,
                    },
                },
                1: {
                    "avg_rtt_ms": round(cell_rtt, 1),
                    "min_rtt_ms": round(max(10, cell_rtt - cell_jitter_std), 1),
                    "max_rtt_ms": round(cell_rtt + cell_jitter_std * 1.5, 1),
                    "jitter_ms": round(cell_jitter, 1),
                    "loss_pct": round(cell_loss_rate * 100, 2),
                    "throughput_mbps": round(cell_pkts * 1380 * 8 / 1_000_000, 1),
                    "is_alive": True,
                    "quality_score": round(cell_score, 3),
                    "prediction": {
                        "delivery_probability": round(cell_delivery, 3),
                        "trend": "stable",
                        "recommended_action": cell_action,
                    },
                },
            }

            combined_mbps = round((sl_pkts + cell_pkts) * 1380 * 8 / 1_000_000, 1)
            total_received = pkt_sent[0] + pkt_sent[1]

            sdn._state["metrics"] = {
                "packets_sent_per_path": dict(pkt_sent),
                "packets_received": total_received,
                "fec_recoveries": fec_recoveries,
                "duplicates_dropped": random.randint(0, 3),
                "reorder_buffer_depth": random.randint(0, 8) if phase_tick > 30 else random.randint(0, 2),
                "fec_mode": fec_mode,
                "fec_overhead_pct": {"xor": 25.0, "reed_solomon": 25.0, "replicate": 100.0}.get(fec_mode, 25.0),
                "scheduler_mode": "ai",
                "aggregate_throughput_mbps": combined_mbps,
                "effective_loss_pct": round(effective_loss * 100, 2),
            }

            sdn._state["scheduler"]["best_path"] = 1 if sl_action in ("avoid", "abandon") else 0
            sdn._state["scheduler"]["stats"]["decisions_total"] = total_received
            sdn._state["scheduler"]["stats"]["decisions_by_reason"] = dict(decisions_by_reason)
            sdn._state["fec"]["current_mode"] = fec_mode
            sdn._state["fec"]["total_recoveries"] = fec_recoveries
            sdn._state["fec"]["mode_changes"] = mode_changes
            sdn._state["fec"]["overhead_pct"] = sdn._state["metrics"]["fec_overhead_pct"]

            # Log packets for the live stream
            for _ in range(min(5, sl_pkts)):
                pkt_log.log(seq, 0, "data", 1380, sl_rtt, seq // 5, seq % 5, reason)
                seq += 1
            for _ in range(min(5, cell_pkts)):
                pkt_log.log(seq, 1, "data", 1380, cell_rtt, seq // 5, seq % 5, reason)
                seq += 1
            if random.random() < 0.3:
                pkt_log.log(seq, 1 if sl_share < 0.3 else 0, "fec_parity", 1380, 0, seq // 5, 4, "")
                seq += 1

            tick += 1
            await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Simulation error: {e}")
            await asyncio.sleep(1.0)


# ── SHUTDOWN ─────────────────────────────────────────────────────────


async def _shutdown(tunnel, tun, sdn=None) -> None:
    """Graceful shutdown handler."""
    logger.info("Shutting down...")
    if sdn:
        await sdn.stop()
    await tunnel.stop()
    if tun:
        await tun.close()
    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            task.cancel()


if __name__ == "__main__":
    main()
