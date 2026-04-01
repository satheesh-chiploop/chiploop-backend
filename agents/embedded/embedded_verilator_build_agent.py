
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


def _find_existing_path(state: dict, keys: list[str]) -> Optional[str]:
    for key in keys:
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _render_plan(top_module: str, rtl_filelist: str, include_dirs: list[str], defines: list[str], harness: str) -> str:
    inc_flags = " ".join([f"-I{d}" for d in include_dirs]) if include_dirs else "<OPTIONAL_INCLUDE_FLAGS>"
    def_flags = " ".join([f"-D{d}" for d in defines]) if defines else "<OPTIONAL_DEFINE_FLAGS>"
    return f"""<!-- ASSUMPTION: Replace placeholders with concrete file paths before execution. -->
<!-- ASSUMPTION: Cocotb integration is driven externally through pytest/cocotb makefile flow. -->

# Verilator Build Plan

## Inputs
- RTL top module: {top_module}
- RTL filelist: {rtl_filelist}
- Optional include flags: {inc_flags}
- Optional define flags: {def_flags}
- C++ harness: {harness}

## Deterministic command template

verilator -cc --build --trace --top-module {top_module} \
  -f {rtl_filelist} \
  {inc_flags} \
  {def_flags} \
  --exe {harness}

## Expected outputs
- Build directory: obj_dir/
- Generated make/build products under: obj_dir/
- Runnable binary name: obj_dir/V{top_module}

## Notes
- Do not use undocumented Verilator flags.
- Drive cocotb via pytest or the cocotb makefile flow around the compiled simulator.
- If firmware/ELF integration is needed, preload or memory-model integration should be handled by the harness or simulator wrapper, not by invented CLI flags.
"""


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    workflow_dir = os.path.abspath(state.get("workflow_dir") or os.getcwd())
    top_module = (
        state.get("rtl_top")
        or state.get("top_module")
        or state.get("design_name")
        or "TOP_MODULE"
    )
    rtl_filelist = _find_existing_path(
        state,
        [
            "rtl_filelist_path",
            "system_filelist_sim_path",
            "filelist_path",
        ],
    ) or "<RTL_FILELIST>"
    harness = _find_existing_path(
        state,
        [
            "cocotb_harness_path",
            "sim_harness_path",
        ],
    ) or "<HARNESS_CPP>"
    include_dirs = state.get("verilator_include_dirs") or []
    defines = state.get("verilator_defines") or []

    plan = _render_plan(top_module, rtl_filelist, include_dirs, defines, harness)
    write_artifact(state, OUTPUT_PATH, plan, key=os.path.basename(OUTPUT_PATH))

    debug_payload = {
        "agent": AGENT_NAME,
        "workflow_dir": workflow_dir,
        "top_module": top_module,
        "rtl_filelist": rtl_filelist,
        "harness": harness,
        "include_dirs": include_dirs,
        "defines": defines,
        "output_path": OUTPUT_PATH,
        "resolved_from_state": {
            "rtl_top": state.get("rtl_top"),
            "top_module": state.get("top_module"),
            "design_name": state.get("design_name"),
            "rtl_filelist_path": state.get("rtl_filelist_path"),
            "system_filelist_sim_path": state.get("system_filelist_sim_path"),
            "filelist_path": state.get("filelist_path"),
            "cocotb_harness_path": state.get("cocotb_harness_path"),
            "sim_harness_path": state.get("sim_harness_path"),
        },
    }
    write_artifact(state, DEBUG_PATH, json.dumps(debug_payload, indent=2), key=os.path.basename(DEBUG_PATH))

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH
    state["status"] = f"✅ {AGENT_NAME} done"
    return state
