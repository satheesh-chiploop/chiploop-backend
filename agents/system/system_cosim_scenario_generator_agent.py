"""
System CoSim Scenario Generator Agent
Production-oriented deterministic scenario generation for L2 co-simulation.

Current scenario classes
- boot
- register read/write
- interrupt propagation

Design goals
- deterministic, auditable outputs
- contract-aware scenario enablement
- easy future extension for DMA / power states / reset sequencing
"""

import datetime
import json
from typing import Any, Dict, List

AGENT_NAME = "System CoSim Scenario Generator Agent"
OUTPUT_SUBDIR = "system/validation/l2/scenarios"

SCENARIOS_JSON = "system_cosim_scenarios.json"
SUMMARY_MD = "system_cosim_scenarios_summary.md"
DEBUG_JSON = "system_cosim_scenario_debug.json"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record(workflow_id: str, filename: str, content: str):
    try:
        from utils.artifact_utils import save_text_artifact_and_record

        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            subdir=OUTPUT_SUBDIR,
            filename=filename,
            content=content,
        )
    except Exception:
        return None

def _software_targets(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    software = manifest.get("software") or {}
    applications = software.get("applications") or []
    entry = software.get("entry") or {}

    targets: List[Dict[str, Any]] = []

    if isinstance(applications, list):
        for item in applications:
            if isinstance(item, dict):
                target = {
                    "app_name": str(item.get("app_name") or "").strip(),
                    "binary_name": str(item.get("binary_name") or "").strip(),
                    "cargo_package": str(item.get("cargo_package") or "").strip(),
                }
                if target["cargo_package"]:
                    targets.append(target)

    if not targets and isinstance(entry, dict):
        target = {
            "app_name": str(entry.get("app_name") or "").strip(),
            "binary_name": str(entry.get("binary_name") or "").strip(),
            "cargo_package": str(entry.get("cargo_package") or "").strip(),
        }
        if target["cargo_package"]:
            targets.append(target)

    return targets

def _scenario_boot(manifest: Dict[str, Any], sw: Dict[str, Any]) -> Dict[str, Any]:
    fw = manifest.get("firmware") or {}
    rtl = manifest.get("rtl") or {}
    app_name = str(sw.get("app_name") or "app").strip()

    return {
        "id": f"{app_name}_boot_smoke",
        "class": "boot",
        "enabled": bool(fw.get("elf") and rtl.get("top")),
        "software_target": sw,
        "deterministic_seed": 101,
        "description": "Boot the firmware ELF on the RTL sim top and verify reset release and first observable software activity.",
        "expected_events": [
            f"app={app_name}",
        ],
        "expected_registers": {},
        "expected_interrupts": [],
        "expected_signals": [],
        "expected_observations": [
            "firmware ELF is loaded",
            "reset is released",
            "simulation reaches first software-visible activity",
        ],
    }
def _scenario_reg_rw(manifest: Dict[str, Any], sw: Dict[str, Any]) -> Dict[str, Any]:
    fw = manifest.get("firmware") or {}
    app_name = str(sw.get("app_name") or "app").strip()

    return {
        "id": f"{app_name}_register_rw_basic",
        "class": "register_read_write",
        "enabled": bool(fw.get("register_map")),
        "software_target": sw,
        "deterministic_seed": 202,
        "description": "Perform deterministic register write/readback against known register map content.",
        "expected_events": [
            f"app={app_name}",
        ],
        "expected_registers": {},
        "expected_interrupts": [],
        "expected_signals": [],
        "expected_observations": [
            "write transaction issued",
            "readback matches expected value",
            "no unexpected fault/timeout",
        ],
    }

def _scenario_interrupt(manifest: Dict[str, Any], sw: Dict[str, Any]) -> Dict[str, Any]:
    fw = manifest.get("firmware") or {}
    interrupts = fw.get("interrupts") or []
    app_name = str(sw.get("app_name") or "app").strip()

    return {
        "id": f"{app_name}_interrupt_propagation_basic",
        "class": "interrupt_propagation",
        "enabled": bool(interrupts),
        "software_target": sw,
        "deterministic_seed": 303,
        "description": "Trigger an interrupt source and validate propagation from RTL event to firmware/software observable handling.",
        "expected_events": [
            f"app={app_name}",
        ],
        "expected_registers": {},
        "expected_interrupts": [str(x) for x in interrupts],
        "expected_signals": [],
        "expected_observations": [
            "interrupt source event occurs",
            "interrupt line/state becomes observable",
            "firmware handler executes",
        ],
        "interrupt_targets": interrupts,
    }


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    workflow_id = str(state.get("workflow_id") or "default")
    print(f"\n⚙️ Running {AGENT_NAME}")

    manifest = state.get("system_cosim_manifest") or {}
    contract_report = state.get("system_cosim_contract_report") or {}

    blocking_errors = [
        item for item in (contract_report.get("issues") or [])
        if item.get("severity") == "error"
    ]
    contract_ready = (contract_report.get("overall_status") == "ready")

    targets = _software_targets(manifest)
    scenarios: List[Dict[str, Any]] = []

    for sw in targets:
        scenarios.extend([
            _scenario_boot(manifest, sw),
            _scenario_reg_rw(manifest, sw),
            _scenario_interrupt(manifest, sw),
        ])
    if not scenarios:
        fallback_target = {
            "app_name": "",
            "binary_name": "",
            "cargo_package": "",
        }
        scenarios = [
            _scenario_boot(manifest, fallback_target),
            _scenario_reg_rw(manifest, fallback_target),
            _scenario_interrupt(manifest, fallback_target),
        ]

    if not contract_ready:
        for s in scenarios:
            s["enabled"] = False
            s["disabled_reason"] = "Blocking contract issues exist."

    enabled_count = sum(1 for s in scenarios if s.get("enabled"))

    plan = {
        "validation_scope": "full_stack",
        "generated_at": _now(),
        "agent": AGENT_NAME,
        "contract_ready": contract_ready,
        "blocking_error_count": len(blocking_errors),
        "scenarios": scenarios,
        "summary": {
            "total": len(scenarios),
            "enabled": enabled_count,
            "disabled": len(scenarios) - enabled_count,
        },
    }

    summary = (
        "# CoSim Scenario Summary\n\n"
        f"- Contract ready: {contract_ready}\n"
        f"- Blocking error count: {len(blocking_errors)}\n"
        f"- Total scenarios: {len(scenarios)}\n"
        f"- Enabled scenarios: {enabled_count}\n"
        f"- Disabled scenarios: {len(scenarios) - enabled_count}\n"
    )

    debug = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "contract_ready": contract_ready,
        "blocking_error_count": len(blocking_errors),
        "scenario_ids": [s["id"] for s in scenarios],
        "enabled_ids": [s["id"] for s in scenarios if s.get("enabled")],
    }

    _record(workflow_id, SCENARIOS_JSON, json.dumps(plan, indent=2))
    _record(workflow_id, SUMMARY_MD, summary)
    _record(workflow_id, DEBUG_JSON, json.dumps(debug, indent=2))

    state["system_cosim_scenarios"] = plan
    state["status"] = "✅ CoSim scenarios ready" if contract_ready else "⚠️ CoSim scenarios generated with disabled state"
    return state
