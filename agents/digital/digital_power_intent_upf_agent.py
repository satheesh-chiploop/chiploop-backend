import os
import json
from typing import Dict, Any, List, Optional
from utils.artifact_utils import save_text_artifact_and_record


def _read_json(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.endswith(".json") and os.path.exists(v):
        with open(v, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _normalize_spec(spec: Dict[str, Any]) -> (Dict[str, Any], str):
    if isinstance(spec.get("hierarchy"), dict):
        return spec, "hierarchical"
    if spec.get("name") and spec.get("rtl_output_file"):
        return spec, "flat"
    raise ValueError("Invalid digital spec JSON form.")


def _pick_top(spec: Dict[str, Any], state_top: Optional[str]) -> str:
    if isinstance(spec.get("hierarchy"), dict):
        top_obj = spec["hierarchy"].get("top_module")
        if isinstance(top_obj, dict) and top_obj.get("name"):
            return str(top_obj["name"]).strip()

    if spec.get("name"):
        return str(spec["name"]).strip()

    if state_top:
        return state_top

    return "top"


def _find_power_intent(spec: Dict[str, Any], top_name: str) -> Dict[str, Any]:
    intent = spec.get("power_intent") or spec.get("power") or spec.get("upf") or {}
    domains: List[Dict[str, Any]] = []

    spec_domains = intent.get("domains")
    if isinstance(spec_domains, list) and spec_domains:
        for d in spec_domains:
            if isinstance(d, dict) and d.get("name"):
                domains.append({
                    "name": d["name"],
                    "elements": d.get("elements") or d.get("instances") or [top_name],
                    "primary_power_net": d.get("primary_power_net") or d.get("vdd") or "VDD",
                    "primary_ground_net": d.get("primary_ground_net") or d.get("vss") or "VSS",
                })

    if not domains:
        domains = [{
            "name": "PD_TOP",
            "elements": [top_name],
            "primary_power_net": "VDD",
            "primary_ground_net": "VSS",
        }]

    isolation = intent.get("isolation") if isinstance(intent.get("isolation"), list) else []
    retention = intent.get("retention") if isinstance(intent.get("retention"), list) else []

    return {
        "domains": domains,
        "isolation": isolation,
        "retention": retention,
    }


def _build_upf(top: str, findings: Dict[str, Any]) -> str:
    lines = []
    lines.append(f"# UPF-lite for {top}")

    lines.append("create_supply_port VDD")
    lines.append("create_supply_port VSS")
    lines.append("create_supply_net VDD")
    lines.append("create_supply_net VSS")
    lines.append("connect_supply_net VDD -ports VDD")
    lines.append("connect_supply_net VSS -ports VSS")

    for d in findings.get("domains", []):
        dname = d["name"]
        lines.append(f"create_power_domain {dname} -elements {{{' '.join(d.get('elements', [top]))}}}")
        lines.append(f"set_domain_supply_net {dname} -primary_power_net {d.get('primary_power_net', 'VDD')} -primary_ground_net {d.get('primary_ground_net', 'VSS')}")

    return "\n".join(lines) + "\n"


def run_agent(state: dict) -> dict:
    agent_name = "Digital Power Intent (UPF-Lite) Agent"
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    spec_path = state.get("digital_spec_json") or state.get("spec_json")
    spec = _read_json(spec_path) if spec_path else {}

    if not spec:
        state["status"] = "❌ Missing digital spec JSON for power intent generation."
        return state

    try:
        spec, mode = _normalize_spec(spec)
    except Exception as e:
        state["status"] = f"❌ Invalid digital spec JSON for power intent generation: {e}"
        return state

    top = _pick_top(spec, state.get("top_module"))
    findings = _find_power_intent(spec, top)
    findings["spec_mode"] = mode
    findings["top_module"] = top

    upf_text = _build_upf(top, findings)

    upf_path = os.path.join(workflow_dir, "digital", "power_intent.upf")
    findings_path = os.path.join(workflow_dir, "digital", "power_intent_findings.json")
    report_path = os.path.join(workflow_dir, "digital", "power_intent_report.txt")
    os.makedirs(os.path.dirname(upf_path), exist_ok=True)

    with open(upf_path, "w", encoding="utf-8") as f:
        f.write(upf_text)

    with open(findings_path, "w", encoding="utf-8") as f:
        json.dump(findings, f, indent=2)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("Digital Power Intent (UPF-Lite) Agent Report\n")
        f.write(f"Spec mode: {mode}\n")
        f.write(f"Top module: {top}\n")
        f.write(f"Domains: {[d['name'] for d in findings.get('domains', [])]}\n")

    try:
        save_text_artifact_and_record(
            workflow_id, agent_name, "digital", "power_intent.upf",
            open(upf_path, "r", encoding="utf-8").read()
        )
        save_text_artifact_and_record(
            workflow_id, agent_name, "digital", "power_intent_findings.json",
            open(findings_path, "r", encoding="utf-8").read()
        )
        save_text_artifact_and_record(
            workflow_id, agent_name, "digital", "power_intent_report.txt",
            open(report_path, "r", encoding="utf-8").read()
        )
    except Exception as e:
        print(f"⚠️ Power intent artifact upload failed: {e}")

    state.setdefault("signoff", {})
    state["signoff"]["power_intent"] = findings
    state["status"] = "✅ Power intent generated."
    return state