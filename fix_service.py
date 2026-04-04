import os

svc = """[Unit]
Description=RATAN HyperAgg Engine v2
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/ratan-engine
EnvironmentFile=/root/ratan-engine/.env
ExecStart=/root/ratan-engine/venv/bin/python -m hyperagg --mode demo --api-port 8080
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""

with open("/etc/systemd/system/ratan-engine.service", "w") as f:
    f.write(svc)

os.system("systemctl daemon-reload")
os.system("systemctl restart ratan-engine")
print("Service fixed and restarted!")
