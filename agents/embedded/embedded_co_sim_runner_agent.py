import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code

AGENT_NAME = "Embedded Co Sim Runner Agent"
PHASE = "cosim_run"
OUTPUT_PATH = "firmware/validate/cosim_run.md"
OUTPUT_SCRIPT = "firmware/validate/run_cosim.sh"

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
Generate co-simulation runner steps AND a runnable shell script.

OUTPUT REQUIREMENTS:
- Produce TWO files using FILE blocks (no markdown fences):
  FILE: firmware/validate/cosim_run.md
  FILE: firmware/validate/run_cosim.sh

- cosim_run.md must include:
  - prerequisites (verilator, python, cocotb)
  - exact commands to build RTL sim, build FW (if applicable), and run cocotb
  - expected outputs (logs, junit optional, coverage files)

- run_cosim.sh must be runnable bash:
  - set -euo pipefail
  - DO NOT hardcode RTL paths like ./rtl or specific filenames unless provided in USER SPEC/TOOLCHAIN.
  - Use placeholders and environment overrides:
    RTL_TOP=${RTL_TOP:-<RTL_TOP>}
    RTL_DIR=${RTL_DIR:-<RTL_DIR>}
    FILELIST=${FILELIST:-<FILELIST>}
  - Prefer calling commands described in firmware/validate/verilator_build.md when present.
  - run cocotb via pytest (pytest -q ...)
  - write outputs under firmware/validate/

- Assumptions MUST be listed as markdown comments at the top of cosim_run.md:
  <!-- ASSUMPTION: ... -->

"""
    out = llm_chat(
    prompt,
    system="You are a senior verification engineer. Output ONLY the requested FILE blocks. No markdown fences. No filler."
)
    out = (out or "").strip()
    if not out:
        out = "ERROR: LLM returned empty output."
    out = strip_markdown_fences_for_code(out)

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

    # Ensure bash script has a safe header if it was generated
    if OUTPUT_SCRIPT in files:
        script = files[OUTPUT_SCRIPT]
        if not script.startswith("#!"):
            script = "#!/usr/bin/env bash\nset -euo pipefail\n\n" + script
        elif "set -euo pipefail" not in script:
            script = script.replace("#!/usr/bin/env bash",
                                "#!/usr/bin/env bash\nset -euo pipefail")
        files[OUTPUT_SCRIPT] = script

  
    # Always write cosim_run.md (backward compatible)
    md = files.get(OUTPUT_PATH, out)
    write_artifact(state, OUTPUT_PATH, md, key=OUTPUT_PATH.split("/")[-1])

    # Write run script if present
    if OUTPUT_SCRIPT in files:
        write_artifact(state, OUTPUT_SCRIPT, files[OUTPUT_SCRIPT], key=OUTPUT_SCRIPT.split("/")[-1])
    
    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
