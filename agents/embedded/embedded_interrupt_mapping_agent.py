import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code,

AGENT_NAME = "Embedded Interrupt Mapping Agent"
PHASE = "irq_map"
OUTPUT_PATH = "firmware/isr/interrupts.rs"

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
Create interrupt vector mapping and ISR stubs.
MANDATORY:
- Include VECTOR_TABLE definition
- Provide DefaultHandler
- Provide weak ISR handlers
- Must compile in no_std firmware environment
OUTPUT REQUIREMENTS:
- Write the primary output to match this path: firmware/isr/interrupts.rs
- Keep it implementation-ready and consistent with Rust + Cargo + Verilator + Cocotb assumptions.
- If information is missing, Add assumptions only as Rust comments:// ASSUMPTION: ...
"""

    out = llm_chat(prompt, system="You are a senior embedded firmware engineer for silicon bring-up and RTL co-simulation. Produce concise, production-quality outputs. Produce compile-ready Rust ISR module only.Produce compile-ready Rust ISR module only.Do not emit crate attributes.")
    if not out:
        out = "ERROR: LLM returned empty output."
    out = strip_markdown_fences_for_code(out)
    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
