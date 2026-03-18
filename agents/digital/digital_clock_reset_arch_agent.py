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
    return spec["name"] if mode == "flat" else spec["hierarchy"]["top_module"]["name"]


def _pick_ports(spec: dict, mode: str) -> List[dict]:
    return _extract_ports_flat(spec) if mode == "flat" else _extract_ports_hier(spec)


def _pick_clock_reset_from_spec(spec: dict) -> Dict[str, Any]:
    oc = spec.get("operating_constraints", {}) if isinstance(spec, dict) else {}
    clocks = oc.get("clock_domains", []) if isinstance(oc.get("clock_domains", []), list) else []
    resets = oc.get("reset_signals", []) if isinstance(oc.get("reset_signals", []), list) else []

    out_clocks = []
    for c in clocks:
        if isinstance(c, dict) and c.get("name"):
            freq = c.get("frequency_mhz")
            period = c.get("period_ns")
            if period is None and freq:
                period = 1000.0 / float(freq)
            if freq is None and period:
                freq = 1000.0 / float(period)
            out_clocks.append({
                "name": c["name"],
                "frequency_mhz": float(freq) if freq is not None else 100.0,
                "period_ns": float(period) if period is not None else 10.0,
            })

    out_resets = []
    for r in resets:
        if isinstance(r, dict) and r.get("name"):
            out_resets.append({
                "name": r["name"],
                "active_low": bool(r.get("active_low", False)),
                "async": bool(r.get("async", False)),
            })

    return {"clocks": out_clocks, "resets": out_resets}


def _infer_clock_reset_from_ports(ports: List[dict]) -> Dict[str, Any]:
    clocks = []
    resets = []
    for p in ports:
        name = p.get("name", "")
        lname = name.lower()
        if "clk" in lname or "clock" in lname:
            clocks.append({
                "name": name,
                "period_ns": 10.0,
                "frequency_mhz": 100.0,
                "assumed": True,
            })
        if "rst" in lname or "reset" in lname:
            resets.append({
                "name": name,
                "active_low": bool(p.get("active_low", ("_n" in lname))),
                "async": bool(p.get("async", False)),
                "assumed": True,
            })
    if not clocks:
        clocks.append({"name": "clk", "period_ns": 10.0, "frequency_mhz": 100.0, "assumed": True})
    if not resets:
        resets.append({"name": "reset_n", "active_low": True, "async": False, "assumed": True})
    return {"clocks": clocks, "resets": resets}


def _build_sdc(top_name: str, ports: List[dict], clock_reset_arch: dict) -> str:
    clk = clock_reset_arch["clocks"][0]
    clk_name = clk["name"]
    period = float(clk["period_ns"])
    io_delay = period / 2.0
    reset_names = {r["name"] for r in clock_reset_arch.get("resets", [])}

    lines = [f"# Auto-generated SDC for {top_name}"]
    lines.append(f"create_clock -name {clk_name} -period {period} [get_ports {{{clk_name}}}]")

    for p in ports:
        pname = p["name"]
        if pname == clk_name:
            continue
        if pname in reset_names:
            continue
        direction = p.get("direction")
        width = int(p.get("width", 1) or 1)
        port_ref = f"{pname}[*]" if width > 1 else pname
        if direction in ("input", "inout"):
            lines.append(f"set_input_delay {io_delay} -clock {clk_name} [get_ports {{{port_ref}}}]")
        if direction in ("output", "inout"):
            lines.append(f"set_output_delay {io_delay} -clock {clk_name} [get_ports {{{port_ref}}}]")

    for r in clock_reset_arch.get("resets", []):
        lines.append(f"set_false_path -from [get_ports {{{r['name']}}}]")

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
    elif isinstance(state.get("spec_json"), dict):
        spec = state["spec_json"]
    else:
        state["status"] = "❌ Missing digital spec JSON."
        return state

    spec, mode = _normalize_spec(spec)
    top_name = _pick_top_name(spec, mode)
    ports = _pick_ports(spec, mode)

    spec_cr = _pick_clock_reset_from_spec(spec)
    inferred = _infer_clock_reset_from_ports(ports)

    arch = {
        "spec_mode": mode,
        "top_module": top_name,
        "clock_reset_architecture": {
            "clocks": spec_cr["clocks"] if spec_cr["clocks"] else inferred["clocks"],
            "resets": spec_cr["resets"] if spec_cr["resets"] else inferred["resets"],
        },
    }

    arch_json_path = os.path.join(workflow_dir, "digital", "clock_reset_arch_intent.json")
    os.makedirs(os.path.dirname(arch_json_path), exist_ok=True)
    with open(arch_json_path, "w", encoding="utf-8") as f:
        json.dump(arch, f, indent=2)

    sdc_name = f"{top_name}.sdc"
    sdc_text = _build_sdc(top_name, ports, arch["clock_reset_architecture"])
    sdc_path = os.path.join(workflow_dir, "digital", "constraints", sdc_name)
    os.makedirs(os.path.dirname(sdc_path), exist_ok=True)
    with open(sdc_path, "w", encoding="utf-8") as f:
        f.write(sdc_text)

    try:
        save_text_artifact_and_record(
            workflow_id, agent_name, "digital", "clock_reset_arch_intent.json",
            open(arch_json_path, "r", encoding="utf-8").read()
        )
        save_text_artifact_and_record(
            workflow_id, agent_name, "digital", sdc_name,
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