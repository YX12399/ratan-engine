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
    parser.add_argument(
        "--log-file",
        default=None,
        help="Write logs to file (e.g., /var/log/hyperagg.log)",
    )

    args = parser.parse_args()

    handlers = [logging.StreamHandler()]
    if args.log_file:
        os.makedirs(os.path.dirname(args.log_file) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file))

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=handlers,
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
    from hyperagg.ai.chat_handler import ChatHandler
    from hyperagg.testing.test_runner import TestRunner

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

    # AI + Testing + Device Registry
    ai_handler = ChatHandler(sdn)
    test_runner = TestRunner(sdn)
    from hyperagg.dashboard.agent_ws import DeviceRegistry
    device_registry = DeviceRegistry()

    # Dashboard (with device management)
    app = create_dashboard_app(
        sdn, pkt_log, ai_handler, test_runner,
        device_registry=device_registry,
    )

    # Graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig, lambda: asyncio.create_task(_shutdown(server, tun, sdn))
        )

    logger.info(f"Dashboard at http://0.0.0.0:{args.api_port}")
    logger.info("Edge agents can connect to ws://0.0.0.0:{args.api_port}/ws/agent")

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
    from hyperagg.ai.chat_handler import ChatHandler
    from hyperagg.testing.test_runner import TestRunner

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

    # Discover and add WAN interfaces
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

    # FIX: Remove LAN interface from WAN paths (prevents routing loop)
    if args.lan_interface and args.lan_interface in wan_ifaces:
        wan_ifaces.remove(args.lan_interface)
        logger.info(f"Excluded LAN interface {args.lan_interface} from WAN paths")

    # FIX: Retry interface detection for late USB tethers
    if not wan_ifaces:
        logger.info("No WAN interfaces yet — waiting for USB tether (retrying 3x)...")
        for retry in range(3):
            await asyncio.sleep(5)
            discovered = nm.discover_interfaces()
            wan_ifaces = [
                i.name for i in discovered
                if i.name != args.lan_interface
            ]
            if wan_ifaces:
                logger.info(f"Found interfaces on retry {retry+1}: {wan_ifaces}")
                break

    if not wan_ifaces:
        logger.error(
            "No WAN interfaces found. Connect Starlink (ethernet) and/or "
            "cellular (USB modem) and retry, or specify with --interfaces."
        )
        sys.exit(1)

    # Startup diagnostic
    print(f"\n{'='*50}")
    print(f"  HyperAgg Client")
    print(f"{'='*50}")
    print(f"  VPS: {vps_host}:{config.get('vps', {}).get('tunnel_port', 9999)}")
    if tun:
        print(f"  TUN: {tun.name} ({config.get('vps', {}).get('client_ip', '10.99.0.1')})  OK")
    else:
        print(f"  TUN: disabled (--no-tun)")
    crypto_ok = bool(config.get("vps", {}).get("encryption_key", "")) and not config.get("vps", {}).get("encryption_key", "").startswith("$")
    print(f"  Encryption: {'enabled' if crypto_ok else 'DISABLED'}")

    for i, iface_name in enumerate(wan_ifaces):
        info = nm.discover_interface(iface_name)
        label = DEFAULT_PATH_LABELS.get(i, f"Path-{i}")
        if info:
            client.add_path(i, iface_name, local_addr=info.ip_addr)
            print(f"  Path {i} ({label}): {iface_name} @ {info.ip_addr}  OK")
        else:
            client.add_path(i, iface_name)
            print(f"  Path {i} ({label}): {iface_name}  NO IP")

    print(f"  Connecting...")
    print(f"{'='*50}\n")

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
        lan_subnet = f"{args.lan_ip.rsplit('.', 1)[0]}.0/24" if args.lan_interface else None
        await tun.setup_routing(
            vps_ip=vps_host, wan_routes=wan_routes, lan_subnet=lan_subnet
        )

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
    # AI + Testing
    ai_handler = ChatHandler(sdn)
    test_runner = TestRunner(sdn)

    app = create_dashboard_app(sdn, pkt_log, ai_handler, test_runner)

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
    from hyperagg.ai.chat_handler import ChatHandler
    from hyperagg.testing.test_runner import TestRunner
    from hyperagg.scheduler.path_monitor import PathMonitor
    from hyperagg.scheduler.path_predictor import PathPredictor
    from hyperagg.scheduler.path_scheduler import PathScheduler
    from hyperagg.fec.fec_engine import FecEngine
    from hyperagg.tunnel.packet import set_connection_start
    from hyperagg.tunnel.reorder_buffer import AdaptiveReorderBuffer
    from hyperagg.scheduler.bandwidth_estimator import BandwidthEstimator, TokenBucketPacer
    from hyperagg.scheduler.congestion_control import AIMDController, TierBandwidthManager
    from hyperagg.scheduler.owd_estimator import OWDEstimator
    from hyperagg.metrics.throughput_calculator import ThroughputCalculator
    from hyperagg.tunnel.session import SessionManager

    logger.info("Starting HyperAgg in DEMO mode")
    logger.info("Using REAL FEC + AI scheduler + all 7 networking modules")
    set_connection_start()

    # Build ALL real engine components (same as TunnelClient uses)
    monitor = PathMonitor(config)
    monitor.register_path(0)
    monitor.register_path(1)
    predictor = PathPredictor(config)
    fec = FecEngine(config)
    scheduler = PathScheduler(config, monitor, predictor)
    pkt_log = PacketLogger()

    # 7 networking modules
    reorder = AdaptiveReorderBuffer(
        window_size=config.get("tunnel", {}).get("sequence_window", 1024),
        initial_timeout_ms=config.get("tunnel", {}).get("reorder_timeout_ms", 100),
    )
    bw_estimator = BandwidthEstimator(ewma_alpha=0.2, window_sec=1.0)
    pacer = TokenBucketPacer()
    pacer.set_rate(0, 5_000_000)  # 40 Mbps Starlink
    pacer.set_rate(1, 2_000_000)  # 15 Mbps cellular
    congestion = {
        0: AIMDController(estimated_bw=5_000_000),
        1: AIMDController(estimated_bw=2_000_000),
    }
    tier_mgr = TierBandwidthManager(total_capacity_bytes_per_sec=7_000_000)
    owd = OWDEstimator(ewma_alpha=0.1)
    throughput_calc = ThroughputCalculator(window_sec=5.0)
    session_mgr = SessionManager(
        state_dir="/tmp/hyperagg_demo_sessions",
        save_interval_sec=5.0,
    )
    session_mgr.create_session()

    sdn = RealDemoController(
        config, monitor, predictor, scheduler, fec,
        reorder, bw_estimator, pacer, congestion,
        tier_mgr, owd, throughput_calc, session_mgr,
    )
    ai_handler = ChatHandler(sdn)
    test_runner = TestRunner(sdn)

    # Start the simulation that feeds real measurements into real components
    sim_task = asyncio.create_task(
        _real_demo_loop(sdn, monitor, predictor, scheduler, fec, pkt_log,
                        reorder, bw_estimator, pacer, congestion,
                        tier_mgr, owd, throughput_calc, session_mgr)
    )

    from hyperagg.testing.preflight import PreflightChecker
    from hyperagg.testing.traffic_gen import TrafficGenerator
    from hyperagg.dashboard.agent_ws import DeviceRegistry
    preflight = PreflightChecker(config)
    traffic_gen = TrafficGenerator()
    device_registry = DeviceRegistry()

    app = create_dashboard_app(
        sdn, pkt_log, ai_handler, test_runner,
        preflight_checker=preflight,
        traffic_generator=traffic_gen,
        device_registry=device_registry,
    )

    logger.info(f"Dashboard at http://localhost:{args.api_port}")

    uvi_config = uvicorn.Config(
        app, host="0.0.0.0", port=args.api_port, log_level="warning"
    )
    uvi_server = uvicorn.Server(uvi_config)

    try:
        await uvi_server.serve()
    finally:
        sim_task.cancel()


