"""
Versioned configuration store — single source of truth for all tunable variables.
Every change is versioned with timestamp so we can correlate config changes with metrics.
"""

import json
import time
import copy
import threading
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field, asdict


@dataclass
class ConfigChange:
    version: int
    timestamp: float
    path: str          # dot-notation path like "mptcp_scheduling.subflow_weights.starlink"
    old_value: Any
    new_value: Any
    source: str        # "api", "dashboard", "cowork", "engine"


class ConfigStore:
    """Thread-safe, versioned configuration store."""

    def __init__(self, defaults_path: str = "config/defaults.json"):
        self._lock = threading.RLock()
        self._version = 0
        self._history: list[ConfigChange] = []
        self._listeners: list[callable] = []

        # Load defaults
        defaults_file = Path(defaults_path)
        if defaults_file.exists():
            with open(defaults_file) as f:
                self._config = json.load(f)
        else:
            self._config = {}

        # Try loading persisted state
        self._persist_path = Path("config/current_state.json")
        if self._persist_path.exists():
            with open(self._persist_path) as f:
                state = json.load(f)
                self._config = state.get("config", self._config)
                self._version = state.get("version", 0)

    def get(self, path: str = "", default: Any = None) -> Any:
        """Get config value by dot-notation path. Empty path returns full config."""
        with self._lock:
            if not path:
                return copy.deepcopy(self._config)
            keys = path.split(".")
            val = self._config
            for k in keys:
                if isinstance(val, dict) and k in val:
                    val = val[k]
                else:
                    return default
            return copy.deepcopy(val)

    def set(self, path: str, value: Any, source: str = "api") -> ConfigChange:
        """Set config value, record change, notify listeners."""
        with self._lock:
            keys = path.split(".")
            old_value = self.get(path)

            # Navigate to parent
            target = self._config
            for k in keys[:-1]:
                if k not in target:
                    target[k] = {}
                target = target[k]

            target[keys[-1]] = value
            self._version += 1

            change = ConfigChange(
                version=self._version,
                timestamp=time.time(),
                path=path,
                old_value=old_value,
                new_value=value,
                source=source,
            )
            self._history.append(change)

            # Persist
            self._persist()

            # Notify listeners
            for listener in self._listeners:
                try:
                    listener(change)
                except Exception:
                    pass

            return change

    def batch_set(self, updates: dict[str, Any], source: str = "api") -> list[ConfigChange]:
        """Set multiple values atomically."""
        changes = []
        with self._lock:
            for path, value in updates.items():
                changes.append(self.set(path, value, source))
        return changes

    def get_history(self, since_version: int = 0, limit: int = 100) -> list[dict]:
        """Get config change history since a version."""
        with self._lock:
            filtered = [
                {
                    "version": c.version,
                    "timestamp": c.timestamp,
                    "path": c.path,
                    "old_value": c.old_value,
                    "new_value": c.new_value,
                    "source": c.source,
                }
                for c in self._history
                if c.version > since_version
            ]
            return filtered[-limit:]

    def subscribe(self, listener: callable):
        """Register a callback for config changes."""
        self._listeners.append(listener)

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def _persist(self):
        """Write current state to disk."""
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._persist_path, "w") as f:
                json.dump({
                    "version": self._version,
                    "config": self._config,
                    "saved_at": time.time(),
                }, f, indent=2)
        except Exception:
            pass

    def reset_to_defaults(self, defaults_path: str = "config/defaults.json"):
        """Reset all config to defaults."""
        with self._lock:
            with open(defaults_path) as f:
                self._config = json.load(f)
            self._version += 1
            self._persist()
