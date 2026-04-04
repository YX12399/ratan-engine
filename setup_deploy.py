import os
env_content = """RATAN_API_PORT=8080
RATAN_DATA_PORT=9000
RATAN_PROBE_PORT=9001
RATAN_BIND=0.0.0.0
RATAN_MODE=demo
RATAN_PROJECT_DIR=/root/ratan-engine
CORS_ORIGINS=https://ratan-engine.vercel.app,https://ratan-engine-assumechat1.vercel.app
"""
with open("/root/ratan-engine/.env", "w") as f:
    f.write(env_content)

svc = """[Unit]
Description=RATAN HyperAgg Engine v2
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/ratan-engine
EnvironmentFile=/root/ratan-engine/.env
ExecStart=/root/ratan-engine/venv/bin/python -m hyperagg --mode demo --host 0.0.0.0 --port 8080
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
os.system("systemctl enable ratan-engine")
os.system("systemctl restart ratan-engine")
print("Setup complete!")
