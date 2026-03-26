#!/bin/bash
# ──────────────────────────────────────────────────────────────────
# RATAN Engine — Raspberry Pi Router Setup
# Configures RPi as a network aggregation router for a connected laptop.
#
# Physical setup:
#   Mac Laptop ──(Ethernet)──→ RPi eth0 (DHCP server, 192.168.50.1)
#   RPi wlan0 ──→ Starlink WiFi
#   RPi usb0  ──→ Phone USB tethering (cellular)
#
# Usage: sudo ./scripts/setup_rpi_router.sh <VPS_IP>
# ──────────────────────────────────────────────────────────────────

set -euo pipefail

VPS_IP="${1:-}"
if [ -z "$VPS_IP" ]; then
    echo "Usage: sudo $0 <VPS_IP>"
    echo "Example: sudo $0 203.0.113.10"
    exit 1
fi

LAN_IFACE="${LAN_IFACE:-eth0}"
LAN_IP="192.168.50.1"
LAN_SUBNET="192.168.50.0/24"
LAN_DHCP_START="192.168.50.100"
LAN_DHCP_END="192.168.50.200"

echo "=== RATAN RPi Router Setup ==="
echo "VPS: $VPS_IP"
echo "LAN: $LAN_IFACE ($LAN_IP)"
echo ""

# ── Step 1: Install dependencies ──────────────────────────────────
echo "[1/7] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq dnsmasq python3-pip >/dev/null

echo "[1/7] Installing Python dependencies..."
pip3 install -q -r requirements.txt

# ── Step 2: Configure LAN interface (static IP for Mac connection) ─
echo "[2/7] Configuring LAN interface ($LAN_IFACE)..."

# Prevent dhcpcd from managing the LAN interface
if ! grep -q "interface $LAN_IFACE" /etc/dhcpcd.conf 2>/dev/null; then
    cat >> /etc/dhcpcd.conf << DHCPCD
# RATAN: Static IP for LAN interface
interface $LAN_IFACE
static ip_address=${LAN_IP}/24
nohook wpa_supplicant
DHCPCD
fi

ip addr flush dev "$LAN_IFACE" 2>/dev/null || true
ip addr add "${LAN_IP}/24" dev "$LAN_IFACE" 2>/dev/null || true
ip link set "$LAN_IFACE" up

# ── Step 3: Configure DHCP server for LAN ──────────────────────────
echo "[3/7] Configuring DHCP server (dnsmasq)..."

cat > /etc/dnsmasq.d/ratan-lan.conf << DNSMASQ
# RATAN: DHCP for laptop on LAN
interface=$LAN_IFACE
bind-interfaces
dhcp-range=${LAN_DHCP_START},${LAN_DHCP_END},255.255.255.0,24h
dhcp-option=option:router,$LAN_IP
dhcp-option=option:dns-server,$LAN_IP
# Forward DNS through tunnel (VPS resolves)
server=1.1.1.1
server=8.8.8.8
DNSMASQ

systemctl restart dnsmasq
systemctl enable dnsmasq

# ── Step 4: Enable IP forwarding ───────────────────────────────────
echo "[4/7] Enabling IP forwarding..."
sysctl -w net.ipv4.ip_forward=1
if ! grep -q "net.ipv4.ip_forward=1" /etc/sysctl.conf; then
    echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
fi

# ── Step 5: Set up NAT for LAN → WAN (temporary, before tunnel) ───
echo "[5/7] Setting up initial NAT rules..."

# Allow LAN traffic to reach the internet (for initial setup)
iptables -t nat -A POSTROUTING -s "$LAN_SUBNET" ! -o "$LAN_IFACE" -j MASQUERADE
iptables -A FORWARD -i "$LAN_IFACE" -j ACCEPT
iptables -A FORWARD -o "$LAN_IFACE" -m state --state RELATED,ESTABLISHED -j ACCEPT

# ── Step 6: Create RATAN environment file ──────────────────────────
echo "[6/7] Creating RATAN configuration..."

mkdir -p /etc/ratan-engine
cat > /etc/ratan-engine/env << ENV
# RATAN Client Mode Configuration
RATAN_SERVER_ADDR=$VPS_IP
RATAN_CLIENT_ID=rpi-$(hostname)
RATAN_API_PORT=8080
RATAN_DATA_PORT=9000
RATAN_PROBE_PORT=9001
RATAN_STARLINK_IFACE=wlan0
RATAN_CELLULAR_IFACE=usb0
RATAN_LOG_FILE=/var/log/ratan/engine.log
RATAN_PROJECT_DIR=$(pwd)
ENV

mkdir -p /var/log/ratan

# ── Step 7: Install systemd service ────────────────────────────────
echo "[7/7] Installing RATAN client service..."

cat > /etc/systemd/system/ratan-client.service << SERVICE
[Unit]
Description=RATAN Engine - Edge Client (Multi-WAN Aggregation)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$(pwd)
EnvironmentFile=/etc/ratan-engine/env
ExecStart=/usr/bin/python3 main.py --mode client
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable ratan-client

echo ""
echo "=== RPi Router Setup Complete ==="
echo ""
echo "Network topology:"
echo "  Mac ──(eth0: 192.168.50.x)──→ RPi ──(wlan0: Starlink)──→ VPS ($VPS_IP)"
echo "                                     ──(usb0: Cellular)──→ VPS ($VPS_IP)"
echo ""
echo "Next steps:"
echo "  1. Connect Mac to RPi via Ethernet (will get 192.168.50.x via DHCP)"
echo "  2. Connect RPi to Starlink WiFi:  sudo nmcli dev wifi connect <SSID> password <PASS>"
echo "  3. Plug phone USB tethering into RPi (shows as usb0)"
echo "  4. Start RATAN:  sudo systemctl start ratan-client"
echo "  5. Check status:  sudo systemctl status ratan-client"
echo "  6. View logs:     sudo journalctl -u ratan-client -f"
echo "  7. Dashboard:     http://$VPS_IP:8080/dashboard"
echo ""
echo "To test without TUN (no root needed for basic connectivity test):"
echo "  python3 main.py --mode client --server-addr $VPS_IP --no-tun"
