import json
from pathlib import Path
from typing import Any, Dict

from model_gateway import complete_text


ALLOWED_TARGETS = {"cpu_software", "gpu_service", "firmware", "fpga", "asic", "fpga_or_asic", "software"}

DEPLOYMENT_ARCHITECTURES = {
    "automatic",
    "fpga_onboard_cpu",
    "fpga_soft_cpu",
    "fpga_external_host",
    "asic_digital_ip",
    "asic_soc",
    "asic_companion",
}


def _target_refinement(req: Dict[str, Any]) -> Dict[str, Any]:
    """Describe what must be resolved after functional partitioning.

    Board selection is deliberately not performed here: FPGA Explorer owns that
    decision after RTL resource and I/O evidence exists.
    """
    path = str(req.get("implementation_path") or "digital_ip_asic")
    requested = str(req.get("deployment_architecture") or "automatic")
    if requested not in DEPLOYMENT_ARCHITECTURES:
        requested = "automatic"
    is_fpga = path in {"fpga_prototype", "fpga_then_asic"}
    candidates = (
        ["fpga_onboard_cpu", "fpga_soft_cpu", "fpga_external_host"]
        if is_fpga
        else ["asic_digital_ip", "asic_soc", "asic_companion"]
    )
    selected = requested if requested != "automatic" else None
    soft_cpu = req.get("soft_cpu") if isinstance(req.get("soft_cpu"), dict) else {}
    asic_cpu = req.get("asic_cpu") if isinstance(req.get("asic_cpu"), dict) else {}
    return {
        "status": "pending_board_selection" if is_fpga else ("pending_asic_architecture" if selected is None else "selected"),
        "requested_mode": requested,
        "selected_mode": selected,
        "candidate_modes": candidates,
        "selection_owner": "fpga_target_explorer" if is_fpga else "application_intelligence",
        "soft_cpu": soft_cpu if selected == "fpga_soft_cpu" else None,
        "asic_cpu": asic_cpu if selected == "asic_soc" else None,
        "firmware_gate": {
            "ready": False,
            "reason": "Finalize CPU/host, bus, address map, interrupts, and register-map version before deployable firmware generation.",
        },
        "required_contract": {
            "compute_host": ["location", "cpu_or_mcu", "architecture", "runtime_or_os", "toolchain"],
            "hardware_interface": ["bus_or_transport", "base_address", "register_map", "interrupts", "dma_or_shared_memory"],
            "target": ["board_and_device" if is_fpga else "asic_integration_mode", "clock_reset", "memory", "io"],
        },
    }


def _json_object(text: str) -> Dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("partitioning model must return one JSON object")
    return value


