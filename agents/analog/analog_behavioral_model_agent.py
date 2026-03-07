import os
import re
import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text


def _extract_sv_module(text: str) -> str:
    if not text:
        return ""

    cleaned = (
        text.replace("```systemverilog", "")
        .replace("```sv", "")
        .replace("```verilog", "")
        .replace("```", "")
        .strip()
    )

    m = re.search(r"(module\b.*?endmodule)", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return cleaned.strip()


def _fallback_model(spec: dict, module_name: str) -> str:
    ports = spec.get("ports") or []

    decls = []
    for p in ports:
        name = p.get("name", "sig")
        direction = p.get("direction", "input")
        width = int(p.get("width", 1) or 1)
        if width > 1:
            decls.append(f"    {direction} logic [{width-1}:0] {name}")
        else:
            decls.append(f"    {direction} logic {name}")

    port_text = ",\n".join(decls) if decls else "    input logic clk"

    return f"""module {module_name} (
{port_text}
);

  // Spec-driven fallback behavioral scaffold.
  // Replace with richer semantics if LLM output is unavailable.

endmodule
"""


def run_agent(state: dict) -> dict:
    print("\\n🧪 Running Analog Behavioral Model Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    spec = state.get("analog_spec", {})
    if not spec:
        raise ValueError("analog_spec missing. Run Analog Spec Builder Agent first.")

    block = spec.get("block_name", "analog_block")
    module_name = spec.get("module_name") or f"{block}_model"

    prompt = f"""
Generate a SystemVerilog behavioral model.

Use this normalized spec:

{json.dumps(spec, indent=2)}

Rules:
- Module name must be exactly: {module_name}
- Use ONLY the ports defined in spec
- Do NOT invent new ports
- Do NOT change widths
- Return ONLY compilable SystemVerilog code
- No markdown fences
- No prose or explanations
"""

    raw = llm_text(prompt)
    model = _extract_sv_module(raw)

    if not model or "module" not in model or "endmodule" not in model:
        model = _fallback_model(spec, module_name)

    analog_dir = os.path.join(workflow_dir, "analog")
    os.makedirs(analog_dir, exist_ok=True)

    model_path = os.path.join(analog_dir, "model.sv")
    params_path = os.path.join(analog_dir, "model_params.json")
    notes_path = os.path.join(analog_dir, "model_notes.md")

    model_params = {
        "block_name": block,
        "module_name": module_name,
        "ports": spec.get("ports", []),
        "behavioral_contract": spec.get("behavioral_contract", {}),
    }

    model_notes = f"""# Analog Model Notes

- block_name: {block}
- module_name: {module_name}
- source: Analog Behavioral Model Agent
- note: model.sv is the canonical behavioral model for Analog_Run and System_Sim
"""

    with open(model_path, "w", encoding="utf-8") as f:
        f.write(model)
    with open(params_path, "w", encoding="utf-8") as f:
        json.dump(model_params, f, indent=2)
    with open(notes_path, "w", encoding="utf-8") as f:
        f.write(model_notes)

    save_text_artifact_and_record(workflow_id, "Analog Behavioral Model Agent", "analog", "model.sv", model)
    save_text_artifact_and_record(workflow_id, "Analog Behavioral Model Agent", "analog", "model_params.json", json.dumps(model_params, indent=2))
    save_text_artifact_and_record(workflow_id, "Analog Behavioral Model Agent", "analog", "model_notes.md", model_notes)

    state["analog_model"] = model_path
    state["analog_model_path"] = model_path
    state["analog_model_module"] = module_name

    return state