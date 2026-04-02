import json
from typing import Any, Dict, List

from ._embedded_common import ensure_workflow_dir, write_artifact

AGENT_NAME = "Embedded Firmware Executive Summary Agent"
PHASE = "executive"
OUTPUT_PATH = "firmware/executive_summary.md"


def _collect_known_artifacts(state: Dict[str, Any]) -> List[str]:
    embedded = state.get("embedded") or {}
    paths: List[str] = []
    for value in embedded.values():
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    paths.append(item.strip())
    dedup: List[str] = []
    seen = set()
    for path in paths:
        if path not in seen:
            dedup.append(path)
            seen.add(path)
    return dedup


def _bullets(items: List[str], fallback: str) -> List[str]:
    return [f"- {x}" for x in items] if items else [f"- {fallback}"]


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    produced = _collect_known_artifacts(state)
    execution = state.get("system_firmware_execution") or {}
    coverage = state.get("system_firmware_coverage_summary") or {}

    execution_status = str(execution.get("overall_status") or "unavailable")
    execution_readiness = str((execution.get("readiness") or {}).get("status") or "unavailable")
    coverage_metrics = coverage.get("coverage_metrics") or {}
    coverage_available = bool(coverage_metrics.get("coverage_available"))

    overview = [
        f"Overall execution status: {execution_status}",
        f"Execution readiness: {execution_readiness}",
        f"Coverage available: {coverage_available}",
    ]
    results = (execution.get("results") or {}) if isinstance(execution, dict) else {}
    if results:
        overview.append(f"Executed tests: {results.get('executed_test_count', 'unavailable')}")
        overview.append(f"Passed tests: {results.get('passed_test_count', 'unavailable')}")
        overview.append(f"Failed tests: {results.get('failed_test_count', 'unavailable')}")

    assumptions: List[str] = []
    if execution_status != "simulation_passed":
        assumptions.append("Runtime simulation is not claimed as passed unless explicitly recorded upstream.")
    if not coverage_available:
        assumptions.append("Coverage metrics are omitted when no executed run or real coverage artifact is present.")

    risks: List[str] = []
    missing = (execution.get("readiness") or {}).get("missing") or []
    if missing:
        risks.append("Missing execution inputs: " + ", ".join(str(x) for x in missing))
    if execution.get("inputs", {}).get("firmware_elf_placeholder"):
        risks.append("Firmware ELF appears to be a placeholder rather than a real compiled binary.")
    if not produced:
        risks.append("Embedded artifact registry is sparse; downstream summaries may miss some outputs.")

    next_steps: List[str] = []
    if execution_status != "simulation_passed":
        next_steps.append("Run co-simulation with a real firmware ELF and collect runtime logs.")
    if not coverage_available:
        next_steps.append("Enable real coverage instrumentation before reporting percentages.")
    next_steps.append("Preserve artifact paths in state['embedded'] for reproducible downstream reporting.")

    lines: List[str] = []
    lines.append("# Firmware Executive Summary")
    lines.append("")
    lines.append("## Overview")
    lines.extend(_bullets(overview[:8], "No high-level execution details were available."))
    lines.append("")
    lines.append("## Artifacts produced")
    lines.extend(_bullets(produced, "(none recorded in state['embedded'])"))
    lines.append("")
    lines.append("## Key assumptions")
    lines.extend(_bullets(assumptions, "No additional assumptions were required beyond upstream recorded state."))
    lines.append("")
    lines.append("## Risks / Gaps")
    lines.extend(_bullets(risks, "No material gaps were detected from the currently recorded artifacts."))
    lines.append("")
    lines.append("## Next verification steps")
    lines.extend(_bullets(next_steps, "No further verification steps were suggested from currently recorded state."))
    lines.append("")

    out = "\n".join(lines)
    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH
    return state
