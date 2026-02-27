import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load


def run_agent(state: dict) -> dict:
    agent_name = "Analog Behavioral Model Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    if not workflow_id or not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing workflow_id or analog_spec"
        return state

    # Production: deterministic artifact name + style (downstream expects model.sv)
    model_style = "sv_rnm"

    prompt = f"""
You are an analog modeling engineer.

Create a behavioral model for this block spec:
{json.dumps(spec, indent=2)}

Target style: {model_style}

Return ONLY JSON (no markdown) with:
{{
  "model_style": "{model_style}",
  "filename": "model.sv",
  "code": ".... full code ....",
  "parameters": {{"PARAM": 1.0}},
  "notes": ["...limitations...", "...assumptions..."]
}}

Rules:
- Keep it compile-friendly and minimal.
- Use parameters for key behaviors.
- If the spec is an LDO, implement a simplified closed-loop regulator:
  - EN handling (if present)
  - dropout knee behavior (approx)
  - load current effect (approx)
  - optional PSRR shaping (approx, not silicon-grade)
- No fake precision: document limitations.
"""

    out = llm_text(prompt)
    obj = safe_json_load(out)
    if not isinstance(obj, dict):
        obj = {}

    code = (obj.get("code") or "").strip()
    params = obj.get("parameters") or {}
    notes = obj.get("notes") or []

    filename = "model.sv"  # deterministic

    state["analog_model_code"] = code
    state["analog_model_params"] = params
    state["analog_model_style"] = model_style

    if not code:
        # fallback minimal compile-friendly SV RNM stub
        code = """\
module model (
  input  logic EN,
  input  real  VIN,
  output real  VOUT
);
  parameter real VOUT_NOM = 1.2;
  parameter real DROPOUT_V = 0.12;   // engineering-level placeholder
  parameter real GAIN = 1.0;         // placeholder
  parameter real ILOAD_A = 0.0;      // optional external drive via tb

  real vreg;

  always @(*) begin
    if (!EN) begin
      vreg = 0.0;
    end else begin
      // crude dropout behavior: clamp VOUT <= VIN - DROPOUT_V
      vreg = VOUT_NOM;
      if (VIN < (VOUT_NOM + DROPOUT_V)) begin
        vreg = (VIN - DROPOUT_V);
        if (vreg < 0.0) vreg = 0.0;
      end
    end
  end

  assign VOUT = vreg;
endmodule
"""

    if not preview_only:
        save_text_artifact_and_record(workflow_id, agent_name, "analog", filename, code)
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "model_params.json", json.dumps(params, indent=2))
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="model_notes.md",
            content="# Model Notes\n\n" + ("\n".join([f"- {n}" for n in notes]) or "- (none)") + "\n",
        )

    return state