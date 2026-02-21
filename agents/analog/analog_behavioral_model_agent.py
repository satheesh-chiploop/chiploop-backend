import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load

def run_agent(state: dict) -> dict:
    agent_name = "Analog Behavioral Model Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    model_style = (state.get("model_style") or "sv_rnm").strip()  # "sv_rnm" | "verilog_a"
    if not workflow_id or not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing workflow_id or analog_spec"
        return state

    prompt = f"""
You are an analog modeling engineer.

Create a behavioral model for this block spec:
{json.dumps(spec, indent=2)}

Target style: {model_style}

Return ONLY JSON (no markdown) with:
{{
  "model_style": "{model_style}",
  "filename": "model.sv" or "model.va",
  "code": ".... full code ....",
  "parameters": {{"PARAM": 1.0}},
  "notes": ["...limitations...", "...assumptions..."]
}}

Rules:
- Keep it compile-friendly and minimal.
- Use parameters for key behaviors.
- Support enable pin if present.
"""

    out = llm_text(prompt)
    obj = safe_json_load(out)
    code = (obj.get("code") or "").strip()
    params = obj.get("parameters") or {}
    notes = obj.get("notes") or []
    filename = (obj.get("filename") or ("model.va" if model_style == "verilog_a" else "model.sv")).strip()

    state["analog_model_code"] = code
    state["analog_model_params"] = params
    state["analog_model_style"] = model_style

    if not preview_only:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename=filename,
            content=code or "// (empty model)\n",
        )
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="model_params.json",
            content=json.dumps(params, indent=2),
        )
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="model_notes.md",
            content="# Model Notes\n\n" + "\n".join([f"- {n}" for n in notes]),
        )

    return state