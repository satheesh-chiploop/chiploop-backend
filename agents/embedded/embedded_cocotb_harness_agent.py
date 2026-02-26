import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code

AGENT_NAME = "Embedded Cocotb Harness Agent"
PHASE = "cocotb_harness"
OUTPUT_PATH = "firmware/validate/cocotb_harness.py"

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
Generate a cocotb harness scaffold.

MANDATORY CONTENT:
- Create a clock using cocotb.clock.Clock
- Apply reset sequencing
- Include at least one example test coroutine
- Include placeholder for ELF preload or firmware stimulus
- Include a watchdog timeout (fail test if boot never completes)
- Must import: Clock, RisingEdge, Timer

CORRECTNESS REQUIREMENTS:
- Use dut.signal.value = X syntax
- Use cocotb.clock.Clock for clock generation
- Never use <= operator
- Do not call .read() on DUT signals
- Use dut.<sig>.value.integer when reading numeric values

STRICT RUNTIME RULES:
- Must include @cocotb.test()
- Clock must start using:
  cocotb.start_soon(Clock(...).start())
- Assume ONLY the following signals exist by default:
  dut.clk
  dut.rst_n

- If additional DUT signals are required (e.g., BOOT_DONE, IRQ, APB signals):
  - Declare them as placeholders using:
    # REQUIRED_SIGNAL: <signal_name>
  - Do NOT directly reference them in executable logic unless explicitly present in USER SPEC.

- If USER SPEC does not define specific DUT signals:
  - Generate a minimal harness that:
    - toggles clock
    - applies reset
    - waits fixed cycles
    - terminates cleanly
  - Do NOT reference non-existent DUT signals.
- Any helper coroutine/function MUST accept dut as an argument (no free-variable dut).
- Do not use cocotb.coroutine, yield-based coroutines, or cocotb.utils.
- Test must explicitly terminate using:
  raise cocotb.result.TestSuccess()
  or watchdog failure.

Use ONLY modern cocotb async API.

Mandatory structure:

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

@cocotb.test()
async def firmware_test(dut):

Simulation MUST terminate using:
raise cocotb.result.TestSuccess()
or timeout failure.

HARD OUTPUT RULES (IMPORTANT):
- Output MUST be RAW PYTHON ONLY (no markdown fences, no headings, no prose outside code).
- Put assumptions as Python comments at the top (starting with # ASSUMPTION: ...).
- Keep it implementation-ready and consistent with Rust + Cargo + Verilator + Cocotb assumptions.
- Do not include triple-backticks anywhere.

OUTPUT PATH:
- firmware/validate/cocotb_harness.py
"""

    out = llm_chat(prompt, system="You are a senior verification engineer. Produce runnable Python only. Never use markdown code fences.")
    if not out:
        out = "ERROR: LLM returned empty output."
    out = strip_markdown_fences_for_code(out)

    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
