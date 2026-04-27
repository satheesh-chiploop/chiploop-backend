from copy import deepcopy
from threading import Lock
from typing import Any, Dict


class DAGStateStore:
    def __init__(self, initial_state: Dict[str, Any] | None = None):
        self._state = dict(initial_state or {})
        self._lock = Lock()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return deepcopy(self._state)

    def merge(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for key, value in (updates or {}).items():
                if key == "artifacts" and isinstance(value, dict) and isinstance(self._state.get(key), dict):
                    merged = dict(self._state[key])
                    merged.update(value)
                    self._state[key] = merged
                else:
                    self._state[key] = value
            return deepcopy(self._state)
