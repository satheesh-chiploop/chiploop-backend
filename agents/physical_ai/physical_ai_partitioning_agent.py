import json
from typing import Any, Dict

from model_gateway import complete_text


ALLOWED_TARGETS = {"cpu_software", "gpu_service", "firmware", "fpga", "asic", "fpga_or_asic", "software"}


def _json_object(text: str) -> Dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("partitioning model must return one JSON object")
    return value


def run_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    req = state["requirements_contract"]
    model = state["selected_physics_model"]
    execution = state["physics_execution"]
    fallback = {
        "schema": "chiploop.application_intelligence.partition.v1",
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
        "decision_factors": ["latency", "determinism", "power", "cost", "accuracy", "interface fit", "runtime availability"],
    }
    if not bool(state.get("generate_architecture_with_model", False)):
        return {**state, "partition_plan": fallback}

    prompt = f"""You are ChipLoop's hardware/software partitioning agent.
Partition the supplied application workloads around the selected physics model and generated architecture.
The surrogate stays in GPU/CPU software unless evidence explicitly proves it is hardware-deployable.
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
        generated = _json_object(complete_text(
            prompt,
            capability="planner",
            agent_name="Hardware Software Partitioning Agent",
            state=state,
        ))
        jobs = generated.get("jobs")
        if not isinstance(jobs, list) or not jobs:
            raise ValueError("partitioning response has no jobs")
        for job in jobs:
            if not isinstance(job, dict) or str(job.get("target")) not in ALLOWED_TARGETS:
                raise ValueError("partitioning response contains an invalid target")
        partition = {
            **fallback,
            **generated,
            "application": req["application"],
            "implementation_path": req.get("implementation_path"),
            "generation_status": "model_generated",
        }
    except Exception as exc:
        partition = {**fallback, "generation_status": "deterministic_fallback", "generation_warning": f"{type(exc).__name__}: {exc}"}
    return {**state, "partition_plan": partition}
