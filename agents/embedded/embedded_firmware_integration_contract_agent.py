import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact

AGENT_NAME = "Embedded Firmware Integration Contract Agent"
PHASE = "contract"
OUTPUT_PATH = "firmware/integration_contract.md"

def _is_boot_workflow(state: dict) -> bool:
    s = " ".join([
        str(state.get("workflow_name") or ""),
        str(state.get("workflow_slug") or ""),
        str(state.get("workflow") or ""),
        str(state.get("app_slug") or ""),
    ]).lower()
    return "boot" in s and "run" not in s  # boot wf, not the full run

def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toggles = state.get("toggles") or {}

    boot_only = _is_boot_workflow(state)

    if boot_only:
        scope = """BOOT-ONLY integration contract:
- Reset cause reporting contract
- Clock/PLL target + fallback contract
- Boot logging schema (minimal)
- READY indication contract (GPIO/register/event)
- Boot failure policy contract (halt/retry/degraded)
Do NOT include DMA/interrupt catalogs or full driver APIs."""
    else:
        scope = """GLOBAL firmware integration contract (for Embedded_Run / full chain):
- APIs, expected behaviors, interrupts, DMA, power modes, logging schema, error codes, versioning.
Include full ownership boundaries between FW and System/Host software."""

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

TOGGLES:
{json.dumps(toggles, indent=2)}

SCOPE (MUST FOLLOW):
{scope}

HARD OUTPUT RULES:
- Output MUST be markdown.
- Must include:
  1) Contract overview (5-8 bullets)
  2) Interfaces (tables where appropriate)
  3) Ownership boundaries (FW vs System/Host vs Validation) as a small table
  4) Assumptions (bullets)
  5) Validation hooks (how to test contract compliance)

OUTPUT PATH:
- firmware/integration_contract.md
"""
    out = llm_chat(prompt, system="You are a staff firmware lead. Be specific. No filler. No hallucinated subsystems.").strip()
    if not out:
        out = "ERROR: LLM returned empty output."

    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state