import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code,strip_outer_markdown_fences

AGENT_NAME = "Embedded Reset Sequencing Agent"
PHASE = "reset_sequence"

OUTPUT_RS = "firmware/boot/reset_sequence.rs"
OUTPUT_MD = "firmware/boot/reset_sequence.md"

def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toggles = state.get("toggles") or {}

    # 1) Policy + sequencing doc (md)
    prompt_md = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Generate a reset sequencing POLICY document for boot-up.

HARD OUTPUT RULES:
- Output MUST be markdown.
- Include:
  1) Reset sources and how to detect them (if registers unknown, state assumptions clearly)
  2) Boot-time sequencing order (rails stable → reset deassert → delay → clocks → memory)
  3) Reset-cause handling policy table (POR vs WDT vs SW vs pin reset)
  4) Minimum delay checklist (e.g., after rails stable, after reset deassert, after PLL enable)

OUTPUT PATH:
- {OUTPUT_MD}
"""
    out_md = llm_chat(prompt_md, system="You are a senior embedded firmware engineer. Be specific and deterministic.").strip()
    out_md = strip_outer_markdown_fences(out_md)
    if not out_md:
        out_md = "ERROR: LLM returned empty output."

    write_artifact(state, OUTPUT_MD, out_md, key=OUTPUT_MD.split("/")[-1])

    # 2) Implementation skeleton (rs) - raw Rust only
    prompt_rs = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Generate an implementation-ready Rust module that supports reset cause capture and reset sequencing hooks.

HARD OUTPUT RULES (IMPORTANT):
- Output MUST be RAW RUST ONLY (no markdown fences, no headings, no prose outside code).
- Use Rust comments (//) for assumptions.
- Do NOT mention Verilator/Cocotb/RTL here.

FUNCTIONAL REQUIREMENTS:
- Define ResetCause bitflags/enum.
- Provide read_reset_cause() -> ResetCause
- Provide clear_reset_cause(cause: ResetCause)
- Provide apply_boot_reset_policy(cause: ResetCause) -> Result<(), ResetError>
- If register addresses/bitfields are unknown, abstract behind a minimal trait (e.g., ResetRegs).

OUTPUT PATH:
- {OUTPUT_RS}
"""
    out_rs = llm_chat(prompt_rs, system="Produce compile-ready Rust. Never use markdown code fences.").strip()
    if not out_rs:
        out_rs = "/* ERROR: LLM returned empty output. */"
    out_rs = strip_markdown_fences_for_code(out_rs)
    write_artifact(state, OUTPUT_RS, out_rs, key=OUTPUT_RS.split("/")[-1])

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = [OUTPUT_MD, OUTPUT_RS]

    return state