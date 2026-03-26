"""
Cowork Bridge API — endpoints for Cowork-to-VPS communication.
Allows Cowork sessions to:
  - Push goal files to the builder loop
  - Pull build progress and logs
  - Trigger tests
  - Read engine status
  - Send commands to the running engine
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import subprocess
import os
import time
import json
from pathlib import Path

router = APIRouter(prefix="/bridge", tags=["cowork-bridge"])

# Builder loop integration
PROJECT_DIR = os.environ.get("RATAN_PROJECT_DIR", "/root/ratan-engine")
GOAL_FILE = os.path.join(PROJECT_DIR, "goal.md")
PROGRESS_FILE = os.path.join(PROJECT_DIR, "progress.json")
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
SYSTEMD_SERVICE_NAME = os.environ.get("RATAN_SERVICE_NAME", "ratan-engine")
COMMAND_TIMEOUT_SEC = int(os.environ.get("RATAN_COMMAND_TIMEOUT", "30"))


class GoalUpdate(BaseModel):
    content: str
    append: bool = False


class CommandRequest(BaseModel):
    command: str
    timeout: int = COMMAND_TIMEOUT_SEC


class BuildStatus(BaseModel):
    running: bool = False
    current_goal: str = ""
    progress_pct: float = 0.0
    last_cycle: int = 0
    last_output: str = ""
    errors: list[str] = []


@router.get("/status")
async def bridge_status():
    """Full status of the VPS — engine, builder, system."""
    return {
        "timestamp": time.time(),
        "engine": _get_engine_status(),
        "builder": _get_builder_status(),
        "system": _get_system_status(),
    }


@router.get("/goal")
async def get_goal():
    """Read current goal file."""
    if os.path.exists(GOAL_FILE):
        with open(GOAL_FILE) as f:
            return {"content": f.read(), "path": GOAL_FILE}
    return {"content": "", "path": GOAL_FILE}


@router.put("/goal")
async def update_goal(update: GoalUpdate):
    """Push a new goal or append to existing."""
    os.makedirs(os.path.dirname(GOAL_FILE), exist_ok=True)
    mode = "a" if update.append else "w"
    with open(GOAL_FILE, mode) as f:
        f.write(update.content)
    return {"status": "updated", "path": GOAL_FILE, "mode": mode}


@router.get("/progress")
async def get_progress():
    """Get builder loop progress."""
    return _get_builder_status()


@router.get("/logs")
async def get_logs(lines: int = 100, source: str = "engine"):
    """Get recent log lines."""
    log_file = os.path.join(LOG_DIR, f"{source}.log")
    if not os.path.exists(log_file):
        return {"lines": [], "source": source}

    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), log_file],
            capture_output=True, text=True, timeout=5,
        )
        return {"lines": result.stdout.strip().split("\n"), "source": source}
    except Exception as e:
        return {"lines": [], "error": str(e)}


@router.post("/command")
async def run_command(req: CommandRequest):
    """Execute a shell command on the VPS (use with caution)."""
    # Whitelist safe commands
    safe_prefixes = [
        "systemctl status", f"systemctl restart {SYSTEMD_SERVICE_NAME}",
        "cat ", "ls ", "tail ", "head ",
        "python3 -m pytest", "python3 tests/",
        "ip addr", "ip route", "ss -tlnp",
        "curl localhost", "ping -c",
    ]

    if not any(req.command.startswith(p) for p in safe_prefixes):
        raise HTTPException(403, "Command not in whitelist. Allowed prefixes: " + str(safe_prefixes))

    try:
        result = subprocess.run(
            req.command, shell=True, capture_output=True, text=True,
            timeout=req.timeout, cwd=PROJECT_DIR,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "Command timed out")
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/restart-engine")
async def restart_engine():
    """Restart the RATAN engine service."""
    try:
        result = subprocess.run(
            ["systemctl", "restart", SYSTEMD_SERVICE_NAME],
            capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SEC,
        )
        return {
            "status": "restarted" if result.returncode == 0 else "failed",
            "output": result.stdout + result.stderr,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/files")
async def list_project_files():
    """List project files."""
    files = []
    for root, dirs, filenames in os.walk(PROJECT_DIR):
        # Skip hidden dirs and __pycache__
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for fname in filenames:
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, PROJECT_DIR)
            files.append({
                "path": rel,
                "size": os.path.getsize(fpath),
                "modified": os.path.getmtime(fpath),
            })
    return {"project_dir": PROJECT_DIR, "files": files}


@router.get("/file/{file_path:path}")
async def read_file(file_path: str):
    """Read a specific project file."""
    full_path = os.path.join(PROJECT_DIR, file_path)
    if not os.path.exists(full_path):
        raise HTTPException(404, f"File not found: {file_path}")
    if not full_path.startswith(PROJECT_DIR):
        raise HTTPException(403, "Path traversal not allowed")

    try:
        with open(full_path) as f:
            return {"path": file_path, "content": f.read()}
    except Exception as e:
        raise HTTPException(500, str(e))


def _get_engine_status() -> dict:
    """Check if RATAN engine service is running."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", SYSTEMD_SERVICE_NAME],
            capture_output=True, text=True, timeout=5,
        )
        return {"running": result.stdout.strip() == "active"}
    except Exception:
        return {"running": False, "note": "systemctl not available"}


def _get_builder_status() -> dict:
    """Read builder loop progress."""
    status = {"running": False, "last_cycle": 0, "current_goal": ""}

    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE) as f:
                status.update(json.load(f))
        except Exception:
            pass

    if os.path.exists(GOAL_FILE):
        try:
            with open(GOAL_FILE) as f:
                status["current_goal"] = f.read()[:500]
        except Exception:
            pass

    return status


def _get_system_status() -> dict:
    """Get system resource usage."""
    try:
        import shutil
        disk = shutil.disk_usage("/")
        return {
            "disk_total_gb": round(disk.total / (1024**3), 1),
            "disk_used_gb": round(disk.used / (1024**3), 1),
            "disk_free_gb": round(disk.free / (1024**3), 1),
        }
    except Exception:
        return {}
