import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code

AGENT_NAME = "Embedded ELF Build Agent"
PHASE = "elf_build"
OUTPUT_PATH = "firmware/build/build_instructions.md "



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


Generate Cargo build instructions and ELF build steps.
OUTPUT REQUIREMENTS:
- Write the primary output to match this path: firmware/build/build_instructions.md
- Keep it implementation-ready and consistent with Rust + Cargo + Verilator + Cocotb assumptions.
- If information is missing, make reasonable assumptions and clearly list them inside the artifact.
ADDITIONAL OUTPUTS:
Also generate:
- firmware/build/Cargo.toml
- firmware/build/memory.x
"""

    out = llm_chat(prompt, system="You are a senior embedded firmware engineer. Output plain markdown only. Never use markdown code fences.")
    if not out:
        out = "ERROR: LLM returned empty output."
    out = strip_markdown_fences_for_code(out)
    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
