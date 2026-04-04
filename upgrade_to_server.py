import os, subprocess, sys

print("=== Step 1: Installing full dependencies ===")
os.system("/root/ratan-engine/venv/bin/pip install -q cryptography>=42.0 reedsolo>=1.7.0 numpy>=1.26.0 scipy>=1.12.0 psutil>=5.9.0 pyyaml>=6.0 aiohttp>=3.9.0 websockets>=12.0 jinja2>=3.1.0 anthropic>=0.39.0")

print("=== Step 2: Also install from requirements.txt ===")
os.system("/root/ratan-engine/venv/bin/pip install -q -r /root/ratan-engine/requirements.txt")

print("=== Step 3: Updating systemd service to server mode ===")
svc = """[Unit]
Description=RATAN HyperAgg Engine v2
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/ratan-engine
EnvironmentFile=/root/ratan-engine/.env
ExecStart=/root/ratan-engine/venv/bin/python -m hyperagg --mode server --api-port 8080
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
"""

with open("/etc/systemd/system/ratan-engine.service", "w") as f:
    f.write(svc)

print("=== Step 4: Opening firewall ports ===")
os.system("ufw allow 8080/tcp")   # API
os.system("ufw allow 9000/udp")   # Data tunnel
os.system("ufw allow 9001/udp")   # Probe port

print("=== Step 5: Reloading and restarting ===")
os.system("systemctl daemon-reload")
os.system("systemctl restart ratan-engine")

import time
time.sleep(3)
ret = os.system("systemctl is-active --quiet ratan-engine")
if ret == 0:
    print("\n=== SUCCESS: ratan-engine is running in SERVER mode ===")
else:
    print("\n=== WARNING: Service may have failed, checking logs... ===")
    os.system("journalctl -u ratan-engine --no-pager -n 20")
