#!/bin/bash
# Deploy RATAN Engine to Hetzner VPS
# Usage: ./scripts/deploy.sh [user@host]

set -euo pipefail

VPS="${1:-root@89.167.91.132}"
REMOTE_DIR="/root/ratan-engine"

echo "=== Deploying RATAN Engine to $VPS ==="

# Sync project files
echo "[1/4] Syncing files..."
rsync -avz --exclude '__pycache__' --exclude '*.pyc' --exclude '.git' \
    --exclude 'node_modules' --exclude 'dashboard/.next' \
    ./ "${VPS}:${REMOTE_DIR}/"

# Install dependencies
echo "[2/4] Installing dependencies..."
ssh "$VPS" "cd ${REMOTE_DIR} && pip3 install -r requirements.txt"

# Install systemd service
echo "[3/4] Installing systemd service..."
ssh "$VPS" "cp ${REMOTE_DIR}/scripts/ratan-engine.service /etc/systemd/system/ && \
    systemctl daemon-reload && \
    systemctl enable ratan-engine"

# Restart service
echo "[4/4] Restarting service..."
ssh "$VPS" "mkdir -p ${REMOTE_DIR}/logs ${REMOTE_DIR}/config && \
    systemctl restart ratan-engine && \
    sleep 2 && \
    systemctl status ratan-engine --no-pager"

echo ""
echo "=== Deployed! ==="
echo "API:    http://$(echo $VPS | cut -d@ -f2):8080"
echo "Health: http://$(echo $VPS | cut -d@ -f2):8080/health"
echo "Docs:   http://$(echo $VPS | cut -d@ -f2):8080/docs"
