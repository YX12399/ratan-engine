import os, sys

print("=== Step 1: Fetching latest code ===")
os.system("cd /root/ratan-engine && git fetch origin")
os.system("cd /root/ratan-engine && git merge origin/main --no-edit 2>/dev/null || echo 'Already up to date'")

# Try v2 architecture branch
ret = os.system("cd /root/ratan-engine && git fetch origin claude/plan-v2-architecture-HRjA7 2>/dev/null")
if ret == 0:
    os.system("cd /root/ratan-engine && git merge origin/claude/plan-v2-architecture-HRjA7 --no-edit 2>/dev/null || echo 'Merge issue'")
    print("Merged v2 architecture branch")

print("=== Step 2: Installing dependencies ===")
os.system("/root/ratan-engine/venv/bin/pip install -q -r /root/ratan-engine/requirements.txt 2>/dev/null")

print("=== Step 3: Restarting service ===")
os.system("systemctl restart ratan-engine")

import time
time.sleep(3)
ret = os.system("systemctl is-active --quiet ratan-engine")
if ret == 0:
    print("\n=== SUCCESS: Service restarted ===")
else:
    print("\n=== Checking logs ===")
    os.system("journalctl -u ratan-engine --no-pager -n 15")

print("\nRemember to add ANTHROPIC_API_KEY to /root/ratan-engine/.env")
