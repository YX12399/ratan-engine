#!/bin/bash
# ──────────────────────────────────────────────────────────────────
# RATAN Engine — Local Multi-Path Test Lab (Vagrant)
# Based on mptcp-vagrant topology, but runs RATAN's actual tunnel.
#
# Creates 2 VMs with 3 independent network paths between them,
# then deploys RATAN in server/client mode for real aggregation testing
# without needing a Raspberry Pi or Starlink hardware.
#
# Prerequisites: VirtualBox + Vagrant installed
# Usage: vagrant up && ./scripts/test_lab_setup.sh
# ──────────────────────────────────────────────────────────────────

set -euo pipefail

echo "=== RATAN Multi-Path Test Lab ==="

# Check Vagrant is running
if ! vagrant status 2>/dev/null | grep -q "running"; then
    echo "Starting Vagrant VMs..."
    vagrant up
fi

SERVER_IP="192.168.56.101"
CLIENT_IP_PATH1="192.168.56.100"
CLIENT_IP_PATH2="192.168.57.100"
CLIENT_IP_PATH3="192.168.58.100"

# ── Deploy RATAN to server VM ──────────────────────────────────────
echo "[1/4] Deploying RATAN to server VM..."
vagrant ssh server -c "
    cd /vagrant
    pip3 install -r requirements.txt 2>/dev/null

    # Start RATAN in server mode (no TUN for VM testing)
    nohup python3 main.py --mode server --no-tun --bind 0.0.0.0 > /tmp/ratan-server.log 2>&1 &
    echo 'Server PID:' \$!
    sleep 2
    echo 'Server status:' \$(curl -s http://localhost:8080/health | head -c 100)
"

# ── Deploy RATAN to client VM ──────────────────────────────────────
echo "[2/4] Deploying RATAN to client VM..."
vagrant ssh client -c "
    cd /vagrant
    pip3 install -r requirements.txt 2>/dev/null

    # Start RATAN in client mode (no TUN, use simulated paths)
    nohup python3 main.py --mode client --no-tun \
        --server-addr $SERVER_IP \
        --starlink-interface eth1 \
        --cellular-interface eth2 \
        > /tmp/ratan-client.log 2>&1 &
    echo 'Client PID:' \$!
"

# ── Verify connectivity ───────────────────────────────────────────
echo "[3/4] Verifying multi-path connectivity..."
vagrant ssh client -c "
    echo 'Path 1 (eth1):' \$(ping -c 1 -I eth1 $SERVER_IP 2>/dev/null | grep 'time=')
    echo 'Path 2 (eth2):' \$(ping -c 1 -I eth2 ${SERVER_IP/56/57} 2>/dev/null | grep 'time=')
    echo 'Path 3 (eth3):' \$(ping -c 1 -I eth3 ${SERVER_IP/56/58} 2>/dev/null | grep 'time=')
"

echo "[4/4] Test lab ready!"
echo ""
echo "Access:"
echo "  Dashboard:  http://$SERVER_IP:8080/dashboard"
echo "  API docs:   http://$SERVER_IP:8080/docs"
echo "  Server log: vagrant ssh server -c 'tail -f /tmp/ratan-server.log'"
echo "  Client log: vagrant ssh client -c 'tail -f /tmp/ratan-client.log'"
echo ""
echo "Simulate path degradation:"
echo "  vagrant ssh client -c 'sudo tc qdisc add dev eth2 root netem delay 100ms loss 5%'"
echo "  (Watch dashboard — health score drops, balancer shifts weight)"
echo ""
echo "Restore path:"
echo "  vagrant ssh client -c 'sudo tc qdisc del dev eth2 root'"
