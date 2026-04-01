
import json
import logging
import os
from typing import Any, Dict, List, Optional

from ._embedded_common import ensure_workflow_dir, write_artifact

logger = logging.getLogger(__name__)

AGENT_NAME = "Embedded Boot Timing Validation Agent"
PHASE = "timing_validate"
OUTPUT_PATH = "firmware/boot/timing_checklist.md"
DEBUG_PATH = "firmware/boot/timing_checklist_debug.json"
SUMMARY_PATH = "firmware/boot/timing_checklist_summary.json"
MANIFEST_PATH = "firmware/firmware_manifest.json"
BOOT_PLAN_JSON_PATH = "firmware/boot/boot_sequence.json"


def _safe_load_json(path: str) -> Optional[dict]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        logger.warning("%s failed to load JSON from %s: %s", AGENT_NAME, path, exc)
    return None


def _load_manifest(state: dict, workflow_dir: str) -> dict:
    manifest = state.get("firmware_manifest") or (state.get("firmware") or {}).get("manifest")
    if isinstance(manifest, dict):
        return dict(manifest)

    manifest_path = state.get("firmware_manifest_path") or (state.get("firmware") or {}).get("manifest_path") or MANIFEST_PATH
    if manifest_path and not os.path.isabs(manifest_path):
        manifest_path = os.path.join(workflow_dir, manifest_path)
    loaded = _safe_load_json(manifest_path)
    return loaded if isinstance(loaded, dict) else {}


def _load_boot_plan(state: dict, workflow_dir: str) -> dict:
    boot_plan = (state.get("firmware_boot") or {}).get("boot_sequence")
    if isinstance(boot_plan, dict):
        return dict(boot_plan)

    manifest = state.get("firmware_manifest") or (state.get("firmware") or {}).get("manifest") or {}

    candidate_paths = [
        (state.get("firmware_boot") or {}).get("boot_sequence_json_path"),
        manifest.get("boot_sequence_json_path"),
        (state.get("firmware_boot") or {}).get("boot_sequence_path"),
        manifest.get("boot_sequence_path"),
        BOOT_PLAN_JSON_PATH,
    ]

    for path in candidate_paths:
        if not path:
            continue
        resolved = path
        if not os.path.isabs(resolved):
            resolved = os.path.join(workflow_dir, resolved)
        loaded = _safe_load_json(resolved)
        if isinstance(loaded, dict):
            return loaded

    return {}



def _steps_from_plan(boot_plan: dict) -> List[dict]:
    for key in ("steps", "sequence", "boot_steps"):
        if isinstance(boot_plan.get(key), list):
            return [s for s in boot_plan[key] if isinstance(s, dict)]
    return []


