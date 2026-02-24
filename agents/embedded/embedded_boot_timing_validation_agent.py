import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact

AGENT_NAME = "Embedded Boot Timing Validation Agent"
PHASE = "timing_validate"
OUTPUT_PATH = "firmware/boot/timing_checklist.md"

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
Produce a BOOT TIMING CHECKLIST that a firmware + validation engineer can execute.

HARD OUTPUT RULES (IMPORTANT):
- Output MUST be a markdown document with:
  1) a short 3-5 line intro
  2) a single checklist TABLE (required) with the columns:
     - ID
     - Requirement
     - Measurement method
     - Instrumentation hook
     - Pass/Fail criteria
     - Owner (FW/HW/VAL)
     - Notes/Risks
- No long narrative paragraphs.
- Use explicit time budgets if present in spec; otherwise assume reasonable defaults and mark as ASSUMPTION.

OUTPUT PATH:
- firmware/boot/timing_checklist.md
"""

    out = llm_chat(
        prompt,
        system="You are a senior embedded validation engineer. Produce deterministic checklist tables. Do not use markdown code fences."
    ).strip()

    if not out:
        out = "ERROR: LLM returned empty output."

    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
