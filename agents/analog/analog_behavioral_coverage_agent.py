import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load


def _normalize_process_corners(proc_list):
    if not isinstance(proc_list, list):
        return []
    mapped = []
    for p in proc_list:
        if not isinstance(p, str):
            continue
        pl = p.lower().strip()
        if pl in ("tt", "typ", "typical"):
            mapped.append("TYP")
        elif pl in ("ss", "slow"):
            mapped.append("SLOW")
        elif pl in ("ff", "fast"):
            mapped.append("FAST")
        else:
            mapped.append(p)
    out = []
    for x in mapped:
        if x not in out:
            out.append(x)
    return out


def run_agent(state: dict) -> dict:
    agent_name = "Analog Behavioral Coverage Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    if not workflow_id or not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing workflow_id or analog_spec"
        return state

    corners = spec.get("corners") or {}
    if not isinstance(corners, dict):
        corners = {}
    norm_corners = {
        "vdd": corners.get("vdd") if isinstance(corners.get("vdd"), list) else [],
        "temp_c": corners.get("temp_c") if isinstance(corners.get("temp_c"), list) else [],
        "process": _normalize_process_corners(corners.get("process")),
    }

    prompt = f"""
You are defining coverage for analog behavioral validation.

Coverage here means:
- scenario coverage (modes, enable/disable, load_step, fault)
- corner coverage (vdd/temp/process) using PDK-agnostic naming
- sweep coverage classes (dc/ac/tran)

Return ONLY JSON:
{{
  "scenario_coverage": ["..."],
  "corner_coverage": {{"vdd":[], "temp_c":[], "process":[]}},
  "sweep_coverage": ["dc","ac","tran"],
  "notes": ["..."]
}}

Use spec:
{json.dumps(spec, indent=2)}

Use normalized corners (PDK-agnostic naming):
{json.dumps(norm_corners, indent=2)}
"""

    out = llm_text(prompt)
    cov = safe_json_load(out)
    if not isinstance(cov, dict):
        cov = {}

    # enforce normalized corners in output if missing
    corner_cov = cov.get("corner_coverage")
    if not isinstance(corner_cov, dict):
        corner_cov = {}
    corner_cov.setdefault("vdd", norm_corners["vdd"])
    corner_cov.setdefault("temp_c", norm_corners["temp_c"])
    corner_cov.setdefault("process", norm_corners["process"])
    cov["corner_coverage"] = corner_cov

    state["analog_coverage_plan"] = cov

    if not preview_only:
        # New scaffold
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "behavioral/coverage_plan.json", json.dumps(cov, indent=2))
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="behavioral/coverage_summary.md",
            content="# Validation Coverage Summary\n\n" + json.dumps(cov, indent=2) + "\n",
        )

        # Legacy compatibility
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "coverage_plan.json", json.dumps(cov, indent=2))
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="analog",
            filename="validation_summary.md",
            content="# Validation Coverage Summary\n\n" + json.dumps(cov, indent=2) + "\n",
        )

    return state