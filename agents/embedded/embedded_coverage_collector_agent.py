import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact

AGENT_NAME = "Embedded Coverage Collector Agent"
PHASE = "coverage"
OUTPUT_PATH = "firmware/validate/coverage.md"
OUTPUT_FW = "firmware/validate/coverage_fw.md"
OUTPUT_RTL = "firmware/validate/coverage_rtl.md"

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
TASK:
Generate FW and RTL coverage collection steps AND a concise coverage summary with numbers.

OUTPUT REQUIREMENTS:
- Produce THREE files using FILE blocks (no markdown fences):
  FILE: firmware/validate/coverage.md
  FILE: firmware/validate/coverage_fw.md
  FILE: firmware/validate/coverage_rtl.md

- coverage.md must include a summary table with numeric placeholders if real numbers are unavailable:
  FW line % | FW function % | RTL line % | RTL toggle % | Notes

- coverage_fw.md must include:
  - tool method (llvm-cov OR gcov) based on assumptions
  - exact commands
  - where report files land

- coverage_rtl.md must include:
  - verilator coverage method (if supported) OR explicit limitation
  - exact commands
  - where report files land

- If information is missing, list assumptions at the TOP of each markdown file as:
  <!-- ASSUMPTION: ... -->
"""
    out = llm_chat(
    prompt,
    system="You are a senior embedded verification engineer. Output ONLY the requested FILE blocks. No markdown fences. No filler."
    )
    out = (out or "").strip()
    if not out:
       out = "ERROR: LLM returned empty output."

    # Parse FILE: blocks
    files = {}
    current = None
    buf = []
    for line in out.splitlines():
        if line.startswith("FILE: "):
            if current:
               files[current] = "\n".join(buf).strip() + "\n"
            current = line.replace("FILE: ", "").strip()
            buf = []
        else:
            buf.append(line)
    if current:
        files[current] = "\n".join(buf).strip() + "\n"

    # Backward compatible: always write coverage.md
    write_artifact(state, OUTPUT_PATH, files.get(OUTPUT_PATH, out), key=OUTPUT_PATH.split("/")[-1])

    # New optional files (only if present)
    if OUTPUT_FW in files:
        write_artifact(state, OUTPUT_FW, files[OUTPUT_FW], key=OUTPUT_FW.split("/")[-1])
    if OUTPUT_RTL in files:
        write_artifact(state, OUTPUT_RTL, files[OUTPUT_RTL], key=OUTPUT_RTL.split("/")[-1])
    
    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
