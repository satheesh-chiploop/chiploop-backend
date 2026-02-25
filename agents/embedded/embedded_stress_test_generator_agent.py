import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code

AGENT_NAME = "Embedded Stress Test Generator Agent"
PHASE = "stress"
OUTPUT_PATH = "firmware/diagnostics/stress_tests.rs"

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
Create stress tests for clocks/IRQs/DMA/memory.
TASK:
Create stress tests for clocks/IRQs/DMA/memory.

OUTPUT REQUIREMENTS:
- Output MUST be RAW RUST ONLY.
- This file is a Rust MODULE (not crate root).
- DO NOT emit crate attributes:
  - NO #![no_std]
  - NO #![feature(...)]
  - NO crate-level configuration.
- Compatible with inclusion via:
  mod stress_tests;
- Assumptions allowed only as:
  // ASSUMPTION: ...
- Write output to:
  firmware/diagnostics/stress_tests.rs

"""

    out = llm_chat(prompt, system="You are a senior embedded firmware engineer. Output MUST be raw, compile-ready Rust only. NEVER include markdown fences.")
    if not out:
        out = "ERROR: LLM returned empty output."
    out = strip_markdown_fences_for_code(out)
    out = out.replace("#![no_std]", "// no_std configured at crate root")
    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
