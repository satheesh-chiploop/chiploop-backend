

import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code

AGENT_NAME = "Embedded Clock And PLL Configuration Agent"
PHASE = "pll_config"
OUTPUT_PATH = "firmware/boot/pll_config.rs"

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

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Generate an IMPLEMENTATION-READY Rust module for clock + PLL configuration.

HARD OUTPUT RULES (IMPORTANT):
- Output MUST be RAW RUST ONLY (no markdown fences, no headings, no prose outside code).
- Use Rust comments (//) for any assumptions or notes.
- Do not reference Verilator/Cocotb/RTL here. This artifact is for boot-time clock bring-up.

FUNCTIONAL REQUIREMENTS:
- Provide a BootError enum for clock/PLL failures.
- Provide a ClockConfig struct that captures final system clock (Hz), clock source, and degraded_mode.
- Provide functions:
  - configure_clocks() -> Result<ClockConfig, BootError>
  - try_enable_pll(target_hz: u32) -> Result<(), BootError>
  - wait_for_pll_lock(timeout_us: u32) -> Result<(), BootError>
- Include a safe fallback to internal oscillator if PLL fails to lock (set degraded_mode=true).
- If register addresses/bitfields are unknown, abstract them behind a minimal trait (e.g., ClockRegs) with stub methods.

OUTPUT PATH:
- firmware/boot/pll_config.rs
"""

    out = llm_chat(
        prompt,
        system="You are a senior embedded firmware engineer. Produce compile-ready Rust. Never use markdown code fences."
    ).strip()

    if not out:
        out = "/* ERROR: LLM returned empty output. */"

    # enforce raw code for .rs
    out = strip_markdown_fences_for_code(out)

    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
