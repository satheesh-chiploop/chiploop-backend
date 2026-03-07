import os
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text


def run_agent(state: dict) -> dict:
    print("\n🧪 Running Analog Behavioral TB Agent...")

    workflow_id = state.get("workflow_id")
    workflow_dir = state.get("workflow_dir")

    spec = state.get("analog_spec", {})
    block = spec.get("block_name", "analog_block")

    prompt = f"""
Generate a SystemVerilog testbench for module {block}_model.

Use this spec:

{spec}

Rules:

- Instantiate {block}_model
- Declare signals for all ports
- Drive simple stimulus
- End simulation after short time
"""

    tb = llm_text(prompt)

    tb_dir = os.path.join(workflow_dir, "analog", "behavioral")
    os.makedirs(tb_dir, exist_ok=True)

    filename = f"tb_{block}_behavioral.sv"
    path = os.path.join(tb_dir, filename)

    with open(path, "w") as f:
        f.write(tb)

    save_text_artifact_and_record(
        workflow_id,
        "Analog Behavioral TB Agent",
        "analog/behavioral",
        filename,
        tb,
    )

    state["analog_tb"] = path

    return state