import os
import re
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text


def _strip_dut_module_if_present(tb: str, module_name: str) -> str:
    """
    Remove an accidentally generated DUT module definition while preserving
    the actual testbench module if present.
    """
    if not tb:
        return tb

    pattern = rf"module\s+{re.escape(module_name)}\b.*?endmodule"
    cleaned = re.sub(pattern, "", tb, flags=re.DOTALL | re.IGNORECASE)

    # Remove accidental markdown fences too
    cleaned = cleaned.replace("```systemverilog", "").replace("```sv", "").replace("```", "")
    return cleaned.strip()


def run_agent(state: dict) -> dict:
    print("\n🧪 Running Analog Behavioral TB Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    spec = state.get("analog_spec", {})

    if not spec:
        raise ValueError("analog_spec missing. Run Analog Spec Builder Agent first.")


    block = spec.get("block_name", "analog_block")
    module_name = spec.get("module_name") or f"{block}_model"

    prompt = f"""
Generate a SystemVerilog TESTBENCH ONLY for module {module_name}.

Use this spec:

{spec}

Rules:
- Output only one testbench module
- Do NOT define or redeclare module {module_name}
- Do NOT generate the DUT RTL
- Instantiate {module_name} as dut
- Declare signals for all DUT ports
- Drive simple generic stimulus only on DUT inputs
- Do not drive DUT outputs
- End simulation after a short time
"""
    
    tb = llm_text(prompt)
    tb = _strip_dut_module_if_present(tb, module_name)

    tb_lower = tb.lower()
    if (
        not tb
        or "endmodule" not in tb_lower
        or f"{module_name.lower()} dut" not in tb_lower
    ):
        raise RuntimeError(f"Generated TB is invalid or missing DUT instantiation for {module_name}")


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