class RealDemoController:
    """Demo controller using REAL engine components — all 7 modules."""

    def __init__(self, config, monitor, predictor, scheduler, fec,
                 reorder=None, bw_estimator=None, pacer=None,
                 congestion=None, tier_mgr=None, owd=None,
                 throughput=None, session_mgr=None):
        self._config = config
        self._start_time = time.monotonic()
        self._events: list[dict] = []
        self._monitor = monitor
        self._predictor = predictor
        self._scheduler = scheduler
        self._fec = fec
        # 7 networking modules
        self._reorder = reorder
        self._bw_estimator = bw_estimator
        self._pacer = pacer
        self._congestion = congestion or {}
        self._tier_mgr = tier_mgr
        self._owd = owd
        self._throughput = throughput
        self._session_mgr = session_mgr
        self._metrics = {
            "packets_sent_per_path": {0: 0, 1: 0},
            "packets_received": 0,
            "fec_recoveries": 0,
            "duplicates_dropped": 0,
            "reorder_buffer_depth": 0,
            "aggregate_throughput_mbps": 0,
            "effective_loss_pct": 0,
        }

    def get_system_state(self) -> dict:
        states = self._monitor.get_all_path_states()
        self._scheduler.update_predictions()
        paths = {}
        for pid, s in states.items():
            pred = self._scheduler._predictions.get(pid)
            paths[pid] = {
                "avg_rtt_ms": round(s.avg_rtt_ms, 1),
                "min_rtt_ms": round(s.min_rtt_ms, 1),
                "max_rtt_ms": round(s.max_rtt_ms, 1),
                "jitter_ms": round(s.jitter_ms, 1),
                "loss_pct": round(s.loss_pct * 100, 2),
                "throughput_mbps": round(s.throughput_mbps, 1),
                "is_alive": s.is_alive,
                "quality_score": round(s.quality_score, 3),
                "prediction": {
                    "delivery_probability": round(pred.delivery_probability, 3) if pred else None,
                    "trend": pred.trend if pred else "stable",
                    "recommended_action": pred.recommended_action if pred else "use",
                } if pred else None,
            }

        sched_stats = self._scheduler.get_stats()
        state = {
            "mode": "demo",
            "uptime_sec": round(time.monotonic() - self._start_time, 1),
            "running": True,
            "paths": paths,
            "metrics": {
                **self._metrics,
                "fec_mode": self._fec.current_mode,
                "fec_overhead_pct": round(self._fec.overhead_pct, 1),
                "scheduler_mode": self._scheduler._mode,
            },
            "scheduler": {
                "mode": self._scheduler._mode,
                "best_path": self._scheduler._best_path,
                "stats": {
                    "decisions_total": sched_stats.decisions_total,
                    "avg_decision_time_us": round(sched_stats.avg_decision_time_us, 1),
                    "decisions_by_reason": dict(sched_stats.decisions_by_reason),
                },
            },
            "fec": self._fec.get_stats(),
        }

        # Module stats (all 7 — same keys as SDNController with TunnelClient)
        if self._bw_estimator:
            state["bandwidth"] = {pid: self._bw_estimator.get_estimated_bw_mbps(pid) for pid in paths}
        if self._pacer:
            state["pacer"] = self._pacer.get_stats()
        if self._congestion:
            state["congestion"] = {pid: cc.get_stats() for pid, cc in self._congestion.items()}
        if self._owd:
            state["owd"] = self._owd.get_stats()
        if self._reorder:
            state["reorder"] = self._reorder.get_stats()
        if self._throughput:
            state["throughput"] = self._throughput.compute()
        if self._tier_mgr:
            state["tier_manager"] = self._tier_mgr.get_stats()
        if self._session_mgr:
            state["session"] = self._session_mgr.get_stats()
        state["adaptive_fec"] = self._fec.get_adaptive_stats()

        # Impairment and traffic generator state (for dashboard rendering)
        state["impairment"] = getattr(self, "_impairment_state", {0: {"action": "clear", "detail": "No impairment"}, 1: {"action": "clear", "detail": "No impairment"}})
        state["traffic"] = getattr(self, "_traffic_state", {"running": False, "mode": "", "rate_mbps": 0, "remaining_sec": 0})

        return state

    def get_events(self, last_n: int = 50) -> list[dict]:
        return self._events[-last_n:]

    def set_scheduler_mode(self, mode: str) -> None:
        self._scheduler._mode = mode
        self._scheduler.update_predictions()
        self._emit_event("config", f"Scheduler mode set to: {mode}")

    def set_fec_mode(self, mode: str) -> None:
        self._fec._mode_setting = mode
        if mode != "auto":
            self._fec._current_mode = mode
        self._emit_event("config", f"FEC mode set to: {mode}")

    def force_path(self, path_id: int) -> None:
        self._emit_event("config", f"Forced traffic to path {path_id}")

    def release_path(self) -> None:
        self._scheduler._mode = self._config.get("scheduler", {}).get("mode", "ai")
        self._emit_event("config", "Released path forcing, back to AI mode")

    async def stop(self) -> None:
        pass

    def _emit_event(self, category: str, message: str) -> None:
        self._events.append(
            {"timestamp": time.time(), "category": category, "message": message}
        )
        if len(self._events) > 500:
            self._events = self._events[-250:]
        logger.info(f"[{category}] {message}")


