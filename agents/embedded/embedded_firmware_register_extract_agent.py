import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact,strip_outer_markdown_fences

AGENT_NAME = "Embedded Firmware Register Extract Agent"
PHASE = "register_extract"
OUTPUT_PATH = "firmware/register_map.json"

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
Extract registers/CSRs from spec/regmap sources and produce a normalized register map.
OUTPUT REQUIREMENTS:
- Output MUST be VALID JSON ONLY (no markdown fences, no prose).
- If information is missing, add "__assumptions": ["...","..."] inside the JSON.
- Write to: firmware/register_map.json
- Write the primary output to match this path: firmware/register_map.json
- Keep it implementation-ready and consistent with Rust + Cargo + Verilator + Cocotb assumptions.

"""

    out = llm_chat(prompt, system="You are a senior firmware engineer. Output valid JSON only. No markdown fences.")
    if not out:
        out = "ERROR: LLM returned empty output."
    out = strip_outer_markdown_fences(out)
    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
