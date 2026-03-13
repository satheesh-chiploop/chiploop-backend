import json
import os
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text

def _fallback_sva() -> str:
    return """\
package analog_checks;

  // Generic placeholder parameters (tune per spec/requirements)
  parameter int MAX_LATENCY_CYCLES = 200;

endpackage

module analog_assertions (
  input logic clk,
  input logic rst_n
);

  // Generic placeholder assertion scaffold.
  // Add signal-specific RNM assertions based on normalized spec and requirements.

    property p_output_not_unknown;
        @(posedge clk) disable iff (!rst_n)
            !$isunknown(output_signal);
    endproperty

    a_output_not_unknown: assert property (p_output_not_unknown);
    else $error("[SVA] Placeholder assertion failed");

endmodule
"""


def run_agent(state: dict) -> dict:
    agent_name = "Analog Behavioral Assertions Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    req = state.get("analog_requirements") or {}

    if not workflow_id or not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing workflow_id or analog_spec"
        return state

    workflow_dir = state.get("workflow_dir","")
    model_path = os.path.join(workflow_dir,"analog/model.sv")

    model_text = ""
    if os.path.exists(model_path):
        with open(model_path) as f:
            model_text = f.read()[:4000]

    prompt = f"""
You are a mixed-signal verification engineer.

Create SystemVerilog assertions/checkers (plain text) for an RNM behavioral model.

Prefer deriving numeric bounds from REQUIREMENTS if present, else from spec.targets.
REQUIREMENTS (may be empty):
{json.dumps(req, indent=2)[:4000]}

SPEC:
{json.dumps(spec, indent=2)[:4000]}

RNM MODEL:
{model_text}

Create checks for:
- reset / initialization safety
- enable or trigger sequencing if such signals exist in spec
- output validity / range checks if numeric bounds are present
- latency / settling placeholder if requirements specify timing
- generic monotonic or stability placeholders only when justified by spec


Return ONLY code (no markdown). Keep it modular as a package + module.
If numeric bounds are missing, use placeholders and clearly comment "TBD".
"""

    sva = (llm_text(prompt) or "").strip()
    if not sva:
        sva = _fallback_sva()

    state["analog_sva"] = sva

    if not preview_only:
        # Canonical
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog/behavioral",
            filename="assertions.sv",
            content=sva,
        )

        # Legacy
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="sva.sv",
            content=sva,
        )

    return state