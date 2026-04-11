import datetime
import json
import os
import shutil
from typing import Any, Dict, List, Optional

from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "System Software CoSim Harness Agent"
OUTPUT_SUBDIR = "system/software_validation/cosim/harness"
MANIFEST_JSON = "system_cosim_harness_manifest.json"
SUMMARY_MD = "system_cosim_harness_summary.md"
DEBUG_JSON = "system_cosim_harness_debug.json"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record_text(workflow_id: str, filename: str, content: str, subdir: str = OUTPUT_SUBDIR) -> Optional[str]:
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=AGENT_NAME,
            subdir=subdir,
            filename=filename,
            content=content,
        )
    except Exception:
        return None


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(str(value).strip())


def _norm_path(value: Any) -> str:
    return "" if value is None else str(value).strip().replace("\\", "/")


def _artifact_path(info: Any) -> str:
    if isinstance(info, dict):
        for key in ("resolved_path", "path", "artifact_path"):
            value = info.get(key)
            if _is_nonempty_str(value):
                return _norm_path(value)
    elif _is_nonempty_str(info):
        return _norm_path(info)
    return ""


def _first_nonempty(*values: Any) -> str:
    for value in values:
        if _is_nonempty_str(value):
            return str(value).strip()
    return ""


def _tool_available(name: str) -> bool:
    return bool(shutil.which(name))


def _discover_software_assets(state: Dict[str, Any]) -> Dict[str, Any]:
    l1 = state.get("system_software_validation_summary_l1") or state.get("system_software_validation_summary") or {}
    validation_manifest = state.get("system_software_validation_manifest") or {}
    discovered = validation_manifest.get("discovered_assets") or {}

    return {
        "l1_ready": bool(
            l1.get("l1_ready") is True
            or l1.get("overall_l1_ready") is True
            or str(l1.get("overall_status") or "").lower() in {"ready", "pass", "green"}
        ),
        "software_root": _first_nonempty(
            state.get("system_software_validation_local_root"),
            (state.get("system") or {}).get("system_software_validation_local_root"),
        ),
        "build_root": _first_nonempty(
            state.get("system_software_build_root"),
            (state.get("system") or {}).get("system_software_build_root"),
        ),
        "package_manifest_path": _artifact_path(discovered.get("software_package_manifest")),
        "api_contract_path": _artifact_path(discovered.get("api_contract")),
        "input_contract_path": _artifact_path(discovered.get("input_contract")),
    }


def _cosim_ingest_view(state: Dict[str, Any]) -> Dict[str, Any]:
    return (
        state.get("system_software_cosim_ingest")
        or state.get("system_cosim_ingest")
        or {}
    )

def _safe_load_json(path: str) -> Dict[str, Any]:
    try:
        if _is_nonempty_str(path) and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}



def _discover_firmware_assets(state: Dict[str, Any]) -> Dict[str, Any]:
    ingest = _cosim_ingest_view(state)
    firmware = ingest.get("firmware_assets") or {}
    manifest = state.get("system_cosim_manifest") or {}
    manifest_fw = (manifest.get("firmware") or {}) if isinstance(manifest, dict) else {}

    register_map_path = _first_nonempty(
        state.get("firmware_register_map_path"),
        firmware.get("register_map_path"),
        manifest_fw.get("register_map"),
    )

    register_map_json = (
        state.get("firmware_register_map")
        or firmware.get("register_map_spec")
        or {}
    )
    if not isinstance(register_map_json, dict) or not register_map_json:
        register_map_json = _safe_load_json(register_map_path)

    return {
        "register_map_path": register_map_path,
        "register_map_json": register_map_json,
        "hal_path": _first_nonempty(
            state.get("firmware_hal_path"),
            firmware.get("hal_path"),
        ),
        "elf_path": _first_nonempty(
            state.get("firmware_elf_path"),
            firmware.get("elf_path"),
            manifest_fw.get("elf"),
        ),
    }