async def _real_demo_loop(sdn, monitor, predictor, scheduler, fec, pkt_log,
                          reorder=None, bw_estimator=None, pacer=None,
                          congestion=None, tier_mgr=None, owd=None,
                          throughput_calc=None, session_mgr=None) -> None:
    """
    Demo loop using REAL engine components.

    Feeds simulated RTT/loss measurements into the real PathMonitor,
    which feeds real PathPredictor, which drives real PathScheduler decisions.
    FEC encode/decode uses real XOR/RS math on simulated packet payloads.

    Timeline (90-second cycle):
      0-30s:  Steady aggregation
      30-45s: Starlink degradation (RTT climbs, loss increases)
      45-55s: Starlink dropout (FEC recovers, scheduler shifts)
      55-70s: Recovery
      70-90s: Steady again
    """
    import os

    global_seq = 0
    tick = 0

    sdn._emit_event("system", "HyperAgg demo — Manchester → M62 → Pennines scenario")
    sdn._emit_event("system", "Real FEC math + AI scheduler + all 7 networking modules active")

    while True:
        try:
            phase_tick = tick % 90

            # --- Scenario phases: determine RTT/loss for each path ---
            if phase_tick < 30:
                sl_rtt = max(5, 30 + random.gauss(0, 3))
                sl_loss = random.random() < 0.01
                cell_rtt = max(10, 60 + random.gauss(0, 8))
                cell_loss = random.random() < 0.03
                if phase_tick == 0 and tick > 0:
                    sdn._emit_event("system", "M62 Highway — both paths strong, full aggregation")
            elif phase_tick < 45:
                d = (phase_tick - 30) / 15.0
                sl_rtt = max(5, (30 + d * 170) + random.gauss(0, 3 + d * 40))
                sl_loss = random.random() < (0.01 + d * 0.14)
                cell_rtt = max(10, 60 + random.gauss(0, 8))
                cell_loss = random.random() < 0.03
                if phase_tick == 30:
                    sdn._emit_event("path", "Approaching Woodhead Tunnel — Starlink signal degrading")
            elif phase_tick < 55:
                sl_rtt = max(5, 500 + random.gauss(0, 100))
                sl_loss = random.random() < 0.80
                cell_rtt = max(10, 65 + random.gauss(0, 10))
                cell_loss = random.random() < 0.03
                if phase_tick == 45:
                    sdn._emit_event("path", "TUNNEL ENTRY — Starlink blocked, cellular-only. FEC covering gap.")
            elif phase_tick < 70:
                r = (phase_tick - 55) / 15.0
                sl_rtt = max(5, (200 - r * 170) + random.gauss(0, 40 - r * 37))
                sl_loss = random.random() < (0.15 - r * 0.14)
                cell_rtt = max(10, 60 + random.gauss(0, 8))
                cell_loss = random.random() < 0.03
                if phase_tick == 55:
                    sdn._emit_event("path", "Tunnel exit — Starlink recovering. AI re-integrating path.")
            else:
                sl_rtt = max(5, 30 + random.gauss(0, 3))
                sl_loss = random.random() < 0.01
                cell_rtt = max(10, 60 + random.gauss(0, 8))
                cell_loss = random.random() < 0.03

            # --- Feed REAL measurements into REAL PathMonitor ---
            if not sl_loss:
                monitor.record_rtt(0, sl_rtt)
            else:
                monitor.record_loss(0)
            if not cell_loss:
                monitor.record_rtt(1, cell_rtt)
            else:
                monitor.record_loss(1)

            # --- REAL scheduler makes decisions ---
            scheduler.update_predictions()

            # Update FEC mode based on real path loss
            states = monitor.get_all_path_states()
            path_loss = {pid: s.loss_pct for pid, s in states.items()}
            old_fec = fec.current_mode
            fec.update_mode(path_loss)
            if fec.current_mode != old_fec:
                sdn._emit_event("fec", f"FEC mode: {old_fec} → {fec.current_mode}")

            # Simulate packet batch — run REAL FEC encode + REAL scheduler
            pkts_this_tick = random.randint(80, 120)
            for _ in range(pkts_this_tick):
                payload = os.urandom(1380)  # Real random payload
                tier = "bulk"

                # REAL FEC encode
                fec_results = fec.encode_packet(payload, tier)

                for fec_payload, fec_meta in fec_results:
                    # REAL scheduler decision
                    decision = scheduler.schedule(global_seq, tier)

                    for assignment in decision.assignments:
                        if not assignment.send:
                            continue
                        pid = assignment.path_id
                        sdn._metrics["packets_sent_per_path"][pid] = (
                            sdn._metrics["packets_sent_per_path"].get(pid, 0) + 1
                        )

                        pkt_log.log(
                            global_seq, pid,
                            "fec_parity" if fec_meta.get("is_parity") else "data",
                            len(fec_payload),
                            rtt_ms=sl_rtt if pid == 0 else cell_rtt,
                            fec_group_id=fec_meta.get("fec_group_id", 0),
                            fec_index=fec_meta.get("fec_index", 0),
                            scheduler_reason=decision.reason,
                        )

                    # REAL FEC decode (simulate receive — drop some based on loss)
                    is_parity = fec_meta.get("is_parity", False)
                    lost = (sl_loss if random.random() < 0.5 else cell_loss)  # Simulated loss
                    if not lost:
                        recovered = fec.decode_packet(
                            fec_meta.get("fec_group_id", 0),
                            fec_meta.get("fec_index", 0),
                            fec_meta.get("fec_group_size", 0),
                            is_parity, fec_payload,
                        )
                        for rec_idx, rec_data in recovered:
                            sdn._metrics["fec_recoveries"] += 1
                            pkt_log.log(global_seq, 0, "recovered", len(rec_data),
                                        fec_group_id=fec_meta.get("fec_group_id", 0))
                            sdn._emit_event("fec", f"Recovered packet from {fec.current_mode} parity")

                    sdn._metrics["packets_received"] += 1

                global_seq += 1

            # Throughput estimate
            sent = sdn._metrics["packets_sent_per_path"]
            total_pkts = sum(sent.values())
            sdn._metrics["aggregate_throughput_mbps"] = round(
                pkts_this_tick * 1380 * 8 / 1_000_000, 1
            )
            sdn._metrics["effective_loss_pct"] = round(
                sum(path_loss.values()) / max(len(path_loss), 1) * 100, 2
            )

            # Expire stale FEC groups
            fec.expire_groups()

            # --- Feed all 7 networking modules with scenario data ---
            states = monitor.get_all_path_states()

            # Module 1: adaptive reorder from path differentials
            if reorder:
                path_rtts = {pid: s.avg_rtt_ms for pid, s in states.items() if s.is_alive}
                path_jitters = {pid: s.jitter_ms for pid, s in states.items() if s.is_alive}
                reorder.update_timeout(path_rtts, path_jitters)

            # Module 2: bandwidth estimation from simulated send volumes
            if bw_estimator:
                sl_bytes = pkts_this_tick * 1380 * 0.6  # ~60% on Starlink
                cell_bytes = pkts_this_tick * 1380 * 0.4
                bw_estimator.record_ack(0, int(sl_bytes))
                bw_estimator.record_ack(1, int(cell_bytes))
                if pacer:
                    for pid in [0, 1]:
                        bw = bw_estimator.get_estimated_bw(pid)
                        if bw > 0:
                            pacer.set_rate(pid, bw)

            # Module 4: AIMD congestion signals
            if congestion:
                if sl_loss:
                    congestion[0].on_loss()
                else:
                    congestion[0].on_success()
                if cell_loss:
                    congestion[1].on_loss()
                else:
                    congestion[1].on_success()
                # Update from BW estimator
                if bw_estimator:
                    for pid, cc in congestion.items():
                        bw = bw_estimator.get_estimated_bw(pid)
                        if bw > 0:
                            cc.update_estimated_bw(bw)

            # Module 4b: tier manager
            if tier_mgr and bw_estimator:
                total_bw = sum(bw_estimator.get_estimated_bw(pid) for pid in [0, 1])
                if total_bw > 0:
                    tier_mgr.update_capacity(total_bw)
                tier_mgr.reset_window()

            # Module 5: OWD with Starlink asymmetry (upload ~2x slower)
            if owd:
                owd.record_rtt_with_asymmetry(0, sl_rtt, up_ratio=0.33)
                owd.record_rtt_with_asymmetry(1, cell_rtt, up_ratio=0.45)

            # Module 6: throughput accounting
            if throughput_calc:
                for _ in range(pkts_this_tick):
                    throughput_calc.record_sent(1380)
                # Some FEC parity
                for _ in range(pkts_this_tick // 5):
                    throughput_calc.record_sent(1380, is_fec_parity=True)

            # Module 7: session state
            if session_mgr:
                session_mgr.update(
                    global_seq=global_seq,
                    scheduler_mode=scheduler._mode,
                    path_rtt_history={
                        pid: list(s.rtt_history[-10:])
                        for pid, s in states.items()
                    },
                )

            tick += 1
            await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Demo loop error: {e}", exc_info=True)
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
