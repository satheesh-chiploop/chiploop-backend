import json
from pathlib import Path
from typing import Any, Dict

from model_gateway import complete_text


def _json_object(text: str) -> Dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("architecture model must return one JSON object")
    return value


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    requirements = state["requirements_contract"]
    model = state["selected_physics_model"]
    execution = state["physics_execution"]
    if not bool(state.get("generate_architecture_with_model", False)):
        existing = execution.get("architecture") or {}
        return {**state, "generated_architecture": existing, "architecture_generation": {"status": "deterministic_test_mode"}}

    evidence = {
        "execution_mode": execution.get("execution_mode") or "validated",
        "inference_status": execution.get("inference_status") or "executed",
        "metrics": execution.get("simulation", {}).get("metrics") or execution.get("metrics") or {},
        "model_interface": execution.get("interface") or {"inputs": model.get("inputs"), "outputs": model.get("outputs")},
    }
    prompt = f"""You are a Physical AI and digital-product architect.
Create the hardware architecture around the selected physics model. The physics/surrogate model itself is not RTL.
Use only the supplied requirements, model interface, and execution evidence. If inference_status is not_executed, do not invent predictions.
Return JSON only with keys: product_name, top_module, product_summary, architecture_decisions, blocks, interfaces, safety_requirements, rtl_spec_text, verification_goals.
top_module must be a valid Verilog identifier describing this application; never reuse a motor-control top for a non-motor application.
rtl_spec_text must be a detailed synthesizable digital-IP requirement suitable for an Arch2RTL workflow.
For FPGA or ASIC implementation, the RTL top level MUST use a compact implementation-friendly interface:
- Do not expose tensors, geometry arrays, flow fields, or surrogate-model payloads as thousands of individual top-level pins.
- Use a bounded streaming data bus no wider than 128 bits with valid/ready, or an address/data register-memory window no wider than 64 bits.
- For a Sky130 ASIC path, payload storage MUST instantiate only the backend-supported hard macro `sky130_sram_1kbyte_1rw1r_32x256_8` using ports `clk0`, `csb0`, `web0`, `wmask0[3:0]`, `addr0[7:0]`, `din0[31:0]`, and `dout0[31:0]`. Bank multiple instances when more capacity is required.
- Do not infer ASIC payload memory with `reg [...] mem [...]`, generic SRAM modules, inferred BRAM, or payload FIFOs implemented as flip-flop/register arrays. Small control/status registers, pointers, and FSM state are allowed; bulk payload storage is not.
- Require the Sky130 macro's behavioral Verilog, Liberty, LEF, GDS, and SPICE collateral before ASIC implementation. If complete collateral is unavailable, stop at the memory-collateral gate rather than silently mapping storage into standard-cell registers.
- For an FPGA-only path, use the platform's BRAM abstraction instead of a Sky130 macro; never send a Sky130 hard macro into FPGA synthesis.
- Keep the estimated top-level scalar-equivalent I/O count below 256 unless the requirements explicitly demand a wider physical package.
- The pretrained surrogate executes in software/GPU; RTL implements request/response transport, buffering, validation, safety, timeout, and actuator command handling rather than the full surrogate tensor interface.

REQUIREMENTS:
{json.dumps(requirements, indent=2)}

SELECTED PHYSICS MODEL:
{json.dumps(model, indent=2, default=str)}

AVAILABLE EVIDENCE:
{json.dumps(evidence, indent=2, default=str)}
"""
    output = complete_text(prompt, capability="spec_generation", agent_name="Physical AI Architecture Agent", state=state)
    architecture = _json_object(output)
    required = {"product_name", "product_summary", "blocks", "interfaces", "rtl_spec_text", "verification_goals"}
    missing = sorted(required - set(architecture))
    if missing:
        raise ValueError(f"architecture model response missing fields: {', '.join(missing)}")
    digital_ip_spec = execution.get("digital_ip_spec") if isinstance(execution.get("digital_ip_spec"), dict) else {}
    architecture.setdefault("top_module", digital_ip_spec.get("top_module") or model.get("digital_ip_top_module"))
    root = Path(state["artifact_dir"])
    path = root / "model_generated_architecture.json"
    raw_path = root / "model_generated_architecture_raw.txt"
    path.write_text(json.dumps(architecture, indent=2, sort_keys=True), encoding="utf-8")
    raw_path.write_text(output, encoding="utf-8")
    execution = dict(execution)
    execution["architecture"] = architecture
    execution.setdefault("files", {})["model_generated_architecture"] = str(path)
    execution["files"]["model_generated_architecture_raw"] = str(raw_path)
    return {
        **state,
        "physics_execution": execution,
        "generated_architecture": architecture,
        "architecture_generation": {
            "status": "generated",
            "agent_model_policy": state.get("model_policy") or {},
            "inference_status": evidence["inference_status"],
        },
    }
