import os
import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load


def run_agent(state: dict) -> dict:
    print("\n🔬 Running Analog Spec Builder Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")

    analog_dir = os.path.join(workflow_dir, "analog")
    spec_dir = os.path.join(analog_dir, "spec")

    os.makedirs(spec_dir, exist_ok=True)

    datasheet = (
        state.get("datasheet_text")
        or state.get("analog_datasheet")
        or state.get("analog_spec_text")
        or state.get("spec_text")
        or state.get("spec")
        or state.get("description")
        or ""
    ).strip()

  

    if not datasheet:
        raise ValueError("Analog datasheet not provided")

    print(f"[DEBUG] analog state keys = {list(state.keys())}")
    print(f"[DEBUG] datasheet_text present = {bool(state.get('datasheet_text'))}")
    print(f"[DEBUG] analog_datasheet present = {bool(state.get('analog_datasheet'))}")
    print(f"[DEBUG] spec present = {bool(state.get('spec'))}")
    print(f"[DEBUG] spec_text present = {bool(state.get('spec_text'))}")

    prompt = f"""
You are an analog design architect.

Convert the following datasheet into a normalized JSON spec.

Return JSON only.

Fields required:
- block_name
- module_name
- description
- ports [name, direction, width]
- behavior_summary
- behavioral_contract
- deliverables

Datasheet:
{datasheet}
"""

    raw = llm_text(prompt)
    spec = safe_json_load(raw)

    if not spec:
        raise RuntimeError("LLM failed to produce valid analog spec")


    block = (spec.get("block_name") or "analog_block").strip()
    module_name = (spec.get("module_name") or block).strip()

    # Canonical naming rule:
    # preserve explicit module_name from spec if present;
    # otherwise default module_name = block_name
    spec["block_name"] = block
    spec["module_name"] = module_name


    spec_path = os.path.join(spec_dir, "spec_normalized.json")

    with open(spec_path, "w") as f:
        json.dump(spec, f, indent=2)

    save_text_artifact_and_record(
        workflow_id,
        "Analog Spec Builder Agent",
        "analog/spec",
        "spec_normalized.json",
        json.dumps(spec, indent=2),
    )

    state["analog_spec"] = spec
    state["analog_block_name"] = block

    # Publish canonical analog module signature for downstream integration
    analog_signature = {
        spec["module_name"]: {
            "ports": spec.get("ports", [])
        }
    }

    state["analog_module_signature"] = analog_signature
    state["analog_signatures"] = analog_signature
    state["analog_rtl_signatures"] = analog_signature

    print(f"✅ Analog spec generated for {block}")

    return state