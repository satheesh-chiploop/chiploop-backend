import json
import os
import re
from typing import Any, Dict, List

from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Analog Sky130 SPICE Netlist Agent"


def _enabled(state: dict) -> bool:
    mode = str(state.get("analog_physical_mode") or "").strip().lower()
    pdk = str(state.get("analog_pdk") or state.get("pdk") or "").strip().lower()
    return mode in {"generate_sky130_gds", "sky130_gds"} or (mode == "generate_gds" and pdk.startswith("sky130"))


def _module_name(state: dict) -> str:
    spec = state.get("analog_spec") if isinstance(state.get("analog_spec"), dict) else {}
    return str(
        state.get("analog_macro_module")
        or spec.get("module_name")
        or spec.get("block_name")
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


def _port_entries(state: dict) -> List[Dict[str, Any]]:
    spec = state.get("analog_spec") if isinstance(state.get("analog_spec"), dict) else {}
    raw_ports = spec.get("ports") if isinstance(spec, dict) else []
    out: List[Dict[str, Any]] = []
    if isinstance(raw_ports, list):
        for port in raw_ports:
            if isinstance(port, dict) and port.get("name"):
                out.append({
                    "name": str(port.get("name") or "").strip(),
                    "direction": str(port.get("direction") or "").strip().lower(),
                    "width": port.get("width") or 1,
                })
            elif isinstance(port, str) and port.strip():
                out.append({"name": port.strip(), "direction": "inout", "width": 1})
    return [p for p in out if p.get("name")]


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


def _role(name: str) -> str:
    low = name.lower()
    if low in {"vdd", "avdd", "dvdd", "vpwr"} or "vdd" in low or "pwr" in low:
        return "power"
    if low in {"vss", "avss", "dvss", "gnd", "vgnd"} or "vss" in low or "gnd" in low:
        return "ground"
    if "clk" in low:
        return "clock"
    if "rst" in low or "reset" in low:
        return "reset"
    return "signal"


def _safe_node(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_$]", "_", str(name or "").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "n"


def _generate_spice_from_spec(state: dict, module_name: str, ports: List[str]) -> str:
    spec = state.get("analog_spec") if isinstance(state.get("analog_spec"), dict) else {}
    spec_text = str(state.get("analog_spec_text") or state.get("analog_datasheet") or "").strip()
    if not spec and not spec_text:
        return ""

    entries = _port_entries(state)
    if not ports:
        ports = [p["name"] for p in entries]
    if not ports:
        ports = ["vin", "vout", "vdd", "vss"]
        entries = [
            {"name": "vin", "direction": "input", "width": 1},
            {"name": "vout", "direction": "output", "width": 1},
            {"name": "vdd", "direction": "inout", "width": 1},
            {"name": "vss", "direction": "inout", "width": 1},
        ]

    power = next((p for p in ports if _role(p) == "power"), "vdd")
    ground = next((p for p in ports if _role(p) == "ground"), "vss")
    signal_inputs = [p["name"] for p in entries if p.get("direction") == "input" and _role(p["name"]) == "signal"]
    signal_outputs = [p["name"] for p in entries if p.get("direction") == "output" and _role(p["name"]) == "signal"]
    control = signal_inputs[0] if signal_inputs else next((p for p in ports if _role(p) == "signal"), "vin")

    lines = [
        _sky130_include().strip(),
        "",
        f"* ChipLoop generated first-pass Sky130 transistor-level SPICE for {module_name}.",
        "* This netlist is generated from the analog spec for layout exploration; replace with designer SPICE for signoff.",
        f".subckt {module_name} {' '.join(ports)}",
        ".param WN=1u WP=2u LMIN=0.15u",
    ]
    for idx, out_name in enumerate(signal_outputs or ["generated_probe"]):
        out_node = _safe_node(out_name)
        ctrl_node = _safe_node(control)
        mid_node = f"n_auto_{idx}"
        lines.extend([
            f"* Auto-generated CMOS stage for {out_name}",
            f"MNA{idx} {out_node} {ctrl_node} {ground} {ground} sky130_fd_pr__nfet_01v8 W={{WN}} L={{LMIN}}",
            f"MPA{idx} {out_node} {ctrl_node} {power} {power} sky130_fd_pr__pfet_01v8 W={{WP}} L={{LMIN}}",
            f"CLOAD{idx} {out_node} {ground} 5f",
            f"RKEEP{idx} {out_node} {mid_node} 1Meg",
            f"CKEEP{idx} {mid_node} {ground} 1f",
        ])
    if not signal_outputs:
        lines.extend([
            f"MNAUX {_safe_node(control)} {_safe_node(control)} {ground} {ground} sky130_fd_pr__nfet_01v8 W={{WN}} L={{LMIN}}",
            f"MPAUX {_safe_node(control)} {_safe_node(control)} {power} {power} sky130_fd_pr__pfet_01v8 W={{WP}} L={{LMIN}}",
        ])
    lines.extend([f".ends {module_name}", ".end", ""])
    return "\n".join(lines)


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
    ports = _ports(state)
    selected_source = ""
    selected_text = ""
    for source, text in _candidate_texts(state):
        if _has_real_devices(text):
            selected_source = source
            selected_text = text
            break
    if not selected_text:
        generated_text = _generate_spice_from_spec(state, module_name, ports)
        if generated_text and _has_real_devices(generated_text):
            selected_source = "generated_from_analog_spec"
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
            "status": "deferred",
            "reason": "real_transistor_level_spice_missing",
            "note": "Analog GDS generation requires transistor-level SPICE. No analog spec or provided real-device SPICE was available.",
        })
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/sky130", "sky130_spice_summary.json", json.dumps(summary, indent=2))
        state["analog_sky130_spice"] = summary
        state["status"] = f"{AGENT_NAME}: deferred"
        return state

    spice = _normalise_sky130_spice(selected_text, module_name, ports)
    spice_path = os.path.join(out_dir, f"{module_name}.spice")
    with open(spice_path, "w", encoding="utf-8") as fh:
        fh.write(spice)

    summary.update({
        "status": "ready",
        "spice": spice_path,
        "relpath": f"analog/sky130/{module_name}.spice",
        "device_level": True,
        "generated": selected_source == "generated_from_analog_spec",
    })
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/sky130", f"{module_name}.spice", spice)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/sky130", "sky130_spice_summary.json", json.dumps(summary, indent=2))

    state["analog_spice_path"] = spice_path
    state["analog_netlist_path"] = spice_path
    state["analog_sky130_spice"] = summary
    state["status"] = f"{AGENT_NAME}: ready"
    return state
