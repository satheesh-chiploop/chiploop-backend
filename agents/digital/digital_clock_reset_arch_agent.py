import os
import json
from typing import Optional, List, Dict, Any
from utils.artifact_utils import save_text_artifact_and_record


def _log(path: str, msg: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(msg.rstrip() + "\n")
    print(msg)


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_json_in_state_or_workflow(state: dict, candidates: List[str], workflow_dir: str) -> Optional[str]:
    for k in candidates:
        v = state.get(k)
        if isinstance(v, str) and v.endswith(".json") and os.path.exists(v):
            return v
    return None


def _normalize_spec(spec: dict) -> (dict, str):
    if isinstance(spec.get("hierarchy"), dict):
        return spec, "hierarchical"
    if spec.get("name") and spec.get("rtl_output_file"):
        return spec, "flat"
    raise ValueError("Invalid digital spec JSON form.")


def _extract_ports_flat(spec: dict) -> List[dict]:
    return spec.get("ports", [])


def _extract_ports_hier(spec: dict) -> List[dict]:
    return spec.get("hierarchy", {}).get("top_module", {}).get("ports", [])


def _pick_top_name(spec: dict, mode: str) -> str:
    if mode == "flat":
        return spec["name"]
    return spec["hierarchy"]["top_module"]["name"]


def _pick_ports(spec: dict, mode: str) -> List[dict]:
    if mode == "flat":
        return _extract_ports_flat(spec)
    return _extract_ports_hier(spec)


def _infer_clock_reset(ports: List[dict]) -> Dict[str, Any]:
    clocks = []
    resets = []

    for p in ports:
        name = p.get("name", "")
        width = p.get("width", 1)

        lname = name.lower()
        if "clk" in lname or "clock" in lname:
            clocks.append({
                "name": name,
                "period_ns": 10.0,
                "frequency_mhz": 100.0,
                "width": width,
            })

        if "rst" in lname or "reset" in lname:
            resets.append({
                "name": name,
                "active_low": bool(p.get("active_low", ("_n" in lname))),
                "async": bool(p.get("async", False)),
                "width": width,
            })

    if not clocks:
        clocks.append({
            "name": "clk",
            "period_ns": 10.0,
            "frequency_mhz": 100.0,
            "width": 1,
            "assumed": True,
        })

    if not resets:
        resets.append({
            "name": "reset_n",
            "active_low": True,
            "async": False,
            "width": 1,
            "assumed": True,
        })

    return {"clocks": clocks, "resets": resets}


def _build_sdc(top_name: str, clock_reset_arch: dict) -> str:
    lines = []
    lines.append(f"# Auto-generated SDC for {top_name}")
    for clk in clock_reset_arch.get("clocks", []):
        clk_name = clk["name"]
        period = clk.get("period_ns", 10.0)
        lines.append(f"create_clock -name {clk_name} -period {period} [get_ports {{{clk_name}}}]")
    return "\n".join(lines) + "\n"


def run_agent(state: dict) -> dict:
    agent_name = "Digital Clock & Reset Architecture Agent"
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    log_path = os.path.join(workflow_dir, "digital_clock_reset_arch_agent.log")
    open(log_path, "w", encoding="utf-8").close()

    spec_path = _find_json_in_state_or_workflow(
        state,
        candidates=["digital_spec_json", "spec_json"],
        workflow_dir=workflow_dir,
    )

    if spec_path:
        spec = _load_json(spec_path)
        _log(log_path, f"Loaded spec JSON from: {spec_path}")
    elif isinstance(state.get("digital_spec_json"), dict):
        spec = state["digital_spec_json"]
        _log(log_path, "Loaded spec JSON from state['digital_spec_json'] (dict).")
    elif isinstance(state.get("spec_json"), dict):
        spec = state["spec_json"]
        _log(log_path, "Loaded spec JSON from state['spec_json'] (dict).")
    else:
        _log(log_path, "ERROR: Could not locate digital spec JSON.")
        save_text_artifact_and_record(
            workflow_id,
            agent_name,
            "digital",
            "digital_clock_reset_arch_agent.log",
            open(log_path, "r", encoding="utf-8").read()
        )
        state["digital_clock_reset_arch_error"] = "missing_spec_json"
        state["status"] = "❌ Missing digital spec JSON."
        return state

    try:
        spec, mode = _normalize_spec(spec)
    except Exception as e:
        _log(log_path, f"ERROR: invalid digital spec JSON: {e}")
        state["status"] = f"❌ Invalid digital spec JSON: {e}"
        return state

    top_name = _pick_top_name(spec, mode)
    ports = _pick_ports(spec, mode)

    arch = {
        "spec_mode": mode,
        "top_module": top_name,
        "clock_reset_architecture": _infer_clock_reset(ports),
    }

    arch_json_path = os.path.join(workflow_dir, "digital", "clock_reset_arch_intent.json")
    os.makedirs(os.path.dirname(arch_json_path), exist_ok=True)
    with open(arch_json_path, "w", encoding="utf-8") as f:
        json.dump(arch, f, indent=2)

    sdc_text = _build_sdc(top_name, arch["clock_reset_architecture"])
    sdc_path = os.path.join(workflow_dir, "digital", "constraints", "top.sdc")
    os.makedirs(os.path.dirname(sdc_path), exist_ok=True)
    with open(sdc_path, "w", encoding="utf-8") as f:
        f.write(sdc_text)

    _log(log_path, f"Generated clock/reset intent JSON: {arch_json_path}")
    _log(log_path, f"Generated SDC: {sdc_path}")

    try:
        save_text_artifact_and_record(
            workflow_id, agent_name, "digital", "clock_reset_arch_intent.json",
            open(arch_json_path, "r", encoding="utf-8").read()
        )
        save_text_artifact_and_record(
            workflow_id, agent_name, "digital", "top.sdc",
            open(sdc_path, "r", encoding="utf-8").read()
        )
        save_text_artifact_and_record(
            workflow_id, agent_name, "digital", "digital_clock_reset_arch_agent.log",
            open(log_path, "r", encoding="utf-8").read()
        )
    except Exception as e:
        _log(log_path, f"WARNING: artifact upload failed: {e}")

    state["clock_reset_arch_path"] = arch_json_path
    state["sdc_path"] = sdc_path
    state["digital_sdc_path"] = sdc_path
    state["status"] = "✅ Clock/reset architecture generated."
    return state
