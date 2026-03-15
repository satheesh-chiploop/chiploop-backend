import json
import os
import re
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact,strip_markdown_fences_for_code

AGENT_NAME = "Embedded Rust Register Layer Generator Agent"
PHASE = "hal_generate"
OUTPUT_PATH = "firmware/hal/registers.rs"


def _safe_load_json(path):
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None

def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}

    workflow_dir = state.get("workflow_dir") or ""




    regmap = (
        state.get("firmware_register_map")
        or (state.get("firmware") or {}).get("register_map")
    )

    if not regmap:
        regmap_path = state.get("firmware_register_map_path") or OUTPUT_PATH.replace("hal/registers.rs", "register_map.json")
        if regmap_path and not os.path.isabs(regmap_path):
            regmap_path = os.path.join(workflow_dir, regmap_path)
            regmap = _safe_load_json(regmap_path)

    # --- Validate register map structure ---
    if regmap and "registers" not in regmap and "blocks" not in regmap:
        state["status"] = "❌ register_map.json missing registers"
        return state


    if regmap:
        for r in regmap.get("registers", []):
            if "name" not in r or "offset" not in r:
                state["status"] = f"❌ malformed register entry: {r}"
                return state



    flat_registers = []

    if regmap:
        if "registers" in regmap and isinstance(regmap["registers"], list):
            flat_registers = regmap["registers"]
        elif "blocks" in regmap and isinstance(regmap["blocks"], list):
            for blk in regmap["blocks"]:
                if isinstance(blk, dict):
                    flat_registers.extend(blk.get("registers", []))

    normalized_regmap = {"registers": flat_registers} if flat_registers else regmap
  
    if regmap and not flat_registers:
        state["status"] = "❌ HAL generation received regmap with zero concrete registers"
        return state

    regmap_json = json.dumps(normalized_regmap, indent=2)[:12000] if regmap else "(not available)"


    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

REGISTER MAP (preferred source):
{regmap_json}

TOOLCHAIN:
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Generate Rust HAL register abstractions.

RULES:
- Prefer REGISTER MAP if available.
- Fall back to USER SPEC if register map is missing.
- Output compile-ready Rust module only.
- Every register type and constant must come directly from REGISTER MAP when REGISTER MAP is present.
- Do NOT invent generic registers like Config, Control, Status, Data unless they exist in REGISTER MAP with those exact names.
- Preserve exact register names from REGISTER MAP in generated Rust identifiers as much as possible.
- Emit one Rust constant or register abstraction per register in REGISTER MAP.
- Use exact register names from REGISTER MAP.
- Do not collapse multiple registers into generic placeholders.
- Emit base address + per-register offsets/constants if REGISTER MAP is present.
- This file itself is the `crate::hal::registers` module.
- Do NOT wrap the output in `mod sensor_controller { ... }`
- Do NOT wrap the output in `pub mod registers { ... }`
- Export items directly at module scope.
- Define exactly one `RegisterBlock`.
- Define exactly one `register_block()` accessor.


"""
    out = llm_chat(prompt)
    if not out:
        state["status"] = "❌ HAL generator LLM returned empty output"
        return state

    out = strip_markdown_fences_for_code(out)

    out = out.replace("#![no_std]", "")
    out = out.replace("#![no_main]", "")
    out = out.strip() + "\n"

    # Normalize common bad wrappers so this file is directly crate::hal::registers
    lines = out.splitlines()

    # Strip `pub mod registers { ... }` wrapper
    if lines and lines[0].strip().startswith("pub mod registers"):
        body = lines[1:]
        while body and not body[-1].strip():
            body.pop()
        if body and body[-1].strip() == "}":
            body.pop()
        out = "\n".join(body).strip() + "\n"
        lines = out.splitlines()

    # Strip `mod sensor_controller { ... }` or similar top wrapper
    if lines and re.match(r"(pub\s+)?mod\s+[A-Za-z_]\w*\s*\{?", lines[0].strip()):
        body = lines[1:]
        while body and not body[-1].strip():
            body.pop()
        if body and body[-1].strip() == "}":
            body.pop()
        out = "\n".join(body).strip() + "\n"


    bad_markers = [
        "Certainly!",
        "Below is an example",
        "Make sure to substitute",
        "Here is",
        "```",
    ]

    if any(m in out for m in bad_markers):
        state["status"] = "❌ HAL generation returned explanatory/prose output instead of pure module code"
        return state

    # Fail fast if the HAL shape is still too weak
    if "RegisterBlock" not in out or "register_block" not in out:
        state["status"] = "❌ HAL generation produced invalid module shape"
        return state

    

    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    state["firmware_hal_code"] = out
    state["firmware_hal_path"] = OUTPUT_PATH

    firmware_block = state.setdefault("firmware", {})
    firmware_block["hal_code"] = out
    firmware_block["hal_path"] = OUTPUT_PATH
    state["status"] = f"✅ {AGENT_NAME} done"


    return state
