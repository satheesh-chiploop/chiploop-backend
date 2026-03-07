import os
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text


def run_agent(state: dict) -> dict:
    print("\n🧪 Running Analog Behavioral Model Agent...")


    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)


    spec = state.get("analog_spec", {})

    if not spec:
        raise ValueError("analog_spec missing. Run Analog Spec Builder Agent first.")


    block = spec.get("block_name", "analog_block")

    prompt = f"""
Generate a SystemVerilog behavioral model.

Use this spec:

{spec}

Rules:

- Module name = {block}_model
- Use the ports defined in spec
- Do not invent new ports
- Do not change widths
- Keep behavioral model simple
"""

    model = llm_text(prompt)

    model_dir = os.path.join(workflow_dir, "analog")
    os.makedirs(model_dir, exist_ok=True)

    filename = "model.sv"
    path = os.path.join(model_dir, filename)

    with open(path, "w") as f:
        f.write(model)

    save_text_artifact_and_record(
        workflow_id,
        "Analog Behavioral Model Agent",
        "analog",
        filename,
        model,
    )

    state["analog_model"] = path

    return state