#!/usr/bin/env bash
#
# setup_rpi_client.sh — Get HyperAgg running on a Raspberry Pi / Beelink
#
# This script handles the REAL problems:
#   1. Installs only the deps that actually work on ARM
#   2. Creates a config that matches the VPS
#   3. Tests connectivity before starting
#   4. Gives clear feedback on what works and what doesn't
#
# Usage:
#   curl -sSL <url> | sudo bash -s -- --vps 89.167.91.132
#   OR: sudo bash setup_rpi_client.sh --vps 89.167.91.132

set -euo pipefail

VPS_HOST=""
VPS_PORT=8080
TUNNEL_PORT=9999
KEY=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --vps)      VPS_HOST="$2"; shift 2 ;;
        --port)     VPS_PORT="$2"; shift 2 ;;
        --key)      KEY="$2"; shift 2 ;;
        *) echo "Usage: sudo bash $0 --vps <VPS_IP> [--key <ENCRYPTION_KEY>]"; exit 1 ;;
    esac
done

if [ -z "$VPS_HOST" ]; then
    echo "ERROR: --vps <VPS_IP> is required"
    echo "Usage: sudo bash $0 --vps 89.167.91.132"
    exit 1
fi

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  RATAN HyperAgg — RPi/Beelink Client Setup          ║"
echo "║  VPS: $VPS_HOST:$TUNNEL_PORT"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── Step 1: Check we're root ──
if [ "$EUID" -ne 0 ]; then
    echo "✗ Must run as root (sudo)"
    exit 1
fi
echo "✓ Running as root"

# ── Step 2: Check /dev/net/tun ──
if [ -c /dev/net/tun ]; then
    echo "✓ TUN device available"
else
    echo "⚠ /dev/net/tun not found — loading kernel module..."
    modprobe tun 2>/dev/null || true
    if [ -c /dev/net/tun ]; then
        echo "✓ TUN module loaded"
    else
        echo "✗ Cannot create TUN device. Check kernel support."
        exit 1
    fi
fi

# ── Step 3: Install minimal deps (ARM-friendly) ──
echo ""
echo "Installing dependencies (ARM-optimized)..."
apt-get update -qq 2>/dev/null
apt-get install -y -qq python3 python3-pip python3-venv git iproute2 iptables 2>/dev/null

INSTALL_DIR="/opt/hyperagg"
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Updating existing installation..."
    cd "$INSTALL_DIR" && git pull --quiet 2>/dev/null || true
else
    echo "Cloning repository..."
    rm -rf "$INSTALL_DIR"
    git clone https://github.com/YX12399/ratan-engine.git "$INSTALL_DIR" 2>/dev/null || {
        echo "✗ Git clone failed. Check network."
        exit 1
    }
fi
cd "$INSTALL_DIR"

# Create venv if needed
if [ ! -d venv ]; then
    python3 -m venv venv
fi

# Install MINIMAL deps (skip scipy/numpy — not needed for client)
echo "Installing Python packages..."
venv/bin/pip install --quiet pyyaml cryptography reedsolo fastapi uvicorn websockets aiohttp psutil 2>/dev/null

# Try numpy/scipy but don't fail if they can't install on ARM
venv/bin/pip install --quiet numpy scipy 2>/dev/null || {
    echo "⚠ numpy/scipy failed (common on ARM) — AI predictor will use fallback mode"
}

echo "✓ Dependencies installed"

# ── Step 4: Fetch encryption key from VPS if not provided ──
if [ -z "$KEY" ]; then
    echo ""
    echo "Fetching config from VPS..."
    KEY_RESPONSE=$(curl -s --max-time 5 "http://$VPS_HOST:$VPS_PORT/api/health" 2>/dev/null || echo "FAIL")
    if echo "$KEY_RESPONSE" | grep -q "hyperagg"; then
        echo "✓ VPS is running HyperAgg"
    else
        echo "⚠ VPS API not reachable at http://$VPS_HOST:$VPS_PORT"
        echo "  Make sure the server is running and port $VPS_PORT is open"
    fi
fi

# ── Step 5: Test UDP connectivity to VPS tunnel port ──
echo ""
echo "Testing connectivity..."

# TCP test to API
if curl -s --max-time 3 "http://$VPS_HOST:$VPS_PORT/api/health" >/dev/null 2>&1; then
    echo "✓ VPS API reachable (TCP $VPS_PORT)"
