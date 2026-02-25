import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code

AGENT_NAME = "Embedded Built In Self Test Agent"
PHASE = "bist"
OUTPUT_PATH = "firmware/diagnostics/bist.rs"

def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

TOOLCHAIN (for future extensibility):
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Create BIST hooks and test routines.

OUTPUT REQUIREMENTS:
- Write the primary output to match this path: firmware/diagnostics/bist.rs
- Output MUST be raw, compile-ready Rust MODULE code only (no markdown fences, no prose).
- This file is a Rust MODULE (not crate root). DO NOT emit crate attributes:
  - NO #![no_std]
  - NO #![no_main]
  - NO #![feature(...)]
- Do not define a main() or panic_handler in this module.
- Provide functions (e.g., pub fn run_bist(...) -> Result<...>) that can be called from firmware/src/lib.rs.
- If information is missing, assumptions only as Rust comments at top:
  // ASSUMPTION: ...

"""

    out = llm_chat(prompt, system="You are a senior embedded firmware engineer. Output MUST be raw, compile-ready Rust only. NEVER include markdown fences.")
    if not out:
        out = "ERROR: LLM returned empty output."
    out = strip_markdown_fences_for_code(out)
    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
