import json
import os
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code

AGENT_NAME = "Embedded DMA Integration Agent"
PHASE = "dma_integrate"
OUTPUT_PATH = "firmware/dma/dma.rs"

def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}

    workflow_dir = state.get("workflow_dir") or ""

    regmap_obj = (
        state.get("firmware_register_map")
        or (state.get("firmware") or {}).get("register_map")
    )

    if regmap_obj:
        regmap = json.dumps(regmap_obj, indent=2)
    else:
        regmap_path = os.path.join(workflow_dir, "firmware/register_map.json")
        regmap = ""
        if os.path.exists(regmap_path):
           with open(regmap_path) as f:
               regmap = f.read()



    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

TOOLCHAIN (for future extensibility):
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

REGISTER MAP:
{regmap}

TASK:
Integrate DMA channels, descriptors, and ISR hooks.

OUTPUT REQUIREMENTS:
- Write the primary output to match this path: firmware/dma/dma.rs
- Keep it implementation-ready and consistent with Rust + Cargo + Verilator + Cocotb assumptions.
- If information is missing, assumptions only as // ASSUMPTION: ... at top. No prose.
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
