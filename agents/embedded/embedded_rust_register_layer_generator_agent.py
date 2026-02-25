import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact,strip_markdown_fences_for_code

AGENT_NAME = "Embedded Rust Register Layer Generator Agent"
PHASE = "hal_generate"
OUTPUT_PATH = "firmware/hal/registers.rs"

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
Generate Rust HAL register abstractions from register map.
OUTPUT REQUIREMENTS:
- Write the primary output to match this path: firmware/hal/registers.rs
- Keep it implementation-ready and consistent with Rust + Cargo + Verilator + Cocotb assumptions.
- If information is missing, include assumptions ONLY as Rust comments: // ASSUMPTION: ...
   Do not include explanations or prose.

"""

    out = llm_chat(prompt, system="You are a senior embedded firmware engineer for silicon bring-up and RTL co-simulation. Produce concise, production-quality outputs.Output MUST be compile-ready Rust module code only.Never include markdown fences or explanations.Do NOT emit crate attributes like #![no_std].")
    if not out:
        out = "ERROR: LLM returned empty output."
    out = strip_markdown_fences_for_code(out)
    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
