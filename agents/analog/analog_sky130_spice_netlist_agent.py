import json
import os
import re
from typing import Dict, List

from model_gateway import complete_text
from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Analog Sky130 SPICE Netlist Agent"


def _enabled(state: dict) -> bool:
    mode = str(state.get("analog_physical_mode") or "").strip().lower()
    pdk = str(state.get("analog_pdk") or state.get("pdk") or "").strip().lower()
    return mode in {"generate_sky130_gds", "sky130_gds"} or (mode == "generate_gds" and pdk.startswith("sky130"))


def _module_name(state: dict) -> str:
    spec = state.get("analog_spec") if isinstance(state.get("analog_spec"), dict) else {}
    contract = state.get("analog_macro_interface_contract") if isinstance(state.get("analog_macro_interface_contract"), dict) else {}
    return str(
        state.get("analog_macro_module")
        or spec.get("module_name")
        or spec.get("block_name")
        or contract.get("macro_name")
        or "analog_macro"
    ).strip()


def _ports(state: dict) -> List[str]:
    spec = state.get("analog_spec") if isinstance(state.get("analog_spec"), dict) else {}
    raw_ports = spec.get("ports") if isinstance(spec, dict) else []
    ports = []
    if isinstance(raw_ports, list):
        for port in raw_ports:
            if isinstance(port, dict) and port.get("name"):
                ports.append(str(port["name"]).strip())
            elif isinstance(port, str) and port.strip():
                ports.append(port.strip())
    return list(dict.fromkeys([p for p in ports if p]))


def _contract_ports(state: dict) -> List[str]:
    contract = state.get("analog_macro_interface_contract") if isinstance(state.get("analog_macro_interface_contract"), dict) else {}
    raw_ports = contract.get("ports") if isinstance(contract.get("ports"), list) else []
    ports = []
    for port in raw_ports:
        if isinstance(port, dict) and port.get("name"):
            ports.append(str(port["name"]).strip())
    return list(dict.fromkeys([p for p in ports if p]))


def _candidate_texts(state: dict) -> List[tuple[str, str]]:
    candidates: List[tuple[str, str]] = []
    for key in ("analog_spice_text", "analog_netlist_text", "spice_text"):
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append((key, value.strip() + "\n"))

    for key in ("analog_spice_path", "analog_netlist_path", "netlist_file"):
        value = state.get(key)
        if isinstance(value, str) and value.strip() and os.path.exists(value):
            try:
                with open(value, "r", encoding="utf-8", errors="ignore") as fh:
                    candidates.append((key, fh.read()))
            except Exception:
                pass
    return candidates


def _has_real_devices(text: str) -> bool:
    device_re = re.compile(r"^\s*[mrcx][A-Za-z0-9_:$.-]*\s+", re.IGNORECASE | re.MULTILINE)
    return bool(device_re.search(text or ""))


def _has_subckt(text: str) -> bool:
    return bool(re.search(r"^\s*\.subckt\s+\S+", text or "", flags=re.IGNORECASE | re.MULTILINE))


def _sky130_include() -> str:
    return '.include "$PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice"\n.lib "$PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice" tt\n'


def _normalise_sky130_spice(text: str, module_name: str, ports: List[str]) -> str:
    body = text.strip() + "\n"
    if "sky130.lib.spice" not in body.lower():
        body = _sky130_include() + "\n" + body
    if not _has_subckt(body):
        pin_text = " ".join(ports) if ports else "vin vout vdd vss"
        body = f".subckt {module_name} {pin_text}\n" + body + f".ends {module_name}\n"
    if not re.search(r"^\s*\.end\s*$", body, flags=re.IGNORECASE | re.MULTILINE):
        body += ".end\n"
    return body


def _strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _generation_context(state: dict) -> dict:
    spec = state.get("analog_spec") if isinstance(state.get("analog_spec"), dict) else {}
    contract = state.get("analog_macro_interface_contract") if isinstance(state.get("analog_macro_interface_contract"), dict) else {}
    text = str(state.get("analog_spec_text") or state.get("analog_datasheet") or "").strip()
    return {"analog_spec": spec, "analog_macro_interface_contract": contract, "analog_spec_text": text}