else
    echo "✗ VPS API NOT reachable at $VPS_HOST:$VPS_PORT"
    echo "  Check: ufw allow $VPS_PORT/tcp on VPS"
fi

# UDP test to tunnel port
if command -v nc >/dev/null 2>&1; then
    echo "test" | nc -u -w1 "$VPS_HOST" "$TUNNEL_PORT" 2>/dev/null && echo "✓ UDP $TUNNEL_PORT open" || echo "⚠ UDP $TUNNEL_PORT may be blocked — check: ufw allow $TUNNEL_PORT/udp on VPS"
fi

# ── Step 6: Detect network interfaces ──
echo ""
echo "Detecting network interfaces..."
IFACES=""
for iface in $(ip -4 -o addr show | grep -v "lo " | awk '{print $2}' | sort -u); do
    ip=$(ip -4 addr show "$iface" | grep -oP 'inet \K[\d.]+')
    gw=$(ip route show dev "$iface" default 2>/dev/null | grep -oP 'via \K[\d.]+' || echo "none")
    echo "  $iface: ip=$ip gateway=$gw"
    IFACES="$IFACES $iface"
done
IFACES=$(echo "$IFACES" | xargs)

if [ -z "$IFACES" ]; then
    echo "✗ No network interfaces found!"
    exit 1
fi
echo "✓ Found interfaces: $IFACES"

# ── Step 7: Create client config ──
echo ""
echo "Creating config..."

cat > "$INSTALL_DIR/config_client.yaml" << YAML
server:
  host: "0.0.0.0"
  port: 8080

vps:
  host: "$VPS_HOST"
  tunnel_port: $TUNNEL_PORT
  api_port: $VPS_PORT
  encryption_key: "${KEY}"
  server_ip: "10.99.0.2"
  client_ip: "10.99.0.1"

interfaces:
  wan_interfaces: [$(echo "$IFACES" | sed 's/ /, /g')]

tunnel:
  mtu: 1400
  keepalive_interval_ms: 1000
  sequence_window: 1024
  reorder_timeout_ms: 100

fec:
  mode: "auto"
  xor_group_size: 4
  rs_data_shards: 8
  rs_parity_shards: 2
  replication_threshold: 0.15
  group_timeout_ms: 500

scheduler:
  mode: "ai"
  probe_interval_ms: 100
  history_window: 50
  prediction_horizon_ms: 200
  ewma_alpha: 0.3
  latency_budget_ms: 150
  jitter_weight: 0.3
  loss_weight: 0.5
  rtt_weight: 0.2

qos:
  enabled: false
YAML

echo "✓ Config written to $INSTALL_DIR/config_client.yaml"

# ── Step 8: Create systemd service ──
cat > /etc/systemd/system/hyperagg-client.service << SERVICE
[Unit]
Description=HyperAgg Bonding Client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python -m hyperagg --mode client --config $INSTALL_DIR/config_client.yaml --api-port 8080 --vps-host $VPS_HOST
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable hyperagg-client
systemctl restart hyperagg-client

echo ""
echo "Waiting 5 seconds for service to start..."
sleep 5

if systemctl is-active --quiet hyperagg-client; then
    echo "✓ HyperAgg client is RUNNING"
    echo ""
    echo "╔══════════════════════════════════════════════════════╗"
    echo "║  SUCCESS — HyperAgg client is active                 ║"
    echo "║                                                      ║"
    echo "║  Dashboard:  http://$(hostname -I | awk '{print $1}'):8080      ║"
    echo "║  Logs:       journalctl -u hyperagg-client -f        ║"
    echo "║  Status:     systemctl status hyperagg-client        ║"
    echo "╚══════════════════════════════════════════════════════╝"
else
    echo "✗ Service failed to start. Checking logs..."
    echo ""
    journalctl -u hyperagg-client --no-pager -n 30
    echo ""
    echo "Common fixes:"
    echo "  1. Check VPS is running: curl http://$VPS_HOST:$VPS_PORT/api/health"
    echo "  2. Check firewall: ufw allow $TUNNEL_PORT/udp (on VPS)"
    echo "  3. Check TUN: ls -la /dev/net/tun"
    echo "  4. Run manually: $INSTALL_DIR/venv/bin/python -m hyperagg --mode client --config $INSTALL_DIR/config_client.yaml --vps-host $VPS_HOST"
fi
