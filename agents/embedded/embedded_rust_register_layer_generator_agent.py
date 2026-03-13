import json
import os
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact,strip_markdown_fences_for_code

AGENT_NAME = "Embedded Rust Register Layer Generator Agent"
PHASE = "hal_generate"
OUTPUT_PATH = "firmware/hal/registers.rs"


def _safe_load_json(path):
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None

def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}

    workflow_dir = state.get("workflow_dir") or ""

    regmap_path = os.path.join(workflow_dir, "firmware/register_map.json")
    regmap = _safe_load_json(regmap_path)

    # --- Validate register map structure ---
    if regmap and "registers" not in regmap and "blocks" not in regmap:
        state["status"] = "❌ register_map.json missing registers"
        return state


    if regmap:
        for r in regmap.get("registers", []):
            if "name" not in r or "offset" not in r:
                state["status"] = f"❌ malformed register entry: {r}"
                return state



    regmap_json = json.dumps(regmap, indent=2)[:12000] if regmap else "(not available)"



    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

REGISTER MAP (preferred source):
{regmap_json}

TOOLCHAIN:
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Generate Rust HAL register abstractions.

RULES:
- Prefer REGISTER MAP if available.
- Fall back to USER SPEC if register map is missing.
- Output compile-ready Rust module only.
- Every register type and constant must come directly from REGISTER MAP when REGISTER MAP is present.
- Do NOT invent generic registers like Config, Control, Status, Data unless they exist in REGISTER MAP with those exact names.
- Preserve exact register names from REGISTER MAP in generated Rust identifiers as much as possible.
- Emit base address + per-register offsets/constants if REGISTER MAP is present.

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
