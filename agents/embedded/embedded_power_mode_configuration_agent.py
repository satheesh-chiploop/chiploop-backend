import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact

AGENT_NAME = "Embedded Power Mode Configuration Agent"
PHASE = "power_modes"
OUTPUT_PATH = "firmware/power/power_modes.md"

def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toggles = state.get("toggles") or {}

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Generate power mode definitions and safe transitions for firmware.

HARD OUTPUT RULES:
- Output MUST be markdown.
- Include:
  1) A single MODE TABLE with columns:
     Mode | Clocks | PLL | Peripherals | SRAM retention | Wake sources | Entry steps | Exit steps | Notes
  2) A short "Do/Don't" list (max 10 bullets) focused on safety pitfalls.
  3) If information is missing, mark assumptions explicitly in an "Assumptions" section (bullets).

OUTPUT PATH:
- firmware/power/power_modes.md
"""
    out = llm_chat(prompt, system="You are a senior embedded firmware engineer. Be concrete and deterministic.").strip()
    if not out:
        out = "ERROR: LLM returned empty output."

    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state