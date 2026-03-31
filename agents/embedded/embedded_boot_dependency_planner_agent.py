
import json
import logging
import os
from typing import Optional

from ._embedded_common import ensure_workflow_dir, llm_chat, strip_markdown_fences_for_code, write_artifact

logger = logging.getLogger(__name__)

AGENT_NAME = "Embedded Boot Dependency Planner Agent"
PHASE = "boot_plan"
OUTPUT_PATH = "firmware/boot/boot_sequence.md"
JSON_OUTPUT_PATH = "firmware/boot/boot_sequence.json"
DEBUG_PATH = "firmware/boot/boot_sequence_debug.json"
SUMMARY_PATH = "firmware/boot/boot_sequence_summary.json"
MANIFEST_PATH = "firmware/firmware_manifest.json"


def _safe_load_json(path: str) -> Optional[dict]:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        logger.warning("%s failed loading %s: %s", AGENT_NAME, path, exc)
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


def _load_regmap(state: dict, workflow_dir: str, manifest: dict) -> dict:
    regmap = state.get("firmware_register_map") or (state.get("firmware") or {}).get("register_map")
    if isinstance(regmap, dict):
        return regmap
    regmap_path = state.get("firmware_register_map_path") or manifest.get("register_map_path") or "firmware/register_map.json"
    if regmap_path and not os.path.isabs(regmap_path):
        regmap_path = os.path.join(workflow_dir, regmap_path)
    loaded = _safe_load_json(regmap_path)
    return loaded if isinstance(loaded, dict) else {}


def _has_field(regmap: dict, register_name: str, field_name: str) -> bool:
    for reg in regmap.get("registers") or []:
        if str(reg.get("name") or "").upper() != register_name.upper():
            continue
        for field in reg.get("fields") or []:
            if str(field.get("name") or "").upper() == field_name.upper():
                return True
    return False


def _infer_boot_steps(manifest: dict, regmap: dict) -> list:
    steps = [
        {
            "id": 0,
            "name": "establish_baseline",
            "kind": "baseline",
            "action": "Assume reset state and establish firmware-visible baseline before programming registers.",
            "depends_on": [],
        }
    ]

    ctrl_exists = any(str(reg.get("name") or "").upper() == "CTRL" for reg in regmap.get("registers") or [])
    if ctrl_exists:
        if _has_field(regmap, "CTRL", "ANA_ENABLE"):
            steps.append({
                "id": len(steps),
                "name": "enable_analog",
                "kind": "register_write",
                "register": "CTRL",
                "field": "ANA_ENABLE",
                "value": 1,
                "depends_on": ["establish_baseline"],
            })
        if _has_field(regmap, "CTRL", "DAC_ENABLE"):
            steps.append({
                "id": len(steps),
                "name": "enable_dac",
                "kind": "register_write",
                "register": "CTRL",
                "field": "DAC_ENABLE",
                "value": 1,
                "depends_on": ["enable_analog"] if any(s["name"] == "enable_analog" for s in steps) else ["establish_baseline"],
            })
        if _has_field(regmap, "CTRL", "ADC_START"):
            deps = []
            if any(s["name"] == "enable_analog" for s in steps):
                deps.append("enable_analog")
            if any(s["name"] == "enable_dac" for s in steps):
                deps.append("enable_dac")
            if not deps:
                deps = ["establish_baseline"]
            steps.append({
                "id": len(steps),
                "name": "start_adc",
                "kind": "register_write",
                "register": "CTRL",
                "field": "ADC_START",
                "value": 1,
                "depends_on": deps,
            })

    if any(str(reg.get("name") or "").upper() == "STATUS" for reg in regmap.get("registers") or []):
        steps.append({
            "id": len(steps),
            "name": "poll_status",
            "kind": "register_read",
            "register": "STATUS",
            "depends_on": [steps[-1]["name"]] if len(steps) > 1 else ["establish_baseline"],
        })

    if any(str(reg.get("name") or "").upper() == "ADC_DATA" for reg in regmap.get("registers") or []):
        steps.append({
            "id": len(steps),
            "name": "read_conversion_result",
            "kind": "register_read",
            "register": "ADC_DATA",
            "depends_on": ["poll_status"] if any(s["name"] == "poll_status" for s in steps) else [steps[-1]["name"]],
        })

    return steps


def _default_boot_plan(manifest: dict, regmap: dict) -> dict:
    steps = _infer_boot_steps(manifest, regmap)
    assumptions = []
    if not manifest.get("hardware_features", {}).get("has_programmable_pll", False):
        assumptions.append("No firmware-visible programmable PLL controls were declared; clock programming is skipped.")
    if not manifest.get("hardware_features", {}).get("has_programmable_power_modes", False):
        assumptions.append("No firmware-visible power modes were declared; power sequencing is limited to register-driven bring-up.")
    if not manifest.get("hardware_features", {}).get("has_reset_cause_registers", False):
        assumptions.append("No firmware-visible reset-cause registers were declared; reset handling uses baseline assumptions only.")

    return {
        "agent": AGENT_NAME,
        "phase": PHASE,
        "top_module": manifest.get("top_module") or "soc_top_sim",
        "digital_block_name": manifest.get("digital_block_name") or regmap.get("module_name") or regmap.get("block_name") or "digital_subsystem",
        "bringup_model": manifest.get("bringup_model", {}).get("type") or "register_driven_mixed_signal_bringup",
        "steps": steps,
        "assumptions": assumptions,
    }


