import json
import os
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
- Write to: firmware/drivers/driver_scaffold.rs

"""

    out = llm_chat(prompt, system="You are a senior embedded Rust engineer. Produce compile-ready Rust only. Never use markdown code fences.")
    if not out:
        out = "ERROR: LLM returned empty output."
    out = strip_markdown_fences_for_code(out)
    out = out.replace("#![no_std]", "// no_std configured at crate root")
    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