def _syntax_repair_prompt(output: str, error: json.JSONDecodeError) -> str:
    start = max(0, error.pos - 240)
    end = min(len(output), error.pos + 240)
    return f"""You are repairing JSON syntax only.
Return one complete JSON object and no markdown or explanation.
Preserve every partition job, interface, decision, factor, tradeoff, field, and value.
Fix only JSON syntax such as a missing colon/comma, quote, bracket, or truncated wrapper.

JSON ERROR: {error.msg} at line {error.lineno}, column {error.colno}, character {error.pos}
ERROR CONTEXT:
{output[start:end]}

PREVIOUS RESPONSE:
{output}
"""


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    req = state["requirements_contract"]
    model = state["selected_physics_model"]
    execution = state["physics_execution"]
    baseline = {
        "schema": "chiploop.application_intelligence.partition.v2",
        "application": req["application"],
        "decision": "heterogeneous",
        "jobs": [
            {"id": "surrogate_inference", "target": "gpu_service", "status": "optional_pending_runtime" if execution.get("inference_status") != "executed" else "qualified", "model_id": model["model_id"], "responsibility": "high-fidelity physics inference and design exploration"},
            {"id": "policy_generation", "target": "cpu_software", "status": "reference_ready", "responsibility": "convert qualified/reference operating points into a bounded deployable policy"},
            {"id": "device_control", "target": "fpga_or_asic", "status": "ready_for_rtl", "responsibility": "deterministic filtering, policy evaluation, safety limits, watchdog, and actuator commands"},
            {"id": "device_management", "target": "firmware", "status": "planned", "responsibility": "configuration, policy loading, diagnostics, and update control"},
            {"id": "product_application", "target": "software", "status": "planned", "responsibility": "monitoring, configuration, visualization, and product workflow"},
        ],
        "interfaces": [
            {"from": "cpu_software", "to": "fpga_or_asic", "contract": "versioned policy/LUT and control registers"},
            {"from": "fpga_or_asic", "to": "firmware", "contract": "status, faults, telemetry, and interrupt"},
        ],
        "implementation_path": req.get("implementation_path"),
        "partition_phases": {
            "functional": {"status": "completed", "target_independent": True},
            "target_refinement": _target_refinement(req),
        },
        "decision_factors": ["latency", "determinism", "power", "cost", "accuracy", "interface fit", "runtime availability"],
    }
    if not bool(state.get("generate_architecture_with_model", False)):
        return {**state, "partition_plan": {**baseline, "generation_status": "deterministic_mode"}}

    prompt = f"""You are ChipLoop's hardware/software partitioning agent.
Partition the supplied application workloads around the selected physics model and generated architecture.
First perform target-independent functional partitioning. The surrogate stays in GPU/CPU software unless evidence explicitly proves it is hardware-deployable.
Do not assume that an FPGA board has a CPU. Treat onboard hard CPU, soft CPU, and external host as distinct deployment modes.
For ASIC distinguish reusable Digital IP, an SoC with an embedded CPU, and a companion ASIC controlled by an external processor.
Return JSON only with keys: decision, jobs, interfaces, decision_factors, tradeoffs.
Every job must have id, target, status, responsibility, inputs, outputs, latency_budget, and rationale.
Allowed targets: {sorted(ALLOWED_TARGETS)}. Use the requested implementation path. Do not choose a specific FPGA board.

APPLICATION CONTRACT:
{json.dumps(state.get("application_contract") or {}, indent=2, default=str)}

MODEL QUALIFICATION:
{json.dumps(state.get("surrogate_mapping") or {}, indent=2, default=str)}

ARCHITECTURE:
{json.dumps(state.get("generated_architecture") or execution.get("architecture") or {}, indent=2, default=str)}

IMPLEMENTATION PATH: {req.get("implementation_path")}
"""
    try:
        root = Path(state["artifact_dir"])
        root.mkdir(parents=True, exist_ok=True)
        raw_path = root / "model_generated_partition_raw.txt"
        repaired_raw_path = root / "model_generated_partition_repaired_raw.txt"
        output = complete_text(
            prompt,
            capability="planner",
            agent_name="Hardware Software Partitioning Agent",
            state=state,
        )
        raw_path.write_text(output, encoding="utf-8")
        try:
            generated = _json_object(output)
        except json.JSONDecodeError as parse_error:
            repaired_output = complete_text(
                _syntax_repair_prompt(output, parse_error),
                capability="planner",
                agent_name="Hardware Software Partitioning Agent",
                state=state,
            )
            repaired_raw_path.write_text(repaired_output, encoding="utf-8")
            generated = _json_object(repaired_output)
        jobs = generated.get("jobs")
        if not isinstance(jobs, list) or not jobs:
            raise ValueError("partitioning response has no jobs")
        for job in jobs:
            if not isinstance(job, dict) or str(job.get("target")) not in ALLOWED_TARGETS:
                raise ValueError("partitioning response contains an invalid target")
        partition = {
            **baseline,
            **generated,
            "application": req["application"],
            "implementation_path": req.get("implementation_path"),
            "partition_phases": baseline["partition_phases"],
            "generation_status": "model_generated",
            "generation_artifacts": {
                "raw": str(raw_path),
                "syntax_repaired_raw": str(repaired_raw_path) if repaired_raw_path.exists() else None,
            },
        }
    except Exception as exc:
        raise RuntimeError(f"Hardware Software Partitioning Agent model generation failed: {type(exc).__name__}: {exc}") from exc
    return {**state, "partition_plan": partition}
