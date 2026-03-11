import json
import os
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact

AGENT_NAME = "Embedded Validation Report Agent"
PHASE = "report"
OUTPUT_PATH = "firmware/validate/validation_report.md"
def _safe_read(path):
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return ""

def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""

    cosim_summary = _safe_read(os.path.join(workflow_dir, "system/firmware/cosim/system_firmware_execution.json"))
    coverage_summary = _safe_read(os.path.join(workflow_dir, "system/firmware/coverage/system_firmware_coverage_summary.json"))

    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

COSIM EXECUTION SUMMARY:
{cosim_summary if cosim_summary else "(not available)"}

COVERAGE SUMMARY:
{coverage_summary if coverage_summary else "(not available)"}

TOOLCHAIN:
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Generate validation report from cosim logs and coverage.

RULES:
- Prefer COSIM + COVERAGE artifacts when available.
- Fall back to USER SPEC if artifacts are missing.

OUTPUT REQUIREMENTS:
- Write the primary output to match this path: firmware/validate/validation_report.md
- Keep it implementation-ready and consistent with Rust + Cargo + Verilator + Cocotb assumptions.
- If information is missing, make reasonable assumptions and clearly list them inside the artifact.
"""

 

    out = llm_chat(prompt, system="You are a senior embedded firmware engineer for silicon bring-up and RTL co-simulation. Produce concise, production-quality outputs. Avoid markdown code fences unless explicitly asked.")
    if not out:
        out = "ERROR: LLM returned empty output."

    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
