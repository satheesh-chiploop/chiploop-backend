import json
import os
from typing import Any


def verification_passed(state: dict[str, Any]) -> tuple[bool, str]:
    summary_path = str(state.get("simulation_summary_coverage_json") or "").strip()
    if not summary_path or not os.path.exists(summary_path):
        return False, "simulation_summary_missing"
    try:
        with open(summary_path, "r", encoding="utf-8") as handle:
            summary = json.load(handle)
    except Exception as exc:
        return False, f"simulation_summary_unreadable:{type(exc).__name__}"
    if not isinstance(summary, dict):
        return False, "simulation_summary_invalid"
    simulation = summary.get("simulation") if isinstance(summary.get("simulation"), dict) else {}
    total = int(simulation.get("total") or 0)
    passed = int(simulation.get("pass") or 0)
    failed = int(simulation.get("fail") or 0)
    if total <= 0:
        return False, "simulation_not_run"
    if failed > 0 or passed < total:
        return False, f"simulation_failed:{passed}/{total}_passed"
    toggles = state.get("toggles") if isinstance(state.get("toggles"), dict) else {}
    toolchain = state.get("toolchain") if isinstance(state.get("toolchain"), dict) else {}
    formal_tool = str(toolchain.get("formal") or state.get("formal_tool") or "none").strip().lower()
    formal_requested = bool(toggles.get("enable_formal")) or formal_tool not in {"", "none", "disabled"}
    if formal_requested:
        formal = summary.get("formal") if isinstance(summary.get("formal"), dict) else {}
        formal_status = str(formal.get("status") or "").strip().lower()
        if formal_status not in {"pass", "passed", "ok", "clean", "completed", "proved"}:
            return False, f"formal_not_passed:{formal_status or 'missing'}"
    return True, f"simulation_passed:{passed}/{total}"
