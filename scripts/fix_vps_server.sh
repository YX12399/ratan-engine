#!/usr/bin/env bash
#
# fix_vps_server.sh — Fix the VPS server for real client connections
#
# Fixes:
#   1. Firewall: open UDP 9999 (the actual tunnel port, not 9000/9001)
#   2. TUN module: ensure it's loaded
#   3. NAT: ensure MASQUERADE is configured
#   4. Service: restart with correct config
#   5. Verify: test that everything works
#
# Run on the VPS: sudo bash fix_vps_server.sh

set -euo pipefail

echo "╔══════════════════════════════════════════════════════╗"
echo "║  HyperAgg VPS Server Fix                             ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# 1. Firewall
echo "=== Fixing firewall ==="
ufw allow 9999/udp comment "HyperAgg tunnel" 2>/dev/null || iptables -A INPUT -p udp --dport 9999 -j ACCEPT 2>/dev/null || true
ufw allow 8080/tcp comment "HyperAgg dashboard" 2>/dev/null || iptables -A INPUT -p tcp --dport 8080 -j ACCEPT 2>/dev/null || true
echo "✓ Ports 9999/udp and 8080/tcp open"

# 2. TUN module
echo ""
echo "=== Checking TUN device ==="
if [ -c /dev/net/tun ]; then
    echo "✓ /dev/net/tun exists"
else
    modprobe tun
    echo "✓ TUN module loaded"
fi

# 3. IP forwarding
echo ""
echo "=== Enabling IP forwarding ==="
sysctl -w net.ipv4.ip_forward=1 > /dev/null
grep -q "net.ipv4.ip_forward=1" /etc/sysctl.conf || echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
echo "✓ IP forwarding enabled"

# 4. NAT — detect outgoing interface
echo ""
echo "=== Configuring NAT ==="
OUT_IFACE=$(ip route get 8.8.8.8 | grep -oP 'dev \K\S+' | head -1)
echo "  Outgoing interface: $OUT_IFACE"

# Add MASQUERADE if not exists
iptables -t nat -C POSTROUTING -s 10.99.0.0/24 -o "$OUT_IFACE" -j MASQUERADE 2>/dev/null || {
    iptables -t nat -A POSTROUTING -s 10.99.0.0/24 -o "$OUT_IFACE" -j MASQUERADE
    echo "✓ MASQUERADE added for 10.99.0.0/24 → $OUT_IFACE"
}

# FORWARD rules
iptables -C FORWARD -i hagg-srv0 -j ACCEPT 2>/dev/null || iptables -A FORWARD -i hagg-srv0 -j ACCEPT 2>/dev/null
iptables -C FORWARD -o hagg-srv0 -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || iptables -A FORWARD -o hagg-srv0 -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null
echo "✓ FORWARD rules configured"

# 5. Restart service
echo ""
echo "=== Restarting HyperAgg server ==="
systemctl restart ratan-engine 2>/dev/null || {
    echo "⚠ systemd service not found — start manually:"
    echo "  cd /root/ratan-engine && venv/bin/python -m hyperagg --mode server --api-port 8080"
}
sleep 3

# 6. Verify
echo ""
echo "=== Verification ==="
VPS_IP=$(hostname -I | awk '{print $1}')

if curl -s --max-time 3 "http://localhost:8080/api/health" | grep -q "hyperagg"; then
    echo "✓ API responding on port 8080"
else
    echo "✗ API not responding — check: journalctl -u ratan-engine -f"
fi

if ip link show hagg-srv0 >/dev/null 2>&1; then
    echo "✓ TUN device hagg-srv0 is UP"
else
    echo "⚠ TUN device not created yet (will create on first client connection)"
fi

# Check if UDP port is listening
if ss -ulnp | grep -q ":9999"; then
    echo "✓ UDP port 9999 is listening"
else
    echo "⚠ UDP 9999 not yet listening — server may need restart"
fi

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  VPS: $VPS_IP"
echo "║  Dashboard: http://$VPS_IP:8080"
echo "║  Tunnel: UDP $VPS_IP:9999"
echo "║                                                      ║"
echo "║  On your RPi, run:                                    ║"
echo "║  sudo bash setup_rpi_client.sh --vps $VPS_IP"
echo "╚══════════════════════════════════════════════════════╝"