def _render_markdown(plan: dict) -> str:
    lines = ["# Boot / Bring-Up Sequence", ""]
    for assumption in plan.get("assumptions") or []:
        lines.append(f"- ASSUMPTION: {assumption}")
    if plan.get("assumptions"):
        lines.append("")
    lines.append(f"- Top module: `{plan.get('top_module')}`")
    lines.append(f"- Digital block: `{plan.get('digital_block_name')}`")
    lines.append(f"- Bring-up model: `{plan.get('bringup_model')}`")
    lines.append("")
    lines.append("## Ordered Steps")
    lines.append("")
    for step in plan.get("steps") or []:
        depends = ", ".join(step.get("depends_on") or []) or "none"
        action = step.get("action") or f"{step.get('kind')} on {step.get('register', 'n/a')}"
        lines.append(f"{step.get('id')}. **{step.get('name')}** — {action} _(depends on: {depends})_")
    lines.append("")
    return "\n".join(lines)


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    logger.info("Starting %s", AGENT_NAME)
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""
    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}

    manifest = _load_manifest(state, workflow_dir)
    regmap = _load_regmap(state, workflow_dir, manifest)

    plan = _default_boot_plan(manifest, regmap)
    write_artifact(state, JSON_OUTPUT_PATH, json.dumps(plan, indent=2), key=os.path.basename(JSON_OUTPUT_PATH))
    write_artifact(state, OUTPUT_PATH, _render_markdown(plan), key=os.path.basename(OUTPUT_PATH))
    write_artifact(
        state,
        DEBUG_PATH,
        json.dumps(
            {
                "agent": AGENT_NAME,
                "manifest_present": bool(manifest),
                "regmap_present": bool(regmap),
                "register_count": len(regmap.get("registers") or []),
                "step_count": len(plan.get("steps") or []),
                "toolchain_keys": sorted(list(toolchain.keys())) if isinstance(toolchain, dict) else [],
                "toggle_keys": sorted(list(toggles.keys())) if isinstance(toggles, dict) else [],
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
                "boot_sequence_path": OUTPUT_PATH,
                "boot_sequence_json_path": JSON_OUTPUT_PATH,
                "step_count": len(plan.get("steps") or []),
                "bringup_model": plan.get("bringup_model"),
            },
            indent=2,
        ),
        key=os.path.basename(SUMMARY_PATH),
    )

    # Canonical firmware toolchain defaults for downstream build/sim agents.
    toolchain = state.setdefault("toolchain", {})
    target_triple = toolchain.get("target_triple") or state.get("target_triple") or manifest.get("build", {}).get("target_triple") or "x86_64-unknown-linux-gnu"
    bin_name = toolchain.get("bin_name") or state.get("firmware_bin_name") or "firmware_app"
    toolchain["target_triple"] = target_triple
    toolchain["bin_name"] = bin_name
    state["target_triple"] = target_triple
    state["firmware_bin_name"] = bin_name

    boot_block = state.setdefault("firmware_boot", {})
    boot_block["boot_sequence_path"] = OUTPUT_PATH
    boot_block["boot_sequence_json_path"] = JSON_OUTPUT_PATH
    boot_block["target_triple"] = target_triple
    boot_block["bin_name"] = bin_name

    manifest = dict(manifest or {})
    manifest["boot_sequence_path"] = OUTPUT_PATH
    manifest["boot_sequence_json_path"] = JSON_OUTPUT_PATH
    build = dict(manifest.get("build") or {})
    build.setdefault("target_triple", target_triple)
    manifest["build"] = build
    write_artifact(state, MANIFEST_PATH, json.dumps(manifest, indent=2), key=os.path.basename(MANIFEST_PATH))
    state["firmware_manifest"] = manifest
    state["firmware_manifest_path"] = MANIFEST_PATH
    firmware_block = state.setdefault("firmware", {})
    firmware_block["manifest"] = manifest
    firmware_block["manifest_path"] = MANIFEST_PATH

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    write_artifact(
        state,
        "firmware/debug/boot_toolchain_debug.json",
        json.dumps(
            {
                "agent": AGENT_NAME,
                "target_triple": state.get("target_triple"),
                "firmware_bin_name": state.get("firmware_bin_name"),
                "toolchain": state.get("toolchain"),
            },
            indent=2,
        ),
        key="boot_toolchain_debug.json",
    )

    state["status"] = f"✅ {AGENT_NAME} done"
    return state
