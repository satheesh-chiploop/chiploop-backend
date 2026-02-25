import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code

AGENT_NAME = "Embedded Cocotb Harness Agent"
PHASE = "cocotb_harness"
OUTPUT_PATH = "firmware/validate/cocotb_harness.py"

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
Generate a cocotb harness scaffold.

MANDATORY CONTENT:
- Create a clock using cocotb.clock.Clock
- Apply reset sequencing
- Include at least one example test coroutine
- Include placeholder for ELF preload or firmware stimulus

HARD OUTPUT RULES (IMPORTANT):
- Output MUST be RAW PYTHON ONLY (no markdown fences, no headings, no prose outside code).
- Put assumptions as Python comments at the top (starting with # ASSUMPTION: ...).
- Keep it implementation-ready and consistent with Rust + Cargo + Verilator + Cocotb assumptions.
- Do not include triple-backticks anywhere.

OUTPUT PATH:
- firmware/validate/cocotb_harness.py
"""

    out = llm_chat(prompt, system="You are a senior verification engineer. Produce runnable Python only. Never use markdown code fences.")
    if not out:
        out = "ERROR: LLM returned empty output."
    out = strip_markdown_fences_for_code(out)

    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
