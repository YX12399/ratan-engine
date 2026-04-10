#!/usr/bin/env bash
#
# first_boot_setup.sh — Run ONCE on a fresh RPi to make it HyperAgg-ready forever.
#
# What this does:
#   1. Installs all dependencies
#   2. Installs the edge agent as a systemd service
#   3. Sets up the WiFi credentials (Starlink)
#   4. Configures eth0 as LAN gateway for the laptop
#   5. After reboot, agent auto-starts and connects to VPS
#
# HOW TO USE:
#   Option A: Plug monitor+keyboard into RPi, login, run this script
#   Option B: Flash RPi OS with SSH enabled, SSH in from laptop, run this script
#   Option C: Put this script on a USB stick, plug into RPi, it auto-runs (advanced)
#
# Usage:
#   sudo bash first_boot_setup.sh \
#     --vps 89.167.91.132 \
#     --wifi-ssid "Starlink-XXXX" \
#     --wifi-pass "your-password"

set -euo pipefail

VPS_HOST=""
WIFI_SSID=""
WIFI_PASS=""
DEVICE_NAME="rpi-$(hostname)"

while [[ $# -gt 0 ]]; do
    case $1 in
        --vps)       VPS_HOST="$2"; shift 2 ;;
        --wifi-ssid) WIFI_SSID="$2"; shift 2 ;;
        --wifi-pass) WIFI_PASS="$2"; shift 2 ;;
        --name)      DEVICE_NAME="$2"; shift 2 ;;
        *) echo "Usage: sudo bash $0 --vps <IP> --wifi-ssid <SSID> --wifi-pass <PASS>"; exit 1 ;;
    esac
done

if [ -z "$VPS_HOST" ]; then
    echo "ERROR: --vps is required"
    echo "Usage: sudo bash $0 --vps 89.167.91.132 --wifi-ssid 'Starlink' --wifi-pass 'password'"
    exit 1
fi

echo ""
echo "╔════════════════════════════════════════════╗"
echo "║  HyperAgg RPi First Boot Setup             ║"
echo "╚════════════════════════════════════════════╝"
echo ""
echo "  VPS:    $VPS_HOST"
echo "  WiFi:   ${WIFI_SSID:-skip}"
echo "  Device: $DEVICE_NAME"
echo ""

# ── 1. System packages ──
echo "[1/7] Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git iproute2 iptables \
    dnsmasq network-manager wireless-tools 2>/dev/null || true

# ── 2. Clone/update repo ──
echo "[2/7] Installing HyperAgg..."
INSTALL_DIR="/opt/hyperagg"
if [ -d "$INSTALL_DIR/.git" ]; then
    cd "$INSTALL_DIR" && git pull --quiet || true
else
    git clone https://github.com/YX12399/ratan-engine.git "$INSTALL_DIR" 2>/dev/null || true
fi
cd "$INSTALL_DIR"

# ── 3. Python deps (ARM-friendly, skip scipy) ──
echo "[3/7] Installing Python dependencies..."
if [ ! -d venv ]; then
    python3 -m venv venv
fi
venv/bin/pip install --quiet pyyaml cryptography reedsolo fastapi uvicorn \
    websockets aiohttp psutil 2>/dev/null
# Try numpy/scipy but don't fail
venv/bin/pip install --quiet numpy scipy 2>/dev/null || echo "  (numpy/scipy skipped — OK on ARM)"

# ── 4. Connect to WiFi (Starlink) ──
if [ -n "$WIFI_SSID" ]; then
    echo "[4/7] Connecting to WiFi: $WIFI_SSID..."
    # Use NetworkManager to connect
    nmcli dev wifi connect "$WIFI_SSID" password "$WIFI_PASS" 2>/dev/null || {
        # Fallback: write wpa_supplicant config
        echo "  nmcli failed, trying wpa_supplicant..."
        cat > /etc/wpa_supplicant/wpa_supplicant-wlan0.conf << WPA
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=GB

network={
    ssid="$WIFI_SSID"
    psk="$WIFI_PASS"
    key_mgmt=WPA-PSK
}
WPA
        systemctl enable wpa_supplicant@wlan0 2>/dev/null
        systemctl restart wpa_supplicant@wlan0 2>/dev/null
        # Request DHCP on wlan0
        dhclient wlan0 2>/dev/null || true
    }
    sleep 3
    WIFI_IP=$(ip -4 addr show wlan0 2>/dev/null | grep -oP 'inet \K[\d.]+' || echo "none")
    echo "  WiFi IP: $WIFI_IP"
else
    echo "[4/7] WiFi: skipped (no --wifi-ssid)"
fi

# ── 5. Configure eth0 as LAN (for laptop) ──
echo "[5/7] Configuring eth0 as LAN gateway..."
# Set static IP on eth0
ip addr add 192.168.50.1/24 dev eth0 2>/dev/null || true
ip link set dev eth0 up

# Configure dnsmasq for DHCP on eth0
cat > /etc/dnsmasq.d/hyperagg.conf << DNS
interface=eth0
bind-interfaces
dhcp-range=192.168.50.100,192.168.50.200,255.255.255.0,12h
dhcp-option=option:router,192.168.50.1
dhcp-option=option:dns-server,192.168.50.1
server=8.8.8.8
server=1.1.1.1
no-resolv
DNS
systemctl restart dnsmasq 2>/dev/null || true
echo "  LAN: 192.168.50.1 (DHCP .100-.200)"

# ── 6. Write agent config ──
echo "[6/7] Writing agent config..."
mkdir -p /etc/hyperagg
cat > /etc/hyperagg/agent.yaml << YAML
device_id: "$DEVICE_NAME"
vps_host: "$VPS_HOST"
vps_port: 8080
tunnel_port: 9999
encryption_key: ""
auto_start: true
interfaces: []
lan_interface: "eth0"
lan_ip: "192.168.50.1"
local_port: 8081
YAML
echo "  Config: /etc/hyperagg/agent.yaml"

# ── 7. Create and enable systemd service ──
echo "[7/7] Creating systemd service..."
cat > /etc/systemd/system/hyperagg-agent.service << SVC
[Unit]
Description=HyperAgg Edge Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python -m hyperagg --mode agent --config /etc/hyperagg/agent.yaml
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVC

systemctl daemon-reload
systemctl enable hyperagg-agent
systemctl start hyperagg-agent

sleep 3
if systemctl is-active --quiet hyperagg-agent; then
    echo ""
    echo "╔════════════════════════════════════════════╗"
    echo "║  SUCCESS — HyperAgg agent is running!       ║"
    echo "║                                             ║"
    echo "║  From your laptop:                          ║"
    echo "║  1. Connect ethernet to this RPi            ║"
    echo "║  2. Wait for DHCP IP (192.168.50.x)        ║"
    echo "║  3. Open http://192.168.50.1:8081           ║"
    echo "║     (local setup UI)                        ║"
    echo "║  4. Or open http://$VPS_HOST:8080           ║"
    echo "║     (VPS dashboard — Devices tab)           ║"
    echo "║  5. Click 'Start Bonding'                   ║"
    echo "╚════════════════════════════════════════════╝"
else
    echo ""
    echo "⚠ Agent may have failed. Check:"
    echo "  journalctl -u hyperagg-agent -f"
fi
