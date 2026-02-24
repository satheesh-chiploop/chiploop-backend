import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code

AGENT_NAME = "Embedded Rust Driver Scaffold Agent"
PHASE = "driver_scaffold"
OUTPUT_PATH = "firmware/drivers/driver_scaffold.rs"

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
Generate driver scaffold, init, basic read/write APIs.

OUTPUT REQUIREMENTS:
- Output MUST be RAW RUST ONLY (no markdown fences, no prose).
- Put assumptions as Rust comments at top (// ASSUMPTION: ...).
- Keep it implementation-ready and consistent with Rust + Cargo + Verilator + Cocotb assumptions.
- Write to: firmware/drivers/driver_scaffold.rs
"""

    out = llm_chat(prompt, system="You are a senior embedded Rust engineer. Produce compile-ready Rust only. Never use markdown code fences.")
    if not out:
        out = "ERROR: LLM returned empty output."
    out = strip_markdown_fences_for_code(out)
    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
