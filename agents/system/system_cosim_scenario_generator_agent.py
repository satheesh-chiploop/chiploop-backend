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

def _llm_expected_behavior(
    state: Dict[str, Any],
    scenario_id: str,
    scenario_class: str,
    validation_spec: Dict[str, Any],
    software_target: Dict[str, Any],
) -> Dict[str, Any]:
    infer = state.get("llm_json_infer")
    if not callable(infer):
        return {}

    prompt = {
        "task": "Infer expected co-simulation behavior from validation spec.",
        "scenario_id": scenario_id,
        "scenario_class": scenario_class,
        "software_target": software_target,
        "validation_spec": validation_spec,
        "required_schema": {
            "expected_events": ["list[str]"],
            "expected_registers": {"example_register": "example_value"},
            "expected_interrupts": ["list[str]"],
            "expected_signals": ["list[str]"],
        },
    }

    try:
        result = infer(prompt)
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _register_candidates(register_map_spec: Dict[str, Any]) -> List[str]:
    out: List[str] = []

    registers = register_map_spec.get("registers")
    if isinstance(registers, dict):
        for name in registers.keys():
            n = str(name).strip()
            if n:
                out.append(n)
    elif isinstance(registers, list):
        for item in registers:
            if isinstance(item, dict):
                n = str(item.get("name") or item.get("register") or "").strip()
                if n:
                    out.append(n)

    return out


def _expected_behavior(
    state: Dict[str, Any],
    manifest: Dict[str, Any],
    scenario_id: str,
    scenario_class: str,
    sw: Dict[str, Any],
) -> Dict[str, Any]:
    validation_spec = manifest.get("validation_spec") or {}
    firmware_spec = validation_spec.get("firmware") or {}
    register_map_spec = firmware_spec.get("register_map_spec") or {}
    interrupts = firmware_spec.get("interrupts") or []

    app_name = str(sw.get("app_name") or "app").strip()

    # Deterministic fallback
    expected = {
        "expected_events": [
            f"app={app_name}",
            f"scenario={scenario_id}",
        ],
        "expected_registers": {},
        "expected_interrupts": [],
        "expected_signals": [],
    }

    if scenario_class == "register_read_write":
        expected["expected_events"].append("register_write=")
        register_names = _register_candidates(register_map_spec)
        if register_names:
            expected["expected_registers"] = {register_names[0]: "0x10"}

    elif scenario_class == "interrupt_propagation":
        expected["expected_events"].append("interrupt_triggered=1")
        expected["expected_interrupts"] = [str(x) for x in interrupts if str(x).strip()]

    elif scenario_class == "boot":
        expected["expected_signals"] = ["reset_released"]

    # Optional LLM enrichment
    llm_expected = _llm_expected_behavior(
        state=state,
        scenario_id=scenario_id,
        scenario_class=scenario_class,
        validation_spec=validation_spec,
        software_target=sw,
    )

    if isinstance(llm_expected.get("expected_events"), list):
        expected["expected_events"] = llm_expected["expected_events"]
    if isinstance(llm_expected.get("expected_registers"), dict):
        expected["expected_registers"] = llm_expected["expected_registers"]
    if isinstance(llm_expected.get("expected_interrupts"), list):
        expected["expected_interrupts"] = llm_expected["expected_interrupts"]
    if isinstance(llm_expected.get("expected_signals"), list):
        expected["expected_signals"] = llm_expected["expected_signals"]

    return expected

def _scenario_boot(state: Dict[str, Any], manifest: Dict[str, Any], sw: Dict[str, Any]) -> Dict[str, Any]:
    fw = manifest.get("firmware") or {}
    rtl = manifest.get("rtl") or {}
    app_name = str(sw.get("app_name") or "app").strip()
    scenario_id = f"{app_name}_boot_smoke"
    expected = _expected_behavior(state, manifest, scenario_id, "boot", sw)

    return {
        "id": scenario_id,
        "class": "boot",
        "enabled": bool(fw.get("elf") and rtl.get("top")),
        "software_target": sw,
        "deterministic_seed": 101,
        "description": "Boot the firmware ELF on the RTL sim top and verify reset release and first observable software activity.",
        **expected,
        "expected_observations": [
            "firmware ELF is loaded",
            "reset is released",
            "simulation reaches first software-visible activity",
        ],
    }

def _scenario_reg_rw(state: Dict[str, Any], manifest: Dict[str, Any], sw: Dict[str, Any]) -> Dict[str, Any]:
    fw = manifest.get("firmware") or {}
    app_name = str(sw.get("app_name") or "app").strip()
    scenario_id = f"{app_name}_register_rw_basic"
    expected = _expected_behavior(state, manifest, scenario_id, "register_read_write", sw)

    return {
        "id": scenario_id,
        "class": "register_read_write",
        "enabled": bool(fw.get("register_map")),
        "software_target": sw,
        "deterministic_seed": 202,
        "description": "Perform deterministic register write/readback against known register map content.",
        **expected,
        "expected_observations": [
            "write transaction issued",
            "readback matches expected value",
            "no unexpected fault/timeout",
        ],
    }

def _scenario_interrupt(state: Dict[str, Any], manifest: Dict[str, Any], sw: Dict[str, Any]) -> Dict[str, Any]:
    fw = manifest.get("firmware") or {}
    interrupts = fw.get("interrupts") or []
    app_name = str(sw.get("app_name") or "app").strip()
    scenario_id = f"{app_name}_interrupt_propagation_basic"
    expected = _expected_behavior(state, manifest, scenario_id, "interrupt_propagation", sw)

    return {
        "id": scenario_id,
        "class": "interrupt_propagation",
        "enabled": bool(interrupts),
        "software_target": sw,
        "deterministic_seed": 303,
        "description": "Trigger an interrupt source and validate propagation from RTL event to firmware/software observable handling.",
        **expected,
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
            _scenario_boot(state, manifest, sw),
            _scenario_reg_rw(state, manifest, sw),
            _scenario_interrupt(state, manifest, sw),
        ])


    if not scenarios:
        fallback_target = {
            "app_name": "",
            "binary_name": "",
            "cargo_package": "",
        }
        scenarios = [
            _scenario_boot(state, manifest, fallback_target),
            _scenario_reg_rw(state, manifest, fallback_target),
            _scenario_interrupt(state, manifest, fallback_target),
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
