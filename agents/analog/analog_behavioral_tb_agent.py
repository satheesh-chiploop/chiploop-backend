import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text


def _fallback_tb(spec: dict) -> str:
    # Minimal RNM testbench scaffold (compile-friendly)
    return """\
`timescale 1ns/1ps

module tb_ldo_behavioral;

  // Simple RNM-style connections (adjust if your model interface differs)
  logic EN;
  real  VIN;
  real  VOUT;

  // Instantiate the behavioral model (expects module name 'model' and ports EN, VIN, VOUT)
  model dut (
    .EN(EN),
    .VIN(VIN),
    .VOUT(VOUT)
  );

  // Basic stimulus
  initial begin
    EN  = 1'b0;
    VIN = 0.0;
    #100;

    // Enable and ramp VIN
    VIN = 1.3;
    #100;
    EN = 1'b1;
    #200;

    // DC-ish sweep (placeholder)
    repeat (15) begin
      VIN = VIN + 0.05;
      #200;
      $display("[TB] t=%0t VIN=%0.3f VOUT=%0.3f", $time, VIN, VOUT);
    end

    // Transient-ish step (placeholder)
    VIN = 2.0;
    #200;
    $display("[TB] transient placeholder: VIN step done. t=%0t VIN=%0.3f VOUT=%0.3f", $time, VIN, VOUT);

    #1000;
    $finish;
  end

endmodule
"""


def run_agent(state: dict) -> dict:
    agent_name = "Analog Behavioral Testbench Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    sim_plan = state.get("analog_sim_plan") or {}

    if not workflow_id or not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing workflow_id or analog_spec"
        return state

    prompt = f"""
You are a SystemVerilog RNM verification engineer.

Generate a minimal SystemVerilog testbench (plain text, not markdown) for the analog behavioral model.

Assumptions:
- Behavioral model file is analog/model.sv
- Model module is named: model
- It has ports (as applicable): EN, VIN, VOUT

Use scenarios and sweeps from this sim plan if present:
{json.dumps(sim_plan, indent=2)}

Use spec:
{json.dumps(spec, indent=2)}

Output: full TB code, compile-friendly.
Include:
- enable sequencing
- basic stimulus: DC-ish VIN sweep loop + transient placeholder
- print key signals/metrics placeholders
Return ONLY code (no markdown fences).
"""

    tb = (llm_text(prompt) or "").strip()
    if not tb:
        tb = _fallback_tb(spec)

    state["analog_tb"] = tb

    if not preview_only:
        # New canonical path
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog/behavioral",
            filename="tb_ldo_behavioral.sv",
            content=tb,
        )

        # Legacy compatibility
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="tb.sv",
            content=tb,
        )

    return state