import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code

AGENT_NAME = "Embedded Verilator Build Agent"
PHASE = "verilator_build"
OUTPUT_PATH = "firmware/validate/verilator_build.md"

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
Generate Verilator build steps + exact commands (or a command template) to build a runnable RTL simulator for Cocotb.

OUTPUT REQUIREMENTS:
- Write the primary output to: firmware/validate/verilator_build.md
- DO NOT assume a specific CPU/ISA or firmware toolchain (no RISC-V/Cortex hardcoding).
- DO NOT assume Rust/Cargo integration; focus on Verilator + Cocotb/pytest flow.
- Must include:
  1) Inputs expected (placeholders ok): <RTL_TOP>, <RTL_FILELIST or RTL_DIR>, optional <INCLUDE_DIRS>, optional <DEFINES>
  2) A concrete Verilator command line template using those placeholders
  3) Build output location (obj_dir) and expected binary name
  4) Use only documented Verilator flags:
   - -cc
   - --exe
   - -I<dir>
   - -D<define>
   - --trace (optional)
   - --build

   DO NOT invent flags.
   DO NOT reference non-existent options (e.g., --with-cocotb).
   Cocotb integration should be described generically via pytest or cocotb makefile flow.
- If information is missing, list assumptions at top as:
  <!-- ASSUMPTION: ... -->
"""
   
    out = llm_chat(prompt, system="You are a senior RTL verification engineer. Output must use only documented Verilator CLI options. Do not invent flags.")
    out = (out or "").strip()
    if not out:
        out = "ERROR: LLM returned empty output."

    # Remove markdown fences if model adds them
    out = strip_markdown_fences_for_code(out)

    # Prevent accidental leftover triple backticks
    out = out.replace("```", "")

    # Normalize common flag typo
    out = out.replace("-top-module", "--top-module")

    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
