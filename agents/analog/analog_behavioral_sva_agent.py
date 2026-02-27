import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text


def _fallback_sva() -> str:
    # Minimal compile-friendly assertion scaffold
    return """\
package analog_checks;

  // Placeholder parameters (tune per spec/requirements)
  parameter real VOUT_MIN = 0.0;
  parameter real VOUT_MAX = 5.0;
  parameter int  SETTLE_CYCLES = 200;

endpackage

module analog_assertions #(parameter real VOUT_MIN = analog_checks::VOUT_MIN,
                           parameter real VOUT_MAX = analog_checks::VOUT_MAX)
(
  input logic clk,
  input logic rst_n,
  input logic en,
  input real  vout
);

  // Example placeholder property: when enabled, vout should be within bounds after some time.
  // This is a structural placeholder; real RNM checkers may need different sampling strategy.
  property p_vout_in_range;
    @(posedge clk) disable iff (!rst_n)
      en |-> (vout >= VOUT_MIN && vout <= VOUT_MAX);
  endproperty

  a_vout_in_range: assert property (p_vout_in_range)
    else $error("[SVA] VOUT out of range: %f", vout);

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

    prompt = f"""
You are a mixed-signal verification engineer.

Create SystemVerilog assertions/checkers (plain text) for an RNM behavioral model.

Prefer deriving numeric bounds from REQUIREMENTS if present, else from spec.targets.
REQUIREMENTS (may be empty):
{json.dumps(req, indent=2)[:4000]}

SPEC:
{json.dumps(spec, indent=2)[:4000]}

Create checks for:
- enable sequencing (EN low => VOUT ~0 or safe state)
- when enabled, VOUT stays within min/max if specified
- settling time window placeholder (parameterized)
- monotonic response placeholder for DC sweep

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