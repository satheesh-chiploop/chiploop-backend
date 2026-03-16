import json
import os
import re
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code

AGENT_NAME = "Embedded Rust Driver Scaffold Agent"
PHASE = "driver_scaffold"
OUTPUT_PATH = "firmware/drivers/driver_scaffold.rs"

def _safe_read(path):
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return ""

def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""

    regmap_path = os.path.join(workflow_dir, "firmware/register_map.json")
    hal_path = os.path.join(workflow_dir, "firmware/hal/registers.rs")

    regmap = _safe_read(regmap_path)
    hal_code = _safe_read(hal_path)



    regmap_obj = (
        state.get("firmware_register_map")
        or (state.get("firmware") or {}).get("register_map")
    )
    hal_code = (
        state.get("firmware_hal_code")
        or (state.get("firmware") or {}).get("hal_code")
    )

    if regmap_obj:
        regmap = json.dumps(regmap_obj, indent=2)
    else:
        regmap_path = state.get("firmware_register_map_path") or "firmware/register_map.json"
        if regmap_path and not os.path.isabs(regmap_path):
            regmap_path = os.path.join(workflow_dir, regmap_path)
        regmap = _safe_read(regmap_path)

    if not hal_code:
        hal_path = state.get("firmware_hal_path") or "firmware/hal/registers.rs"
        if hal_path and not os.path.isabs(hal_path):
            hal_path = os.path.join(workflow_dir, hal_path)
        hal_code = _safe_read(hal_path)


    if not regmap:
        state["status"] = "❌ firmware register map missing in state for driver generation"
        return state

    if not hal_code:
        state["status"] = "❌ firmware HAL missing in state for driver generation"
        return state

    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

REGISTER MAP:
{regmap if regmap else "(not available)"}

HAL REGISTER LAYER:
{hal_code if hal_code else "(not available)"}

TOOLCHAIN:
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Generate a Rust driver scaffold.

RULES:
- Prefer REGISTER MAP + HAL layer when available.
- Fall back to USER SPEC if artifacts are missing.

MANDATORY:
- Every register referenced must exist in REGISTER MAP.
- Do NOT invent registers.
- Use offsets from REGISTER MAP exactly.
- If HAL REGISTER LAYER is present, use its exported register types/constants/functions instead of redefining them.
- Do NOT generate placeholder symbols like RegisterType1, RegisterType2, OFFSET_REG1, OFFSET_REG2.
- Do NOT claim REGISTER MAP or HAL is unavailable when their contents are provided in the prompt.
- Generate only a thin driver scaffold around the actual HAL/register names.
- Do NOT wrap the output in `mod driver_scaffold { ... }`
- The file must contain module contents only.
- Use `use crate::hal::registers::*;` as the import style.
- When using `use crate::hal::registers::*;`, call imported HAL items directly.
- Do NOT prefix HAL calls with `registers::`
- Define exactly one public driver struct named `SensorControllerDriver`.

OUTPUT REQUIREMENTS:
- Output MUST be RAW RUST ONLY (no markdown fences, no prose).
- This file is a Rust MODULE, not crate root.
- DO NOT generate crate attributes:
  - NO #![no_std]
  - NO #![allow(...)]
  - NO #![feature(...)]
- Assume crate configuration exists in lib.rs or main.rs.
- Put assumptions only as Rust comments:
  // ASSUMPTION: ...
- Code must compile when included via:
  mod driver_scaffold;
- Do NOT wrap the output in `mod driver_scaffold { ... }`
- The file must contain module contents only
- Use `use crate::hal::registers::*;` as the import style
- Define exactly one public driver struct named `SensorControllerDriver`
- Write to: firmware/drivers/driver_scaffold.rs

"""
    out = llm_chat(prompt, system="You are a senior embedded Rust engineer. Produce compile-ready Rust only. Never use markdown code fences.")
    if not out:
        state["status"] = "❌ Driver scaffold LLM returned empty output"
        return state

    out = strip_markdown_fences_for_code(out)
    out = out.replace("#![no_std]", "// no_std configured at crate root")

    bad_markers = [
        "Certainly!",
        "Below is an example",
        "Make sure to substitute",
        "Here is",
        "```",
    ]

    if any(m in out for m in bad_markers):
        state["status"] = "❌ Driver scaffold returned explanatory/prose output instead of pure module code"
        return state

    # Safely unwrap a top-level `pub mod driver_scaffold { ... }` wrapper if present
    lines = out.splitlines()
    if lines and re.match(r"pub\s+mod\s+driver_scaffold", lines[0].strip()):
        # Drop first line and final trailing brace if it exists
        body = lines[1:]
        while body and not body[-1].strip():
            body.pop()
        if body and body[-1].strip() == "}":
            body.pop()
        out = "\n".join(body).strip() + "\n"
    else:
        out = out.strip() + "\n"


    # Normalize HAL usage: wildcard import should not be combined with `registers::...`
    if "use crate::hal::registers::*;" in out:
        out = out.replace("registers::register_block()", "register_block()")
        out = out.replace("registers::RegisterBlock", "RegisterBlock")

    # Normalize MMIO register access patterns
    out = re.sub(
        r"let\s+(\w+)\s*=\s*register_block\.(\w+)\s*;",
        r"let \1 = &register_block.\2;",
        out,
    )

    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    state["driver_scaffold_path"] = OUTPUT_PATH
    state["firmware_driver_path"] = OUTPUT_PATH
    state["firmware_driver_code"] = out

    firmware_block = state.setdefault("firmware", {})
    firmware_block["driver_scaffold_path"] = OUTPUT_PATH
    firmware_block["driver_code"] = out

    # Publish simple provenance hints for downstream summary/build agents
    state["driver_scaffold_inputs"] = {
        "used_regmap": bool(regmap),
        "used_hal": bool(hal_code),
        "used_spec": bool(spec_text),
    }

    state["status"] = f"✅ {AGENT_NAME} done"

    return state