def _discover_semantic_assets(state: Dict[str, Any]) -> Dict[str, Any]:
    ingest = _cosim_ingest_view(state)
    semantic_ingest = ingest.get("semantic_assets") or {}
    manifest = state.get("system_cosim_manifest") or {}
    validation_spec = manifest.get("validation_spec") or {}
    rtl_spec = validation_spec.get("rtl") or {}

    digital_spec_path = _first_nonempty(
        state.get("digital_spec_json_path"),
        semantic_ingest.get("digital_spec_json_path"),
        rtl_spec.get("digital_spec_path"),
    )
    integration_intent_path = _first_nonempty(
        state.get("system_integration_intent_path"),
        semantic_ingest.get("integration_intent_path"),
        rtl_spec.get("integration_intent_path"),
    )

    digital_spec_json = (
        state.get("digital_spec_json")
        or semantic_ingest.get("digital_spec_json")
        or rtl_spec.get("digital_spec_json")
        or {}
    )
    if not isinstance(digital_spec_json, dict) or not digital_spec_json:
        digital_spec_json = _safe_load_json(digital_spec_path)

    integration_intent_json = (
        state.get("system_integration_intent")
        or semantic_ingest.get("integration_intent_json")
        or rtl_spec.get("integration_intent_json")
        or {}
    )
    if not isinstance(integration_intent_json, dict) or not integration_intent_json:
        integration_intent_json = _safe_load_json(integration_intent_path)

    return {
        "digital_spec_json_path": digital_spec_path,
        "digital_spec_json": digital_spec_json,
        "integration_intent_path": integration_intent_path,
        "integration_intent_json": integration_intent_json,
    }

def _discover_rtl_assets(state: Dict[str, Any]) -> Dict[str, Any]:
    ingest = _cosim_ingest_view(state)
    rtl = ingest.get("rtl_assets") or {}
    manifest = state.get("system_cosim_manifest") or {}
    manifest_rtl = (manifest.get("rtl") or {}) if isinstance(manifest, dict) else {}
    filelists = manifest_rtl.get("filelists") or {}

    sim_filelist = filelists.get("sim") if isinstance(filelists, dict) else []
    sim_first = sim_filelist[0] if isinstance(sim_filelist, list) and sim_filelist else ""

    return {
        "sim_top_path": _first_nonempty(
            state.get("rtl_top_path"),
            rtl.get("top_path"),
            manifest_rtl.get("top"),
        ),
        "sim_harness_path": _first_nonempty(
            state.get("rtl_sim_harness_path"),
            rtl.get("sim_harness_path"),
            sim_first,
        ),
        "verilator_makefile_path": _first_nonempty(
            state.get("rtl_verilator_makefile_path"),
            rtl.get("verilator_makefile_path"),
        ),
        "waveform_root": _first_nonempty(
            state.get("rtl_waveform_root"),
            rtl.get("waveform_root"),
        ),
    }