def _generate_sky130_spice_with_llm(state: dict, module_name: str, ports: List[str]) -> str:
    ctx = _generation_context(state)
    if not ctx["analog_spec"] and not ctx["analog_macro_interface_contract"] and not ctx["analog_spec_text"]:
        return ""

    prompt = f"""
Generate a real transistor-level Sky130 SPICE subcircuit for analog GDS generation with ALIGN.

Module/subckt name:
{module_name}

Required port order:
{json.dumps(ports, indent=2)}

Available analog spec JSON:
{json.dumps(ctx["analog_spec"], indent=2)}

Available macro interface contract JSON:
{json.dumps(ctx["analog_macro_interface_contract"], indent=2)}

Analog spec text, if any:
{ctx["analog_spec_text"]}

Strict requirements:
- Return SPICE text only. No Markdown.
- Must include exactly one .subckt named {module_name}.
- Must include transistor-level MOS devices using sky130_fd_pr__nfet_01v8 and/or sky130_fd_pr__pfet_01v8.
- Preserve the required port order in the .subckt line.
- Include explicit W and L parameters on MOS devices.
- Do not emit placeholder comments instead of devices.
- Do not emit only R/C/load scaffolding.
- End with .ends {module_name}.
"""
    return _strip_code_fences(complete_text(
        prompt,
        capability="analog_generation",
        agent_name=AGENT_NAME,
        state=state,
    ))


def run_agent(state: dict) -> dict:
    print(f"\nRunning {AGENT_NAME}...")
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    out_dir = os.path.join(workflow_dir, "analog", "sky130")
    os.makedirs(out_dir, exist_ok=True)

    if not _enabled(state):
        state["analog_sky130_spice"] = {"status": "skipped", "reason": "analog_physical_mode_not_generate_sky130_gds"}
        state["status"] = f"{AGENT_NAME}: skipped"
        return state

    module_name = _module_name(state)
    ports = _ports(state) or _contract_ports(state)
    selected_source = ""
    selected_text = ""
    for source, text in _candidate_texts(state):
        if _has_real_devices(text):
            selected_source = source
            selected_text = text
            break
    if not selected_text:
        generated_text = _generate_sky130_spice_with_llm(state, module_name, ports)
        if generated_text and _has_real_devices(generated_text):
            selected_source = "generated_by_sky130_spice_agent"
            selected_text = generated_text

    summary: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "pdk": "sky130A",
        "module": module_name,
        "source": selected_source,
    }

    if not selected_text:
        summary.update({
            "status": "failed",
            "reason": "real_transistor_level_spice_missing",
            "note": "Analog GDS generation requires real transistor-level SPICE. The SPICE generator did not produce valid device-level SPICE.",
        })
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/sky130", "sky130_spice_summary.json", json.dumps(summary, indent=2))
        state["analog_sky130_spice"] = summary
        state["status"] = f"{AGENT_NAME}: failed"
        raise RuntimeError("Analog GDS generation requires real transistor-level SPICE; no valid device-level SPICE was provided.")

    spice = _normalise_sky130_spice(selected_text, module_name, ports)
    spice_path = os.path.join(out_dir, f"{module_name}.spice")
    with open(spice_path, "w", encoding="utf-8") as fh:
        fh.write(spice)

    summary.update({
        "status": "ready",
        "spice": spice_path,
        "relpath": f"analog/sky130/{module_name}.spice",
        "device_level": True,
        "generated": selected_source == "generated_by_sky130_spice_agent",
    })
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/sky130", f"{module_name}.spice", spice)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/sky130", "sky130_spice_summary.json", json.dumps(summary, indent=2))

    state["analog_spice_path"] = spice_path
    state["analog_netlist_path"] = spice_path
    state["analog_sky130_spice"] = summary
    state["status"] = f"{AGENT_NAME}: ready"
    return state