def _default_rows(manifest: dict, steps: List[dict]) -> List[dict]:
    has_pll = bool(((manifest.get("hardware_features") or {}).get("has_programmable_pll")))
    has_power_modes = bool(((manifest.get("hardware_features") or {}).get("has_programmable_power_modes")))
    irq_signal = ((manifest.get("interrupt_model") or {}).get("top_irq_signal")) or "irq"

    rows: List[dict] = []
    seen = set()

    for idx, step in enumerate(steps, start=1):
        name = str(step.get("name") or step.get("id") or f"step_{idx}")
        lowered = name.lower()

        if "reset" in lowered:
            rows.append({
                "id": f"BT{idx:02d}",
                "requirement": "Reset sequencing order shall match the planned boot sequence.",
                "measurement_method": "Review boot trace / firmware log timestamps for reset-related steps.",
                "instrumentation_hook": "boot_sequence.json step order + firmware boot trace",
                "pass_fail": "Reset steps occur in the declared order with no missing stage.",
                "owner": "FW/HW/VAL",
                "notes": f"Step: {name}",
            })
            seen.add("reset")
        elif "clock" in lowered or "pll" in lowered:
            rows.append({
                "id": f"BT{idx:02d}",
                "requirement": "Clock configuration shall complete before dependent peripherals are enabled.",
                "measurement_method": "Check boot trace ordering and any clock-ready / lock indication.",
                "instrumentation_hook": "boot_sequence.json + clock status registers / trace",
                "pass_fail": "Clock-related step completes before peripheral enable; no timeout observed.",
                "owner": "FW/HW/VAL",
                "notes": f"Step: {name}",
            })
            seen.add("clock")
        elif "power" in lowered:
            rows.append({
                "id": f"BT{idx:02d}",
                "requirement": "Power/analog enable dependencies shall be satisfied before functional use.",
                "measurement_method": "Correlate boot trace with status/ready register reads.",
                "instrumentation_hook": "firmware boot trace + status register sampling",
                "pass_fail": "No peripheral use occurs before corresponding power/ready indication.",
                "owner": "FW/HW/VAL",
                "notes": f"Step: {name}",
            })
            seen.add("power")

    if "reset" not in seen:
        rows.append({
            "id": "BT90",
            "requirement": "Reset deassertion shall complete before firmware accesses functional registers.",
            "measurement_method": "Check first MMIO access occurs after reset release in trace/log.",
            "instrumentation_hook": "firmware trace + reset observable",
            "pass_fail": "No functional MMIO access before reset release.",
            "owner": "FW/HW/VAL",
            "notes": "ASSUMPTION: reset is externally observable in co-sim or trace logs.",
        })

    if has_pll:
        rows.append({
            "id": "BT91",
            "requirement": "PLL lock shall be observed before switching to PLL-derived system clock.",
            "measurement_method": "Check lock indicator polling result and clock switch event ordering.",
            "instrumentation_hook": "PLL lock status + boot trace",
            "pass_fail": "Clock source switch happens only after PLL lock is true.",
            "owner": "FW/HW/VAL",
            "notes": "Applies only when programmable PLL support is present.",
        })
    else:
        rows.append({
            "id": "BT91",
            "requirement": "Clock bring-up shall not assume programmable PLL control when PLL hardware is absent.",
            "measurement_method": "Review boot plan and clock config artifact for skipped PLL programming.",
            "instrumentation_hook": "firmware_manifest.json + boot artifacts",
            "pass_fail": "No firmware PLL programming step required.",
            "owner": "FW/HW/VAL",
            "notes": "Derived from hardware_features.has_programmable_pll = false.",
        })

    rows.append({
        "id": "BT92",
        "requirement": f"Interrupt visibility on top signal '{irq_signal}' shall only be relied on after boot-critical initialization completes.",
        "measurement_method": "Review boot ordering versus interrupt enable/event handling point.",
        "instrumentation_hook": "interrupt map + boot trace",
        "pass_fail": "Interrupt servicing begins only after required init steps complete.",
        "owner": "FW/VAL",
        "notes": "Generic timing safeguard for co-sim/bring-up.",
    })

    if has_power_modes:
        rows.append({
            "id": "BT93",
            "requirement": "Any entry to reduced-power mode shall be blocked until boot validation checkpoints pass.",
            "measurement_method": "Inspect boot policy and trace for premature power-mode transitions.",
            "instrumentation_hook": "power mode config + boot trace",
            "pass_fail": "No low-power transition before boot-ready checkpoint.",
            "owner": "FW/VAL",
            "notes": "Applies only when programmable power modes exist.",
        })

    return rows


def _rows_to_markdown(rows: List[dict]) -> str:
    intro = [
        "This checklist validates timing-sensitive boot dependencies for the current firmware bring-up flow.",
        "It is generated from the firmware manifest and boot sequence artifacts when available.",
        "Where explicit budgets are unavailable, conservative assumptions are called out in the Notes/Risks column.",
        "",
        "| ID | Requirement | Measurement method | Instrumentation hook | Pass/Fail criteria | Owner (FW/HW/VAL) | Notes/Risks |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        intro.append(
            f"| {row['id']} | {row['requirement']} | {row['measurement_method']} | {row['instrumentation_hook']} | {row['pass_fail']} | {row['owner']} | {row['notes']} |"
        )
    return "\n".join(intro) + "\n"


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    logger.info("Starting %s", AGENT_NAME)
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""
    manifest = _load_manifest(state, workflow_dir)
    boot_plan = _load_boot_plan(state, workflow_dir)
    steps = _steps_from_plan(boot_plan)

    rows = _default_rows(manifest, steps)
    out = _rows_to_markdown(rows)

    write_artifact(state, OUTPUT_PATH, out, key=os.path.basename(OUTPUT_PATH))

    write_artifact(
        state,
        DEBUG_PATH,
        json.dumps(
            {
                "agent": AGENT_NAME,
                "manifest_present": bool(manifest),
                "boot_plan_present": bool(boot_plan),
                "step_count": len(steps),
                "row_count": len(rows),
                "boot_sequence_json_path": (
                    (state.get("firmware_boot") or {}).get("boot_sequence_json_path")
                    or manifest.get("boot_sequence_json_path")
                    or BOOT_PLAN_JSON_PATH
                ),
            },
            indent=2,
        ),
        key=os.path.basename(DEBUG_PATH),
    )
    



    write_artifact(
        state,
        SUMMARY_PATH,
        json.dumps(
            {
                "agent": AGENT_NAME,
                "phase": PHASE,
                "checklist_path": OUTPUT_PATH,
                "row_count": len(rows),
            },
            indent=2,
        ),
        key=os.path.basename(SUMMARY_PATH),
    )

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    firmware_boot = state.setdefault("firmware_boot", {})
    firmware_boot["timing_checklist_path"] = OUTPUT_PATH

    state["status"] = f"✅ {AGENT_NAME} done"
    return state