def _discover_scenarios(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    scenario_manifest = (
        state.get("system_software_cosim_scenarios")
        or state.get("system_cosim_scenarios")
        or {}
    )
    scenarios = scenario_manifest.get("scenarios") or state.get("system_software_cosim_scenario_list") or []
    out: List[Dict[str, Any]] = []
    for item in scenarios:
        if isinstance(item, dict):
            sid = _first_nonempty(item.get("scenario_id"), item.get("id"), item.get("name"))
            if sid:
                enriched = dict(item)
                enriched["scenario_id"] = sid
                out.append(enriched)
    return out


def _build_commands(scenarios: List[Dict[str, Any]], state: Dict[str, Any], tools: Dict[str, bool]) -> List[Dict[str, Any]]:
    commands: List[Dict[str, Any]] = []

    explicit_global = state.get("system_software_cosim_commands") or []
    if isinstance(explicit_global, list):
        for idx, cmd in enumerate(explicit_global):
            if isinstance(cmd, list) and cmd:
                commands.append({
                    "scenario_id": "__global__",
                    "command_id": f"global_{idx}",
                    "command": [str(x) for x in cmd],
                    "source": "state.system_software_cosim_commands",
                })

    software_entry = _discover_software_entry(state)
    entry_command = software_entry.get("command") or []
    entry_app_name = _first_nonempty(
        software_entry.get("app_name"),
        software_entry.get("binary_name"),
    )

    for scenario in scenarios:
        scenario_id = scenario["scenario_id"]
        if scenario.get("enabled") is False:
            continue
        scenario_commands = scenario.get("commands") or []

        if isinstance(scenario_commands, list):
            for idx, cmd in enumerate(scenario_commands):
                if isinstance(cmd, list) and cmd:
                    commands.append({
                        "scenario_id": scenario_id,
                        "command_id": f"{scenario_id}_{idx}",
                        "command": [str(x) for x in cmd],
                        "source": "scenario.commands",
                    })

        if scenario_commands:
            continue

        runner = scenario.get("runner") or ""

        if runner == "cocotb" and tools.get("python"):
            test_mod = _first_nonempty(scenario.get("cocotb_test"), scenario.get("test_module"))
            if test_mod:
                commands.append({
                    "scenario_id": scenario_id,
                    "command_id": f"{scenario_id}_cocotb",
                    "command": ["python", "-m", test_mod],
                    "source": "scenario.runner:cocotb",
                })
                continue

        elif runner == "verilator" and tools.get("make"):
            make_target = _first_nonempty(scenario.get("make_target"), "sim")
            rtl_assets = _discover_rtl_assets(state)
            make_dir = _first_nonempty(
                scenario.get("make_dir"),
                state.get("rtl_build_root"),
                state.get("rtl_sim_root"),
                rtl_assets.get("waveform_root"),
            )
            if make_dir:
                commands.append({
                    "scenario_id": scenario_id,
                    "command_id": f"{scenario_id}_verilator",
                    "command": ["make", "-C", make_dir, make_target],
                    "source": "scenario.runner:verilator",
                })
                continue

        scenario_target = scenario.get("software_target") or {}
        scenario_pkg = _first_nonempty(
            scenario_target.get("cargo_package"),
            software_entry.get("cargo_package"),
        )
        scenario_app_name = _first_nonempty(
            scenario_target.get("app_name"),
            software_entry.get("app_name"),
            software_entry.get("binary_name"),
        )

        if scenario_pkg:
            scenario_args: List[str] = []
            scenario_name = _first_nonempty(
                scenario.get("scenario_id"),
                scenario.get("id"),
                scenario.get("name"),
            )
            if scenario_name:
                scenario_args = ["--", "--scenario", scenario_name]

            commands.append({
                "scenario_id": scenario_id,
                "command_id": f"{scenario_id}_software_entry",
                "command": ["cargo", "run", "-p", scenario_pkg] + scenario_args,
                "source": "software.target.command",
                "entry_app_name": scenario_app_name,
                "cargo_package": scenario_pkg,
            })
        elif isinstance(entry_command, list) and entry_command:
            scenario_args = []
            scenario_name = _first_nonempty(
                scenario.get("scenario_id"),
                scenario.get("id"),
                scenario.get("name"),
            )
            if scenario_name:
                scenario_args = ["--", "--scenario", scenario_name]

            commands.append({
                "scenario_id": scenario_id,
                "command_id": f"{scenario_id}_software_entry",
                "command": [str(x) for x in entry_command] + scenario_args,
                "source": "software.entry.command",
                "entry_app_name": entry_app_name,
            })
    return commands




def _discover_software_entry(state: Dict[str, Any]) -> Dict[str, Any]:
    manifest = state.get("system_cosim_manifest") or {}
    manifest_sw = (manifest.get("software") or {}) if isinstance(manifest, dict) else {}
    package = state.get("system_software_package") or {}
    validation_manifest = state.get("system_software_validation_manifest") or {}
    discovered_assets = validation_manifest.get("discovered_assets") or {}

    entry = (
        manifest_sw.get("entry")
        or state.get("system_software_entry")
        or package.get("entry")
        or discovered_assets.get("software_entry")
        or {}
    )
    return entry if isinstance(entry, dict) else {}


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id") or "default"

    software = _discover_software_assets(state)
    firmware = _discover_firmware_assets(state)
    rtl = _discover_rtl_assets(state)
    semantic_assets = _discover_semantic_assets(state)
    scenarios = _discover_scenarios(state)
    software_entry = _discover_software_entry(state)

    tools = {
        "python": _tool_available("python"),
        "python3": _tool_available("python3"),
        "make": _tool_available("make"),
        "verilator": _tool_available("verilator"),
    }

    blocked_dependencies: List[str] = []

    if not software["l1_ready"]:
        blocked_dependencies.append("software_l1_not_ready")
    if not scenarios:
        blocked_dependencies.append("scenario_manifest_missing_or_empty")
    if not _is_nonempty_str(firmware["elf_path"]):
        blocked_dependencies.append("firmware_elf_missing")
    if not _is_nonempty_str(firmware["register_map_path"]):
        blocked_dependencies.append("firmware_register_map_missing")

    # In Supabase-first mode, an RTL sim filelist entry is enough to consider the
    # harness artifacts resolved for downstream runtime preparation.
    if not (
        _is_nonempty_str(rtl["sim_harness_path"])
        or _is_nonempty_str(rtl["verilator_makefile_path"])
        or _is_nonempty_str(rtl["sim_top_path"])
    ):
        blocked_dependencies.append("rtl_harness_missing")
    

    commands = _build_commands(scenarios, state, tools)
    if not semantic_assets.get("digital_spec_json"):
        blocked_dependencies.append("digital_spec_missing")
    if not semantic_assets.get("integration_intent_json"):
        blocked_dependencies.append("integration_intent_missing")
    if not firmware.get("register_map_json"):
        blocked_dependencies.append("register_map_spec_missing")

    if not commands:
        blocked_dependencies.append("no_executable_cosim_commands_resolved")

    harness_status = "ready" if not blocked_dependencies else "blocked"


    

    manifest = {
        "package_type": "system_cosim_harness_manifest",
        "package_version": "1.0",
        "generated_at": _now(),
        "validation_scope": "l2_cosim",
        "co_sim_enabled": True,
        "l1_ready": software["l1_ready"],
        "harness_status": harness_status,
        "blocked_dependencies": blocked_dependencies,
        "software_assets": software,
        "software_entry": software_entry,
        "firmware_assets": firmware,
        "rtl_assets": rtl,
        "semantic_assets": semantic_assets,
        "tool_availability": tools,
        "scenario_count": len(scenarios),
        "scenarios": scenarios,
        "resolved_commands": commands,
    }

    summary_lines = [
        "# System Software CoSim Harness Summary",
        "",
        f"- Generated at: `{manifest['generated_at']}`",
        f"- L1 ready: `{manifest['l1_ready']}`",
        f"- Harness status: `{harness_status}`",
        f"- Scenario count: `{len(scenarios)}`",
        "",
        "## Blocked dependencies",
    ]
    if blocked_dependencies:
        summary_lines.extend([f"- `{x}`" for x in blocked_dependencies])
    else:
        summary_lines.append("- none")

    summary_lines.extend(["", "## Resolved commands"])
    if commands:
        for cmd in commands:
            summary_lines.append(f"- `{cmd['scenario_id']}` → `{ ' '.join(cmd['command']) }`")
    else:
        summary_lines.append("- none")



    debug_payload = {
        "agent": AGENT_NAME,
        "generated_at": _now(),
        "software": software,
        "software_entry": software_entry,
        "firmware": firmware,
        "rtl": rtl,
        "semantic_assets": {
            "digital_spec_json_path": semantic_assets.get("digital_spec_json_path"),
            "integration_intent_path": semantic_assets.get("integration_intent_path"),
            "digital_spec_loaded": bool(semantic_assets.get("digital_spec_json")),
            "integration_intent_loaded": bool(semantic_assets.get("integration_intent_json")),
        },
        "tool_availability": tools,
        "blocked_dependencies": blocked_dependencies,
        "resolved_command_count": len(commands),
    }

    _record_text(workflow_id, MANIFEST_JSON, json.dumps(manifest, indent=2))
    _record_text(workflow_id, SUMMARY_MD, "\n".join(summary_lines) + "\n")
    _record_text(workflow_id, DEBUG_JSON, json.dumps(debug_payload, indent=2))

    state["system_software_cosim_harness_manifest"] = manifest
    state["system_software_cosim_harness_manifest_path"] = f"{OUTPUT_SUBDIR}/{MANIFEST_JSON}"
    state["system_software_cosim_harness_status"] = harness_status
    state["system_software_cosim_semantic_assets"] = semantic_assets
    state["status"] = "✅ System software co-sim harness ready" if harness_status == "ready" else "⚠️ System software co-sim harness blocked"
    return state
