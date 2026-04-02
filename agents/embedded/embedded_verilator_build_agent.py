
import json
import logging
import os
from typing import Optional

from ._embedded_common import ensure_workflow_dir, write_artifact

logger = logging.getLogger(__name__)

AGENT_NAME = "Embedded Verilator Build Agent"
PHASE = "verilator_build"
OUTPUT_PATH = "firmware/validate/verilator_build.md"
DEBUG_PATH = "firmware/validate/verilator_build_debug.json"

def _find_existing_list(state: dict, keys: list[str]) -> list[str]:
    for container in (state, state.get("system") or {}):
        for key in keys:
            value = container.get(key)
            if isinstance(value, list):
                cleaned = [str(v).strip() for v in value if isinstance(v, str) and str(v).strip()]
                if cleaned:
                    return cleaned
    return []

def _find_existing_path(state: dict, keys: list[str]) -> Optional[str]:
    for key in keys:
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    system_block = state.get("system") or {}
    for key in keys:
        value = system_block.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _render_plan(top_module: str, rtl_filelist: str, include_dirs: list[str], defines: list[str], harness: str) -> str:
    inc_flags = " ".join([f"-I{d}" for d in include_dirs]) if include_dirs else "<OPTIONAL_INCLUDE_FLAGS>"
    def_flags = " ".join([f"-D{d}" for d in defines]) if defines else "<OPTIONAL_DEFINE_FLAGS>"

    is_python = str(harness).endswith(".py")

    if is_python:
        harness_section = f"- Cocotb Python test/harness: {harness}"
        exe_section = "# NOTE: No --exe used. Cocotb drives simulation externally."
        exe_cmd = ""
    else:
        harness_section = f"- C++ harness: {harness}"
        exe_section = f"--exe {harness}"
        exe_cmd = f"  {exe_section} \\"

    return f"""<!-- ASSUMPTION: Replace placeholders with concrete file paths before execution. -->
<!-- ASSUMPTION: Cocotb integration is driven externally through pytest/cocotb makefile flow. -->

# Verilator Build Plan

## Inputs
- RTL top module: {top_module}
- RTL filelist: {rtl_filelist}
- Optional include flags: {inc_flags}
- Optional define flags: {def_flags}
{harness_section}

## Deterministic command template

verilator -cc --build --trace --top-module {top_module} \\
  -f {rtl_filelist} \\
  {inc_flags} \\
  {def_flags} \\
  {exe_cmd}

## Expected outputs
- Build directory: obj_dir/
- Generated make/build products under: obj_dir/
- Runnable binary name: obj_dir/V{top_module}

## Notes
- Python cocotb tests are executed via pytest or cocotb makefile flow.
- Do not pass Python files to --exe.
- If firmware/ELF integration is needed, handle it via simulator memory preload or cocotb hooks.
"""

def _materialize_filelist(workflow_dir: str, relpath: str, entries: list[str]) -> str:
    abs_path = os.path.join(workflow_dir, relpath)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write("\n".join(entries) + "\n")
    return abs_path

