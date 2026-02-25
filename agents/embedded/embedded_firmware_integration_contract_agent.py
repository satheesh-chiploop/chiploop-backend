import json


from ._embedded_common import (
    ensure_workflow_dir,
    llm_chat,
    write_artifact,
    strip_outer_markdown_fences,
)

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

        scope = """BOOT-ONLY integration contract (Embedded_Boot):
IN SCOPE (MUST COVER):
- Reset cause reporting contract (where exposed, how cleared)
- Clock/PLL target + fallback contract (including lock timeout + degraded mode)
- Boot logging schema (minimal: required fields + severity)
- READY indication contract (GPIO/register/event) + timing expectation
- Boot failure policy contract (halt/retry/degraded) + ownership boundary

OUT OF SCOPE (MUST NOT INCLUDE):
- Interrupt catalogs, IRQ priority schemes, ISR APIs
- DMA programming, descriptors, throughput tuning
- Full driver APIs beyond boot-critical init
- Firmware update / OTA / secure boot (unless explicitly in spec_text)
"""

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
  2) Contract version + compatibility policy
  3) Interfaces (tables where appropriate)
  4) Ownership boundaries (FW vs System/Host vs Validation) as a small table
  5) Assumptions (bullets)
  6) Validation hooks (how to test contract compliance)
  - For BOOT-ONLY mode, include a single table named "Boot Contract Interfaces" with rows for:
  ResetCause, ClockConfig, BootLog, ReadySignal, BootFailurePolicy
- Do not add sections for Interrupts/DMA unless explicitly requested in USER SPEC

OUTPUT PATH:
- firmware/integration_contract.md
"""
    out = llm_chat(prompt, system="You are a staff firmware lead. Be specific. No filler. No hallucinated subsystems.").strip()
    if not out:
        out = "ERROR: LLM returned empty output."
    out = strip_outer_markdown_fences(out)
    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state