def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    workflow_dir = os.path.abspath(state.get("workflow_dir") or os.getcwd())

    top_module = (
        state.get("soc_top_sim_module")
        or state.get("system_top_sim_module")
        or state.get("rtl_top")
        or state.get("top_module")
        or state.get("design_name")
        or (state.get("system") or {}).get("soc_top_sim_module")
        or ""
    ).strip()

    rtl_filelist_list = _find_existing_list(
        state,
        [
            "system_rtl_filelist_sim",
            "rtl_inputs",
            "system_rtl_files",
            "rtl_filelist_sim",
        ],
    )

    rtl_filelist = _find_existing_path(
        state,
        [
            "rtl_filelist_path",
            "system_filelist_sim_path",
            "filelist_path",
        ],
    ) or ""

    harness = _find_existing_path(
        state,
        [
            "cocotb_harness_path",
            "sim_harness_path",
        ],
    ) or ""

    
    
    # Harness is not required at this stage — downstream agent will generate it
    if not harness:
        harness = "firmware/validate/cocotb_harness.py"

    include_dirs = state.get("verilator_include_dirs") or []
    defines = state.get("verilator_defines") or []

    if not rtl_filelist and rtl_filelist_list:
      rel_filelist = "firmware/validate/verilator_rtl_filelist.f"

      rtl_filelist = _materialize_filelist(
          workflow_dir,
          rel_filelist,
          rtl_filelist_list,
      )

      # ✅ Persist as artifact (CRITICAL FIX)
      write_artifact(
          state,
          rel_filelist,
          "\n".join(rtl_filelist_list) + "\n",
          key=os.path.basename(rel_filelist),
      )

    missing = []
    if not top_module:
        missing.append("top_module")
    if not rtl_filelist:
        missing.append("rtl_filelist")


    if missing:
        debug_payload = {
            "agent": AGENT_NAME,
            "workflow_dir": workflow_dir,
            "status": "blocked_missing_inputs",
            "missing_inputs": missing,
            "resolved_from_state": {
                "soc_top_sim_module": state.get("soc_top_sim_module"),
                "system_top_sim_module": state.get("system_top_sim_module"),
                "rtl_top": state.get("rtl_top"),
                "top_module": state.get("top_module"),
                "design_name": state.get("design_name"),
                "system_rtl_filelist_sim": state.get("system_rtl_filelist_sim"),
                "rtl_inputs": state.get("rtl_inputs"),
                "system_rtl_files": state.get("system_rtl_files"),
                "system_block": state.get("system"),
                "rtl_filelist_path": state.get("rtl_filelist_path"),
                "system_filelist_sim_path": state.get("system_filelist_sim_path"),
                "filelist_path": state.get("filelist_path"),
                "cocotb_harness_path": state.get("cocotb_harness_path"),
                "sim_harness_path": state.get("sim_harness_path"),
            },
        }
        write_artifact(state, DEBUG_PATH, json.dumps(debug_payload, indent=2), key=os.path.basename(DEBUG_PATH))
        state["status"] = f"❌ {AGENT_NAME} missing required inputs: {', '.join(missing)}"
        return state

    plan = _render_plan(top_module, rtl_filelist, include_dirs, defines, harness)
    write_artifact(state, OUTPUT_PATH, plan, key=os.path.basename(OUTPUT_PATH))

    debug_payload = {
        "agent": AGENT_NAME,
        "workflow_dir": workflow_dir,
        "status": "resolved",
        "top_module": top_module,
        "rtl_filelist": rtl_filelist,
        "rtl_filelist_entries": rtl_filelist_list,
        "harness": harness,
        "harness_required_for_plan": False,
        "harness_source": "state" if (
            state.get("cocotb_harness_path") or
            state.get("sim_harness_path") or
            (state.get("system") or {}).get("cocotb_harness_path") or
            (state.get("system") or {}).get("sim_harness_path")
        ) else "default_expected_path",
        "include_dirs": include_dirs,
        "defines": defines,
        "output_path": OUTPUT_PATH,
        "resolved_from_state": {
            "soc_top_sim_module": state.get("soc_top_sim_module"),
            "system_top_sim_module": state.get("system_top_sim_module"),
            "rtl_top": state.get("rtl_top"),
            "top_module": state.get("top_module"),
            "design_name": state.get("design_name"),
            "system_rtl_filelist_sim": state.get("system_rtl_filelist_sim"),
            "rtl_inputs": state.get("rtl_inputs"),
            "system_rtl_files": state.get("system_rtl_files"),
            "system_block": state.get("system"),
            "rtl_filelist_path": state.get("rtl_filelist_path"),
            "system_filelist_sim_path": state.get("system_filelist_sim_path"),
            "filelist_path": state.get("filelist_path"),
            "cocotb_harness_path": state.get("cocotb_harness_path"),
            "sim_harness_path": state.get("sim_harness_path"),
        },
    }

    
    write_artifact(state, DEBUG_PATH, json.dumps(debug_payload, indent=2), key=os.path.basename(DEBUG_PATH))

    # Pass expected harness path to downstream agents
    state["expected_cocotb_harness_path"] = harness
    state["sim_harness_path"] = state.get("sim_harness_path") or harness
    state["cocotb_harness_path"] = state.get("cocotb_harness_path") or harness

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH
    state["status"] = f"✅ {AGENT_NAME} done"
    return state
