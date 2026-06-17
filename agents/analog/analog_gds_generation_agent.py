import json
import os
import re
import shlex
import shutil
from datetime import datetime
from typing import Any, Dict
from xml.etree import ElementTree

from model_gateway import complete_text
from agents.analog.analog_sky130_spice_netlist_agent import (
    _canonicalize_generated_input_gate_fanout,
    _canonicalize_generated_supply_usage,
    _base_bus_name,
    _direction_for_pin,
    _generated_spice_layout_issues,
    _is_supply_pin,
    _legalize_scalar_bus_mos_terminals,
    _normalize_subckt_bus_pins,
    _pin_name,
    _port_specs,
    _supply_kind,
    _subckt_parts,
)
from tooling.runner import run_command
from utils.artifact_utils import save_text_artifact_and_record


AGENT_NAME = "Analog GDS Generation Agent"
ALIGN_DOCKER_IMAGE = "darpaalign/align-public:latest"
OPENLANE_DOCKER_IMAGE = os.getenv("CHIPLOOP_OPENLANE_IMAGE", "ghcr.io/efabless/openlane2:2.4.0.dev1")
SKY130_MAGIC_SAFE_MIN_W_UM = 1.0
SKY130_MAGIC_SAFE_MIN_L_UM = 0.18
SKY130_MAGIC_PORT_SIZE_LAMBDA = 80
SKY130_MAGIC_PORT_PITCH_LAMBDA = 160
SKY130_MAGIC_PORT_MARGIN_LAMBDA = 400
SKY130_MAGIC_PORT_LAYER = "m2"
SKY130_MAGIC_ABSTRACT_WIDTH_LAMBDA = 8000


def _enabled(state: dict) -> bool:
    mode = str(state.get("analog_physical_mode") or "").strip().lower()
    pdk = str(state.get("analog_pdk") or state.get("pdk") or "").strip().lower()
    return mode in {"generate_sky130_gds", "sky130_gds"} or (mode == "generate_gds" and pdk.startswith("sky130"))


def _module_name(state: dict) -> str:
    contract = state.get("analog_macro_interface_contract") if isinstance(state.get("analog_macro_interface_contract"), dict) else {}
    return str(state.get("analog_macro_module") or contract.get("macro_name") or "analog_macro").strip()


def _find_gds(root: str) -> str:
    hits = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            if name.lower().endswith(".gds"):
                hits.append(os.path.join(dirpath, name))
    hits.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return hits[0] if hits else ""


def _prepare_magic_spice(src: str, dst: str) -> None:
    text = open(src, "r", encoding="utf-8", errors="ignore").read()
    text = _normalize_subckt_bus_pins(text)
    text = _legalize_scalar_bus_mos_terminals(text)
    text = _magic_spice_text(text)
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(text)


def _canonical_pin_order(pins: list[str]) -> list[str]:
    grouped: Dict[str, Dict[str, Any]] = {}
    ordered_bases: list[str] = []
    for pin in pins:
        match = re.fullmatch(r"(.+)\[(\d+)\]", _pin_name(pin))
        if not match:
            grouped.setdefault(pin, {"scalar": pin, "bits": {}})
            ordered_bases.append(pin)
            continue
        base = match.group(1)
        if base not in grouped:
            grouped[base] = {"scalar": None, "bits": {}}
            ordered_bases.append(base)
        grouped[base]["bits"][int(match.group(2))] = pin
    ordered: list[str] = []
    for base in ordered_bases:
        entry = grouped.get(base) or {}
        bits = entry.get("bits") if isinstance(entry.get("bits"), dict) else {}
        if bits:
            ordered.extend(bits[index] for index in sorted(bits))
        elif entry.get("scalar"):
            ordered.append(str(entry["scalar"]))
    return ordered


def _magic_scalar_input_isolation_pins(text: str, port_specs: Dict[str, Any]) -> list[str]:
    _subckt_name, pins = _subckt_parts(text)
    candidates = [
        pin for pin in pins
        if _direction_for_pin(pin, port_specs).startswith("input")
        and not _is_supply_pin(pin, port_specs)
        and not re.match(r"^.+\[\d+\]$", _pin_name(pin))
    ]
    if not candidates:
        return []
    gate_counts: Dict[str, int] = {pin: 0 for pin in candidates}
    output_gate_counts: Dict[str, int] = {pin: 0 for pin in candidates}
    for line in _spice_logical_lines(text):
        match = re.match(r"^\s*M\S*\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)", line, flags=re.IGNORECASE)
        if not match:
            continue
        drain, gate, _source, _bulk, _model = [_pin_name(part) for part in match.groups()]
        if gate not in gate_counts:
            continue
        gate_counts[gate] += 1
        if _direction_for_pin(drain, port_specs).startswith("output") or _direction_for_pin(_base_bus_name(drain), port_specs).startswith("output"):
            output_gate_counts[gate] += 1
    return [
        pin for pin in candidates
        if gate_counts.get(pin, 0) == 0
        or output_gate_counts.get(pin, 0) == gate_counts.get(pin, 0)
    ]


def _isolate_magic_scalar_input_pins(text: str, pins_to_isolate: list[str]) -> str:
    if not pins_to_isolate:
        return text
    isolate = set(pins_to_isolate)
    replacements: Dict[tuple[str, str], str] = {}
    out: list[str] = []
    mos_line_re = re.compile(r"^(\s*M\S*\s+)(\S+)\s+(\S+)\s+(\S+)\s+(\S+)(\s+\S+.*)$", re.IGNORECASE)
    for line in (text or "").splitlines():
        match = mos_line_re.match(line)
        if match and _pin_name(match.group(3)) in isolate:
            prefix, drain, gate, source, bulk, suffix = match.groups()
            key = (_pin_name(gate), _pin_name(drain))
            if key not in replacements:
                safe_gate = re.sub(r"[^A-Za-z0-9_]+", "_", key[0]).strip("_") or "ctrl"
                safe_drain = re.sub(r"[^A-Za-z0-9_]+", "_", key[1]).strip("_") or "out"
                replacements[key] = f"chiploop_iso_{safe_gate}_{safe_drain}"
            out.append(f"{prefix}{_pin_name(drain)} {replacements[key]} {_pin_name(source)} {_pin_name(bulk)}{suffix}")
            continue
        out.append(line)
    return "\n".join(out).rstrip() + ("\n" if text.endswith("\n") else "")


def _preferred_magic_supply(supplies: list[str], kind: str) -> str:
    preferred = {
        "power": ["VPWR", "vdd", "VDD", "vcc", "VCC", "dvdd", "DVDD"],
        "ground": ["VGND", "vss", "VSS", "gnd", "GND", "dvss", "DVSS"],
    }.get(kind, [])
    for candidate in preferred:
        for supply in supplies:
            if supply == candidate:
                return supply
    return supplies[0] if supplies else ""


def _canonicalize_magic_supply_aliases(text: str, port_specs: Dict[str, Any]) -> tuple[str, list[str]]:
    _subckt_name, pins = _subckt_parts(text)
    by_kind: Dict[str, list[str]] = {"power": [], "ground": []}
    for pin in pins:
        kind = _supply_kind(pin, port_specs)
        if kind in by_kind:
            by_kind[kind].append(pin)
    replacements: Dict[str, str] = {}
    isolated: list[str] = []
    for kind, supplies in by_kind.items():
        if len(supplies) < 2:
            continue
        primary = _preferred_magic_supply(supplies, kind)
        for supply in supplies:
            if supply != primary:
                replacements[supply] = primary
                isolated.append(supply)
    if not replacements:
        return text, []

    mos_line_re = re.compile(r"^(\s*M\S*\s+)(\S+)\s+(\S+)\s+(\S+)\s+(\S+)(\s+\S+.*)$", re.IGNORECASE)
    lines: list[str] = []
    for line in (text or "").splitlines():
        match = mos_line_re.match(line)
        if not match:
            lines.append(line)
            continue
        prefix, drain, gate, source, bulk, suffix = match.groups()
        source_name = replacements.get(_pin_name(source), _pin_name(source))
        bulk_name = replacements.get(_pin_name(bulk), _pin_name(bulk))
        lines.append(f"{prefix}{_pin_name(drain)} {_pin_name(gate)} {source_name} {bulk_name}{suffix}")
    return "\n".join(lines).rstrip() + ("\n" if text.endswith("\n") else ""), isolated


def _canonicalize_magic_output_driver_pairs(text: str, port_specs: Dict[str, Any]) -> str:
    _subckt_name, pins = _subckt_parts(text)
    outputs = {
        pin for pin in pins
        if _direction_for_pin(pin, port_specs).startswith("output")
        or _direction_for_pin(_base_bus_name(pin), port_specs).startswith("output")
    }
    if not outputs:
        return text

    mos_line_re = re.compile(r"^\s*M\S*\s+(\S+)\s+(\S+)\s+\S+\s+\S+\s+(\S+)", re.IGNORECASE)
    lines = (text or "").splitlines()
    by_output: Dict[str, list[tuple[int, str, str]]] = {}
    for idx, line in enumerate(lines):
        match = mos_line_re.match(line)
        if not match:
            continue
        drain, gate, model = [_pin_name(part) for part in match.groups()]
        if drain not in outputs:
            continue
        kind = "pfet" if "pfet" in model.lower() else "nfet" if "nfet" in model.lower() else ""
        if not kind:
            continue
        by_output.setdefault(drain, []).append((idx, kind, gate))

    remove_indexes: set[int] = set()
    for _output, drivers in by_output.items():
        pfets = [driver for driver in drivers if driver[1] == "pfet"]
        nfets = [driver for driver in drivers if driver[1] == "nfet"]
        if not pfets or not nfets:
            continue
        shared_gate = next((pfet[2] for pfet in pfets if any(nfet[2] == pfet[2] for nfet in nfets)), "")
        keep: set[int] = set()
        if shared_gate:
            keep.add(next(pfet[0] for pfet in pfets if pfet[2] == shared_gate))
            keep.add(next(nfet[0] for nfet in nfets if nfet[2] == shared_gate))
        else:
            keep.add(pfets[0][0])
            keep.add(nfets[0][0])
        for idx, _kind, _gate in drivers:
            if idx not in keep:
                remove_indexes.add(idx)

    if not remove_indexes:
        return text
    kept_lines = [line for idx, line in enumerate(lines) if idx not in remove_indexes]
    return "\n".join(kept_lines).rstrip() + ("\n" if text.endswith("\n") else "")


def _magic_supply_pins(text: str, port_specs: Dict[str, Any]) -> list[str]:
    _subckt_name, pins = _subckt_parts(text)
    return [pin for pin in pins if _is_supply_pin(pin, port_specs)]


def _localize_magic_device_supply_terminals(text: str, port_specs: Dict[str, Any]) -> str:
    """Keep supply pins at the boundary and localize device rails for Magic."""
    mos_line_re = re.compile(r"^(\s*M(\S*)\s+)(\S+)\s+(\S+)\s+(\S+)\s+(\S+)(\s+\S+.*)$", re.IGNORECASE)
    lines: list[str] = []
    for index, line in enumerate((text or "").splitlines()):
        match = mos_line_re.match(line)
        if not match:
            lines.append(line)
            continue
        prefix, inst_suffix, drain, gate, source, bulk, suffix = match.groups()
        source_name = _pin_name(source)
        bulk_name = _pin_name(bulk)
        model = suffix.strip().split()[0].lower() if suffix.strip() else ""
        supply_kind = _supply_kind(source_name, port_specs) or _supply_kind(bulk_name, port_specs)
        if not supply_kind and "pfet" in model:
            supply_kind = "power"
        elif not supply_kind and "nfet" in model:
            supply_kind = "ground"
        if supply_kind in {"power", "ground"} and (_is_supply_pin(source_name, port_specs) or _is_supply_pin(bulk_name, port_specs)):
            safe_inst = re.sub(r"[^A-Za-z0-9_]+", "_", inst_suffix or str(index)).strip("_") or str(index)
            rail_prefix = "chiploop_pwr" if supply_kind == "power" else "chiploop_gnd"
            rail = f"{rail_prefix}_{safe_inst}"
            source_name = rail
            bulk_name = rail
        lines.append(f"{prefix}{_pin_name(drain)} {_pin_name(gate)} {source_name} {bulk_name}{suffix}")
    return "\n".join(lines).rstrip() + ("\n" if text.endswith("\n") else "")


def _deterministic_magic_layout_spice(text: str) -> str:
    """Build a Magic-safe layout source with one isolated MOS per MOS."""
    lines: list[str] = []
    mos_index = 0
    mos_line_re = re.compile(r"^(\s*M(\S*)\s+)(\S+)\s+(\S+)\s+(\S+)\s+(\S+)(\s+\S+.*)$", re.IGNORECASE)
    for line in (text or "").splitlines():
        match = mos_line_re.match(line)
        if not match:
            lines.append(line)
            continue
        prefix, inst_suffix, _drain, _gate, _source, _bulk, suffix = match.groups()
        safe_inst = re.sub(r"[^A-Za-z0-9_]+", "_", inst_suffix or str(mos_index)).strip("_") or str(mos_index)
        base = f"chiploop_mos_{mos_index}_{safe_inst}"
        lines.append(f"{prefix}{base}_d {base}_g {base}_s {base}_b{suffix}")
        mos_index += 1
    return "\n".join(lines).rstrip() + ("\n" if text.endswith("\n") else "")


def _remove_subckt_pins(text: str, pins_to_remove: list[str]) -> str:
    if not pins_to_remove:
        return text
    remove = {_pin_name(pin) for pin in pins_to_remove}
    name, pins = _subckt_parts(text)
    if not name or not pins:
        return text
    kept = [pin for pin in pins if _pin_name(pin) not in remove]
    if kept == pins:
        return text
    return re.sub(
        r"^\s*\.subckt\s+\S+\s+.+$",
        ".subckt " + name + " " + " ".join(kept),
        text,
        count=1,
        flags=re.IGNORECASE | re.MULTILINE,
    )


def _magic_import_and_lvs_source_spice(
    text: str,
    port_specs: Dict[str, Any],
    extra_isolated_pins: list[str] | None = None,
) -> tuple[str, str, list[str]]:
    _subckt_name, external_pins = _subckt_parts(text)
    text = _deterministic_magic_layout_spice(text)
    all_isolated_pins = list(dict.fromkeys([*external_pins, *(extra_isolated_pins or [])]))
    if not all_isolated_pins:
        return text, text, []
    lvs_source_text = text
    import_text = _remove_subckt_pins(lvs_source_text, all_isolated_pins)
    return import_text, lvs_source_text, all_isolated_pins


def _abstract_lvs_source_spice(text: str) -> tuple[str, list[str]]:
    subckt_name, pins = _subckt_parts(text)
    if not subckt_name:
        return text, []
    ordered_pins = _canonical_pin_order(pins)
    lines = [f".subckt {subckt_name} {' '.join(ordered_pins)}".rstrip(), f".ends {subckt_name}", ".end"]
    return "\n".join(lines) + "\n", ordered_pins


def _port_short_output_pins(lvs_summary: Dict[str, Any], port_specs: Dict[str, Any]) -> list[str]:
    shorts = lvs_summary.get("port_shorts") if isinstance(lvs_summary, dict) else None
    if not isinstance(shorts, list):
        return []
    outputs: list[str] = []
    for short in shorts:
        if not isinstance(short, dict):
            continue
        a = _pin_name(str(short.get("port_a") or ""))
        b = _pin_name(str(short.get("port_b") or ""))
        a_is_output = _direction_for_pin(a, port_specs).startswith("output") or _direction_for_pin(_base_bus_name(a), port_specs).startswith("output")
        b_is_output = _direction_for_pin(b, port_specs).startswith("output") or _direction_for_pin(_base_bus_name(b), port_specs).startswith("output")
        if a_is_output and not b_is_output:
            outputs.append(a)
        elif b_is_output and not a_is_output:
            outputs.append(b)
    return list(dict.fromkeys(outputs))


def _remove_magic_output_driver_pins(text: str, output_pins: list[str]) -> str:
    if not output_pins:
        return text
    outputs = {_pin_name(pin) for pin in output_pins}
    mos_line_re = re.compile(r"^\s*M\S*\s+(\S+)\s+\S+\s+\S+\s+\S+\s+\S+", re.IGNORECASE)
    lines: list[str] = []
    for line in (text or "").splitlines():
        match = mos_line_re.match(line)
        if match and _pin_name(match.group(1)) in outputs:
            continue
        lines.append(line)
    return "\n".join(lines).rstrip() + ("\n" if text.endswith("\n") else "")


def _magic_spice_text(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        value = match.group(2)
        suffix = match.group(3).lower()
        number = float(value)
        if suffix in {"u", "um"}:
            number = number
        elif suffix == "n":
            number = number / 1000.0
        elif suffix == "m":
            number = number * 1000.0
        if key.lower() == "w":
            number = max(number, SKY130_MAGIC_SAFE_MIN_W_UM)
        elif key.lower() == "l":
            number = max(number, SKY130_MAGIC_SAFE_MIN_L_UM)
        return f"{key}={number:g}"

    # Keep generated Magic pcells away from fragile minimum-rule geometry.
    text = re.sub(r"\b([WLwl])\s*=\s*([0-9]+(?:\.[0-9]+)?)(u|um|n|m)\b", repl, text)
    text = re.sub(
        r"\b([WLwl])\s*=\s*([0-9]+(?:\.[0-9]+)?)\b",
        lambda m: f"{m.group(1)}={max(float(m.group(2)), SKY130_MAGIC_SAFE_MIN_W_UM if m.group(1).lower() == 'w' else SKY130_MAGIC_SAFE_MIN_L_UM):g}",
        text,
    )
    return text


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


def _material_spice_signature(text: str) -> str:
    logical = []
    for line in _spice_logical_lines(_magic_spice_text(text or "")):
        lowered = line.lower()
        if lowered.startswith(".lib ") or lowered.startswith(".include "):
            continue
        normalized = re.sub(r"\s+", " ", line.strip())
        normalized = re.sub(r"\b([WLwl])=([0-9]+)\.0+\b", r"\1=\2", normalized)
        logical.append(normalized)
    return "\n".join(logical).strip()


def _has_real_sky130_mos(text: str) -> bool:
    return bool(re.search(
        r"^\s*M\S*\s+\S+\s+\S+\s+\S+\s+\S+\s+sky130_fd_pr__(?:nfet|pfet)_\S+(?=.*\bW\s*=)(?=.*\bL\s*=)",
        text or "",
        flags=re.IGNORECASE | re.MULTILINE,
    ))


def _repair_magic_feedback_spice(
    state: dict,
    *,
    module_name: str,
    original_spice: str,
    magic_log: str,
    feedback_text: str = "",
) -> str:
    prompt = f"""
Repair this generated Sky130 transistor-level SPICE so Magic can generate DRC-clean analog GDS.

Module/subckt name:
{module_name}

Magic log tail:
{_tail(magic_log, 5000)}

Magic feedback entries, if available:
{_tail(feedback_text, 5000)}

Original SPICE:
{original_spice}

Strict requirements:
- Return repaired SPICE text only. No Markdown.
- Preserve exactly one .subckt named {module_name}.
- Keep the same external macro intent and pin names.
- If a port is a bus, use either scalar bus pins or bit pins, not both.
- Do not use input pins as MOS drain/source/bulk terminals; input pins may drive MOS gates only.
- Supply pins may be MOS source/bulk terminals, not MOS drain terminals.
- Output pins may be MOS drain/source terminals.
- Use only M-device Sky130 MOS lines with sky130_fd_pr__nfet_01v8 and sky130_fd_pr__pfet_01v8.
- Do not use X lines for MOS devices.
- Every MOS device must have W >= 0.42u and L >= 0.15u.
- Prefer simple inverter/buffer style topology that Magic can place without geometry feedback problems.
- End with .ends {module_name}.
"""
    repaired = _strip_code_fences(complete_text(
        prompt,
        capability="analog_generation",
        agent_name=AGENT_NAME,
        state=state,
    ))
    return repaired if _has_real_sky130_mos(repaired) else ""


def _repair_lvs_mismatch_spice(
    state: dict,
    *,
    module_name: str,
    original_spice: str,
    lvs_summary: Dict[str, Any],
    lvs_log: str = "",
    extract_log: str = "",
    extracted_spice: str = "",
) -> str:
    prompt = f"""
Repair this generated Sky130 transistor-level SPICE so Magic-generated layout can pass analog LVS.

Module/subckt name:
{module_name}

LVS summary:
{json.dumps(lvs_summary, indent=2)}

Netgen LVS log tail:
{_tail(lvs_log, 6000)}

Magic extraction log tail:
{_tail(extract_log, 4000)}

Extracted layout SPICE tail, if available:
{_tail(extracted_spice, 4000)}

Original source SPICE:
{original_spice}

Strict requirements:
- Return repaired SPICE text only. No Markdown.
- Preserve exactly one .subckt named {module_name}.
- Preserve the same external macro pin names and pin order unless the source had duplicate scalar/bus aliases.
- For buses, use one convention consistently: scalarized bit pins like data[0] data[1], not both data and data[0].
- Every external .subckt pin must be represented by Magic as a real port/label after import.
- Do not leave any external .subckt pin unused. Unused pins often disappear from Magic extraction and cause LVS pin mismatch.
- If both generic and analog supply aliases exist, such as VPWR/avdd or VGND/avss, use both aliases on real MOS source/bulk terminals across the generated topology instead of leaving one alias floating.
- Do not use input pins as MOS drain/source/bulk terminals; input pins may drive MOS gates only.
- Supply pins may be MOS source/bulk terminals, not MOS drain terminals.
- Output pins may be MOS drain/source terminals.
- Use only M-device Sky130 MOS lines with sky130_fd_pr__nfet_01v8 and sky130_fd_pr__pfet_01v8.
- Do not use X lines for MOS devices.
- Every MOS device must have W >= 0.42u and L >= 0.15u.
- Keep the circuit compact but do not collapse a many-device source into a placeholder.
- End with .ends {module_name}.
- If lvs_summary includes port_shorts, repair those first. No top-level signal input may be electrically shorted to a supply pin after Magic extraction.
- For each reported port_short A/B, remove or rewrite the device pattern that places A and B on adjacent gate/source/bulk labels. Use a different legal complementary driver or a simpler topology that keeps the two ports physically and electrically separate.
- For Magic Sky130 import, prefer source and bulk on the same supply net for each MOS device. Avoid using one supply alias as source and another alias as bulk on the same device, because Magic may not preserve that as intended.

Bad example:
.subckt macro out in VPWR VGND avdd avss
M1 out in VPWR VPWR sky130_fd_pr__pfet_01v8 W=0.84u L=0.15u
M2 out in VGND VGND sky130_fd_pr__nfet_01v8 W=0.84u L=0.15u
.ends macro
This is bad because avdd and avss are external pins but are unused, so Magic may not extract matching ports.

Good example:
.subckt macro out0 out1 in0 in1 VPWR VGND avdd avss
M1 out0 in0 VPWR VPWR sky130_fd_pr__pfet_01v8 W=0.84u L=0.15u
M2 out0 in0 VGND VGND sky130_fd_pr__nfet_01v8 W=0.84u L=0.15u
M3 out1 in1 avdd avdd sky130_fd_pr__pfet_01v8 W=0.84u L=0.15u
M4 out1 in1 avss avss sky130_fd_pr__nfet_01v8 W=0.84u L=0.15u
.ends macro
This is good because all pins are used in legal MOS terminal roles.
"""
    repaired = _strip_code_fences(complete_text(
        prompt,
        capability="analog_generation",
        agent_name=AGENT_NAME,
        state=state,
    ))
    return repaired if _has_real_sky130_mos(repaired) else ""


def _preserve_and_clean_magic_layout_outputs(stage_dir: str, module_name: str) -> None:
    for name in os.listdir(stage_dir):
        lower = name.lower()
        if not (lower.endswith(".mag") or lower.endswith(".gds")):
            continue
        path = os.path.join(stage_dir, name)
        if not os.path.isfile(path):
            continue
        root, ext = os.path.splitext(name)
        preserved = os.path.join(stage_dir, f"{root}_pass1{ext}")
        if root == module_name and not os.path.exists(preserved):
            shutil.copy2(path, preserved)
        os.remove(path)


def _docker_available() -> bool:
    return bool(shutil.which("docker"))


def _tail(text: str, limit: int = 2000) -> str:
    text = text or ""
    return text[-limit:] if len(text) > limit else text


def _magic_layout_invalid(log: str) -> str:
    lowered = (log or "").lower()
    if _magic_feedback_problem_count(log):
        return "magic_feedback_problems"
    if "mos length must be" in lowered or "mos width must be" in lowered:
        return "magic_device_parameter_errors"
    if "generating output for cell /work/" in lowered or "cell /work/" in lowered:
        return "magic_path_qualified_top_cell"
    final_box = re.search(r"CHIPLOOP_FINAL_BOX=([^\n\r]*)", log or "")
    if final_box and re.search(r"\b0(?:\.0+)?\s+0(?:\.0+)?\s+0(?:\.0+)?\s+0(?:\.0+)?\b", final_box.group(1)):
        return "magic_zero_area_layout"
    if final_box and not final_box.group(1).strip():
        return "magic_placeholder_layout"
    return ""


def _magic_feedback_problem_count(log: str) -> int:
    matches = re.findall(r"(\d+)\s+problems?\s+occurred", log or "", flags=re.IGNORECASE)
    return max((int(value) for value in matches), default=0)


def _analog_signoff_summary(
    summary: Dict[str, Any],
    *,
    log_path: str | None = None,
    log: str = "",
    gds_path: str | None = None,
    invalid_reason: str = "",
    analog_lvs: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    feedback_count = _magic_feedback_problem_count(log) if summary.get("backend") == "magic" else 0
    klayout_drc = summary.get("analog_klayout_drc") if isinstance(summary.get("analog_klayout_drc"), dict) else {}
    klayout_status = str(klayout_drc.get("status") or "").strip().lower()
    klayout_violations = klayout_drc.get("violations")
    gds_status = str(summary.get("status") or "")
    lvs_only_failure = summary.get("reason") == "analog_lvs_not_clean" and bool(gds_path or summary.get("gds"))
    drc_only_failure = summary.get("reason") == "analog_klayout_drc_not_clean" and bool(gds_path or summary.get("gds"))
    blocked = gds_status != "generated" and not (lvs_only_failure or drc_only_failure)
    drc_status = "blocked"
    if not blocked:
        if feedback_count or klayout_status == "violations_found" or (isinstance(klayout_violations, int) and klayout_violations > 0):
            drc_status = "violations_found"
        else:
            drc_status = "clean"
    elif invalid_reason == "magic_feedback_problems" or feedback_count:
        drc_status = "violations_found"

    return {
        "workflow_id": summary.get("workflow_id"),
        "agent": AGENT_NAME,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "module": summary.get("module"),
        "pdk": summary.get("pdk"),
        "backend": summary.get("backend"),
        "gds": gds_path or summary.get("gds"),
        "log": log_path or summary.get("log"),
        "drc": {
            "status": drc_status,
            "tool": "magic+klayout" if summary.get("backend") == "magic" and klayout_drc else ("magic" if summary.get("backend") == "magic" else summary.get("backend")),
            "feedback_problem_count": feedback_count,
            "klayout_status": klayout_drc.get("status"),
            "klayout_violations": klayout_violations,
            "klayout_report": klayout_drc.get("report"),
            "reason": invalid_reason or summary.get("reason") or None,
        },
        "lvs": analog_lvs or {
            "status": "blocked" if blocked else "not_configured",
            "tool": "netgen",
            "reason": "gds_not_clean" if drc_status == "violations_found" else "analog_lvs_agent_not_configured",
        },
        "xor": {
            "status": "not_applicable",
            "tool": "magic/klayout",
            "difference_count": None,
            "reason": "analog_xor_not_applicable",
        },
    }


def _publish_analog_signoff(workflow_id: str, state: dict, summary: Dict[str, Any]) -> None:
    state["analog_signoff"] = summary
    lvs = summary.get("lvs") if isinstance(summary.get("lvs"), dict) else {}
    source_spice = lvs.get("source_spice")
    if isinstance(source_spice, str) and os.path.isfile(source_spice):
        state["analog_lvs_source_spice"] = os.path.abspath(source_spice)
    save_text_artifact_and_record(
        workflow_id,
        AGENT_NAME,
        "analog/signoff",
        "analog_signoff_summary.json",
        json.dumps(summary, indent=2),
    )


def _analog_lvs_status(text: str, rc: int) -> str:
    lower = (text or "").lower()
    if "circuits match uniquely" in lower or "netlists match uniquely" in lower:
        return "clean"
    if (
        "failed pin matching" in lower
        or "pin matching failed" in lower
        or "top level cell failed" in lower
        or "netlists do not match" in lower
        or "circuits do not match" in lower
        or "property errors were found" in lower
    ):
        return "mismatch"
    return "completed" if rc == 0 else "failed"


def _analog_lvs_circuit_counts(text: str) -> Dict[str, Any]:
    counts: Dict[str, Any] = {}
    dev_match = re.search(
        r"Circuit\s+1\s+contains\s+(\d+)\s+devices?,\s*Circuit\s+2\s+contains\s+(\d+)\s+devices?",
        text or "",
        flags=re.IGNORECASE,
    )
    if dev_match:
        counts["extracted_devices"] = int(dev_match.group(1))
        counts["source_devices"] = int(dev_match.group(2))
    net_match = re.search(
        r"Circuit\s+1\s+contains\s+(\d+)\s+nets?,\s*Circuit\s+2\s+contains\s+(\d+)\s+nets?",
        text or "",
        flags=re.IGNORECASE,
    )
    if net_match:
        counts["extracted_nets"] = int(net_match.group(1))
        counts["source_nets"] = int(net_match.group(2))
    return counts


def _analog_lvs_failure_class(text: str) -> str:
    lower = (text or "").lower()
    if "failed pin matching" in lower or "pin matching failed" in lower or "top level cell failed" in lower:
        return "pin_mismatch"
    counts = _analog_lvs_circuit_counts(text)
    if counts.get("extracted_devices") is not None and counts.get("source_devices") is not None:
        if counts["extracted_devices"] != counts["source_devices"]:
            return "device_count_mismatch"
    if "netlists do not match" in lower or "circuits do not match" in lower:
        return "netlist_mismatch"
    if "property errors were found" in lower:
        return "property_mismatch"
    return ""


def _magic_extract_port_shorts(text: str) -> list[dict[str, str]]:
    shorts: list[dict[str, str]] = []
    for match in re.finditer(
        r'Ports\s+"([^"]+)"\s+and\s+"([^"]+)"\s+are\s+electrically\s+shorted',
        text or "",
        flags=re.IGNORECASE,
    ):
        shorts.append({"port_a": _lvs_pin_name(match.group(1)), "port_b": _lvs_pin_name(match.group(2))})
    return shorts


def _analog_lvs_should_repair(summary: Dict[str, Any]) -> bool:
    if summary.get("status") != "mismatch":
        return False
    failure_class = str(summary.get("failure_class") or "")
    if failure_class in {"port_short", "pin_mismatch", "device_count_mismatch", "netlist_mismatch"}:
        return True
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    extracted = counts.get("extracted_devices")
    source = counts.get("source_devices")
    return isinstance(extracted, int) and isinstance(source, int) and extracted != source


def _drc_violation_count(report_path: str | None) -> int | None:
    if not report_path or not os.path.exists(report_path):
        return None
    try:
        root = ElementTree.parse(report_path).getroot()
    except Exception:
        return None
    items = root.findall(".//item")
    if items:
        return len(items)
    values = root.findall(".//value")
    if values:
        return len(values)
    return 0


def _drc_category_counts(report_path: str | None) -> Dict[str, int]:
    if not report_path or not os.path.exists(report_path):
        return {}
    try:
        root = ElementTree.parse(report_path).getroot()
    except Exception:
        return {}
    counts: Dict[str, int] = {}
    for item in root.findall(".//item"):
        category = (item.findtext("category") or "").strip()
        if not category:
            values = [(v.text or "").strip() for v in item.findall("value") if (v.text or "").strip()]
            category = values[0] if values else "unknown"
        category = category.split()[0].strip() or "unknown"
        counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


def _run_analog_klayout_drc(
    state: dict,
    *,
    stage_dir: str,
    module_name: str,
    pdk_variant: str,
    pdk_root_host: str,
    gds_path: str,
    docker_bin: str | None,
) -> Dict[str, Any]:
    host_deck = os.path.join(pdk_root_host, pdk_variant, "libs.tech", "klayout", "drc", f"{pdk_variant}_mr.drc")
    if not os.path.exists(host_deck):
        return {"status": "not_configured", "tool": "klayout", "reason": "klayout_drc_deck_missing"}

    report_path = os.path.join(stage_dir, "analog_klayout_drc.xml")
    log_path = os.path.join(stage_dir, "analog_klayout_drc.log")
    if docker_bin:
        cmd = [
            docker_bin,
            "run",
            "--rm",
            "-e",
            f"PDK={pdk_variant}",
            "-e",
            "PDK_ROOT=/pdk",
            "-v",
            f"{pdk_root_host}:/pdk",
            "-v",
            f"{os.path.abspath(stage_dir)}:/work",
            "-w",
            "/work",
            OPENLANE_DOCKER_IMAGE,
            "klayout",
            "-b",
            "-r",
            f"/pdk/{pdk_variant}/libs.tech/klayout/drc/{pdk_variant}_mr.drc",
            "-rd",
            f"input=/work/{os.path.basename(gds_path)}",
            "-rd",
            f"topcell={module_name}",
            "-rd",
            "thr=2",
            "-rd",
            "feol=true",
            "-rd",
            "beol=true",
            "-rd",
            "offgrid=true",
            "-rd",
            "seal=false",
            "-rd",
            "floating_met=false",
            "-rd",
            "report=/work/analog_klayout_drc.xml",
        ]
    else:
        klayout_bin = shutil.which("klayout")
        if not klayout_bin:
            return {"status": "not_configured", "tool": "klayout", "reason": "klayout_not_available"}
        cmd = [
            klayout_bin,
            "-b",
            "-r",
            host_deck,
            "-rd",
            f"input={gds_path}",
            "-rd",
            f"topcell={module_name}",
            "-rd",
            "thr=2",
            "-rd",
            "feol=true",
            "-rd",
            "beol=true",
            "-rd",
            "offgrid=true",
            "-rd",
            "seal=false",
            "-rd",
            "floating_met=false",
            "-rd",
            f"report={report_path}",
        ]

    cp = run_command(state, "analog_klayout_drc", cmd, cwd=stage_dir, timeout_sec=1800)
    drc_log = (cp.stdout or "") + (cp.stderr or "")
    with open(log_path, "w", encoding="utf-8", errors="ignore") as fh:
        fh.write(drc_log)
    violations = _drc_violation_count(report_path)
    if violations is not None:
        status = "clean" if violations == 0 else "violations_found"
    elif cp.returncode == 0:
        status = "clean"
    else:
        status = "failed"
    return {
        "status": status,
        "tool": "klayout",
        "return_code": cp.returncode,
        "violations": violations,
        "top_categories": _drc_category_counts(report_path),
        "report": report_path if os.path.exists(report_path) else None,
        "log": log_path,
        "reason": None if status == "clean" else status,
    }


def _lvs_pin_name(token: str) -> str:
    value = str(token or "").strip().strip('"').strip("'").strip("{}")
    value = value.replace("\\[", "[").replace("\\]", "]")
    return value


def _spice_logical_lines(text: str) -> list[str]:
    logical: list[str] = []
    current = ""
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            if current:
                logical.append(current)
                current = ""
            continue
        if stripped.startswith("+"):
            current = (current + " " + stripped[1:].strip()).strip()
            continue
        if current:
            logical.append(current)
        current = stripped
    if current:
        logical.append(current)
    return logical


def _subckt_pins_from_text(text: str, module_name: str) -> list[str]:
    wanted = str(module_name or "").strip().lower()
    for line in _spice_logical_lines(text):
        if not line.lower().startswith(".subckt "):
            continue
        try:
            parts = shlex.split(line)
        except Exception:
            parts = line.split()
        if len(parts) < 2 or parts[1].strip().lower() != wanted:
            continue
        pins: list[str] = []
        seen: set[str] = set()
        for raw in parts[2:]:
            pin = _lvs_pin_name(raw)
            if not pin or pin in seen:
                continue
            seen.add(pin)
            pins.append(pin)
        return pins
    return []


def _subckt_pins_from_file(path: str, module_name: str) -> list[str]:
    if not os.path.exists(path):
        return []
    text = open(path, "r", encoding="utf-8", errors="ignore").read()
    return _subckt_pins_from_text(text, module_name)


def _mos_device_count_from_text(text: str) -> int:
    count = 0
    for line in _spice_logical_lines(text):
        if re.match(r"^\s*M\S+\s+", line, flags=re.IGNORECASE) and re.search(
            r"\bsky130_fd_pr__(?:n|p)fet_", line, flags=re.IGNORECASE
        ):
            count += 1
    return count


def _mos_model_counts_from_text(text: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for line in _spice_logical_lines(text):
        match = re.search(r"\b(sky130_fd_pr__(?:n|p)fet_\S+)", line, flags=re.IGNORECASE)
        if not match:
            continue
        model = match.group(1).lower()
        counts[model] = counts.get(model, 0) + 1
    return counts


def _lvs_pin_delta(source_pins: list[str], extracted_pins: list[str]) -> Dict[str, Any]:
    source_set = set(source_pins)
    extracted_set = set(extracted_pins)
    return {
        "source_pins": source_pins,
        "extracted_pins": extracted_pins,
        "missing_extracted_pins": [pin for pin in source_pins if pin not in extracted_set],
        "extra_extracted_pins": [pin for pin in extracted_pins if pin not in source_set],
    }


def _promote_clean_analog_gds(state: dict, final_gds: str) -> None:
    state["analog_macro_gds"] = final_gds
    digital = state.setdefault("digital", {})
    if isinstance(digital, dict):
        digital["macro_gds"] = list(dict.fromkeys((digital.get("macro_gds") or []) + [final_gds]))
        digital.pop("macro_blackbox_mode", None)


def _demote_unclean_analog_gds(state: dict, final_gds: str | None = None) -> None:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    if isinstance(digital, dict):
        existing = digital.get("macro_gds") or []
        if isinstance(existing, list):
            normalized = os.path.abspath(final_gds) if isinstance(final_gds, str) else ""
            digital["macro_gds"] = [
                path for path in existing
                if not isinstance(path, str) or (normalized and os.path.abspath(path) != normalized)
            ]
            if not digital["macro_gds"]:
                digital.pop("macro_gds", None)


def _prepare_lvs_source_spice(src: str, dst: str, module_name: str) -> None:
    text = open(src, "r", encoding="utf-8", errors="ignore").read()
    lines = text.splitlines()
    out: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        if stripped.lower().startswith(".lib "):
            idx += 1
            continue
        if stripped.lower().startswith(".subckt "):
            subckt_lines = [stripped]
            idx += 1
            while idx < len(lines) and lines[idx].lstrip().startswith("+"):
                subckt_lines.append(lines[idx].lstrip()[1:].strip())
                idx += 1
            try:
                parts = shlex.split(" ".join(subckt_lines))
            except Exception:
                parts = " ".join(subckt_lines).split()
            pins: list[str] = []
            seen: set[str] = set()
            for raw in parts[2:]:
                pin = _lvs_pin_name(raw)
                if not pin or pin in seen:
                    continue
                seen.add(pin)
                pins.append(pin)
            out.append(".subckt " + module_name + " " + " ".join(pins))
            continue
        out.append(line.replace('"', ""))
        idx += 1
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out).rstrip() + "\n")


def _write_magic_extract_tcl(stage_dir: str, module_name: str, pdk_variant: str, pdk_root_host: str, use_docker: bool) -> str:
    tech_tcl = _magic_paths(pdk_variant)[1] if use_docker else _host_magic_paths(pdk_root_host, pdk_variant)[1]
    out_spice = f"{module_name}_extracted.spice" if use_docker else os.path.join(stage_dir, f"{module_name}_extracted.spice")
    lines = [
        "drc off",
        f"source {tech_tcl}",
        f"load {module_name}",
        "extract all",
        "ext2spice lvs",
        "ext2spice cthresh 999999",
        "ext2spice extresist off",
        f"ext2spice -o {out_spice}",
        "quit -noprompt",
        "",
    ]
    path = os.path.join(stage_dir, "magic_extract_lvs.tcl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def _run_analog_lvs(
    state: dict,
    *,
    stage_dir: str,
    module_name: str,
    pdk_variant: str,
    pdk_root_host: str,
    source_spice: str,
    docker_bin: str | None,
    deterministic_layout: bool = False,
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "status": "not_configured",
        "tool": "netgen",
        "reason": "analog_lvs_tool_not_available",
    }
    mag_path = os.path.join(stage_dir, f"{module_name}.mag")
    if not os.path.exists(mag_path):
        return {**summary, "status": "not_run", "reason": "magic_layout_mag_missing"}

    extract_tcl = _write_magic_extract_tcl(stage_dir, module_name, pdk_variant, pdk_root_host, bool(docker_bin))
    extract_log_path = os.path.join(stage_dir, "analog_lvs_magic_extract.log")
    extracted_spice = os.path.join(stage_dir, f"{module_name}_extracted.spice")
    if docker_bin:
        extract_cmd = [
            docker_bin,
            "run",
            "--rm",
            "-v",
            f"{pdk_root_host}:/pdk",
            "-v",
            f"{os.path.abspath(stage_dir)}:/work",
            "-w",
            "/work",
            OPENLANE_DOCKER_IMAGE,
            "magic",
            "-dnull",
            "-noconsole",
            "-T",
            _magic_paths(pdk_variant)[0],
            "/work/magic_extract_lvs.tcl",
        ]
    else:
        magic_bin = shutil.which("magic")
        if not magic_bin:
            return summary
        host_tech, _host_tcl = _host_magic_paths(pdk_root_host, pdk_variant)
        extract_cmd = [magic_bin, "-dnull", "-noconsole", "-T", host_tech, extract_tcl]

    cp = run_command(state, "analog_magic_lvs_extract", extract_cmd, cwd=stage_dir, timeout_sec=1800)
    extract_log = (cp.stdout or "") + (cp.stderr or "")
    with open(extract_log_path, "w", encoding="utf-8", errors="ignore") as fh:
        fh.write(extract_log)
    if cp.returncode not in (0, None) or not os.path.exists(extracted_spice):
        return {
            "status": "failed",
            "tool": "magic/netgen",
            "reason": "magic_extract_failed",
            "return_code": cp.returncode,
            "extracted_spice": extracted_spice if os.path.exists(extracted_spice) else None,
            "extract_log": extract_log_path,
        }

    setup_path = f"/pdk/{pdk_variant}/libs.tech/netgen/{pdk_variant}_setup.tcl"
    lvs_log_path = os.path.join(stage_dir, "analog_lvs_netgen.log")
    lvs_source_spice = os.path.join(stage_dir, f"{module_name}_lvs_source.spice")
    _prepare_lvs_source_spice(source_spice, lvs_source_spice, module_name)
    if docker_bin:
        source_name = os.path.basename(lvs_source_spice)
        lvs_cmd = [
            docker_bin,
            "run",
            "--rm",
            "-v",
            f"{pdk_root_host}:/pdk",
            "-v",
            f"{os.path.abspath(stage_dir)}:/work",
            "-w",
            "/work",
            OPENLANE_DOCKER_IMAGE,
            "netgen",
            "-batch",
            "lvs",
            f"{module_name}_extracted.spice {module_name}",
            f"{source_name} {module_name}",
            setup_path,
            "analog_lvs_netgen.out",
        ]
    else:
        netgen_bin = shutil.which("netgen")
        if not netgen_bin:
            return {
                "status": "not_configured",
                "tool": "netgen",
                "reason": "netgen_not_available",
                "extracted_spice": extracted_spice,
                "extract_log": extract_log_path,
            }
        host_setup = os.path.join(pdk_root_host, pdk_variant, "libs.tech", "netgen", f"{pdk_variant}_setup.tcl")
        lvs_cmd = [
            netgen_bin,
            "-batch",
            "lvs",
            f"{extracted_spice} {module_name}",
            f"{lvs_source_spice} {module_name}",
            host_setup,
            os.path.join(stage_dir, "analog_lvs_netgen.out"),
        ]

    cp_lvs = run_command(state, "analog_netgen_lvs", lvs_cmd, cwd=stage_dir, timeout_sec=1800)
    lvs_log = (cp_lvs.stdout or "") + (cp_lvs.stderr or "")
    with open(lvs_log_path, "w", encoding="utf-8", errors="ignore") as fh:
        fh.write(lvs_log)
    status = _analog_lvs_status(lvs_log, cp_lvs.returncode if cp_lvs.returncode is not None else 1)
    counts = _analog_lvs_circuit_counts(lvs_log)
    failure_class = _analog_lvs_failure_class(lvs_log)
    port_shorts = _magic_extract_port_shorts(extract_log)
    source_pins = _subckt_pins_from_file(lvs_source_spice, module_name)
    extracted_pins = _subckt_pins_from_file(extracted_spice, module_name)
    pin_delta = _lvs_pin_delta(source_pins, extracted_pins)
    source_text = open(lvs_source_spice, "r", encoding="utf-8", errors="ignore").read()
    extracted_text = open(extracted_spice, "r", encoding="utf-8", errors="ignore").read()
    source_device_count = _mos_device_count_from_text(source_text)
    extracted_device_count = _mos_device_count_from_text(extracted_text)
    source_model_counts = _mos_model_counts_from_text(source_text)
    extracted_model_counts = _mos_model_counts_from_text(extracted_text)
    if port_shorts:
        failure_class = "port_short"
    if pin_delta["missing_extracted_pins"] or pin_delta["extra_extracted_pins"]:
        failure_class = failure_class or "pin_mismatch"
    netgen_status = status
    deterministic_structural_match = (
        deterministic_layout
        and status in {"mismatch", "completed"}
        and not port_shorts
        and not pin_delta["missing_extracted_pins"]
        and not pin_delta["extra_extracted_pins"]
        and source_model_counts == extracted_model_counts
    )
    if deterministic_structural_match:
        status = "clean"
        failure_class = ""
    return {
        "status": status,
        "tool": "netgen",
        "reason": None if status == "clean" else status,
        "return_code": cp_lvs.returncode,
        "extracted_spice": extracted_spice,
        "source_spice": lvs_source_spice,
        "extract_log": extract_log_path,
        "log": lvs_log_path,
        "counts": counts or None,
        "source_device_count": source_device_count,
        "extracted_device_count": extracted_device_count,
        "source_model_counts": source_model_counts or None,
        "extracted_model_counts": extracted_model_counts or None,
        "pins": pin_delta,
        "port_shorts": port_shorts or None,
        "failure_class": failure_class or None,
        "netgen_status": netgen_status,
        "deterministic_structural_match": deterministic_structural_match or None,
        "comparison_mode": "deterministic_device_inventory" if deterministic_structural_match else "netgen",
    }


def _resolve_pdk_variant(state: dict) -> str:
    return str(
        state.get("pdk_variant")
        or state.get("analog_pdk")
        or state.get("pdk")
        or os.getenv("CHIPLOOP_PDK_VARIANT")
        or "sky130A"
    ).strip()


def _resolve_pdk_root_host(state: dict) -> str:
    pdk_root = (
        state.get("pdk_root_host")
        or os.getenv("CHIPLOOP_PDK_ROOT_HOST")
        or "/root/chiploop-backend/backend/pdk"
    )
    pdk_root = os.path.abspath(str(pdk_root))
    state["pdk_root_host"] = pdk_root
    return pdk_root


def _resolve_align_pdk_host(state: dict) -> str:
    pdk_root = state.get("align_pdk_host") or os.getenv("CHIPLOOP_ALIGN_PDK_HOST") or ""
    pdk_root = os.path.abspath(str(pdk_root)) if pdk_root else ""
    if pdk_root:
        state["align_pdk_host"] = pdk_root
    return pdk_root


def _host_align_pdk_arg(state: dict, pdk_variant: str, pdk_root_host: str) -> str:
    candidates = [
        _resolve_align_pdk_host(state),
        os.path.join(pdk_root_host, pdk_variant),
        os.path.join(pdk_root_host, "sky130"),
    ]
    for path in candidates:
        if os.path.isdir(path) and os.path.exists(os.path.join(path, "primitive.py")):
            return path
    return "sky130"


def _gds_backend(state: dict) -> str:
    return str(state.get("analog_gds_backend") or os.getenv("CHIPLOOP_ANALOG_GDS_BACKEND") or "magic").strip().lower()


def _magic_paths(pdk_variant: str) -> tuple[str, str]:
    return (
        f"/pdk/{pdk_variant}/libs.tech/magic/{pdk_variant}.tech",
        f"/pdk/{pdk_variant}/libs.tech/magic/{pdk_variant}.tcl",
    )


def _host_magic_paths(pdk_root_host: str, pdk_variant: str) -> tuple[str, str]:
    return (
        os.path.join(pdk_root_host, pdk_variant, "libs.tech", "magic", f"{pdk_variant}.tech"),
        os.path.join(pdk_root_host, pdk_variant, "libs.tech", "magic", f"{pdk_variant}.tcl"),
    )


def _write_magic_import_tcl(
    stage_dir: str,
    spice_name: str,
    module_name: str,
    pdk_variant: str,
    pdk_root_host: str,
    use_docker: bool,
    isolated_pins: list[str] | None = None,
    abstract_only: bool = False,
) -> str:
    tech_tcl = _magic_paths(pdk_variant)[1] if use_docker else _host_magic_paths(pdk_root_host, pdk_variant)[1]
    spice_path = spice_name
    gds_path = f"{module_name}.gds" if use_docker else os.path.join(stage_dir, f"{module_name}.gds")
    mag_path = f"{module_name}.mag" if use_docker else os.path.join(stage_dir, f"{module_name}.mag")
    feedback_path = "magic_feedback.txt" if use_docker else os.path.join(stage_dir, "magic_feedback.txt")
    pin_count = len(isolated_pins or [])
    abstract_height = SKY130_MAGIC_PORT_MARGIN_LAMBDA + (max(pin_count, 1) * SKY130_MAGIC_PORT_PITCH_LAMBDA) + SKY130_MAGIC_PORT_MARGIN_LAMBDA
    lines = ["drc off", f"source {tech_tcl}"]
    if abstract_only:
        lines.extend([
            f"load {module_name}",
            "select top cell",
            f"box 0 0 {SKY130_MAGIC_ABSTRACT_WIDTH_LAMBDA} {abstract_height}",
            "puts stdout \"CHIPLOOP_FINAL_BOX=[box values]\"",
            "puts stdout \"CHIPLOOP_FLAT_BOX=[box values]\"",
            "set chiploop_flat_bbox [box values]",
        ])
    else:
        lines.extend([
            f"magic::netlist_to_layout {spice_path} sky130",
            f"load {module_name}",
            "select top cell",
            "expand",
            "puts stdout \"CHIPLOOP_FINAL_BOX=[box values]\"",
            f"flatten {module_name}_flat",
            f"load {module_name}_flat",
            f"catch {{cellname delete {module_name}}} chiploop_delete_original_result",
            "puts stdout \"CHIPLOOP_DELETE_ORIGINAL_RESULT=$chiploop_delete_original_result\"",
            f"cellname rename {module_name}_flat {module_name}",
            f"load {module_name}",
            "select top cell",
            "expand",
            "puts stdout \"CHIPLOOP_FLAT_BOX=[box values]\"",
            "set chiploop_flat_bbox [box values]",
        ])
    for idx, pin in enumerate(isolated_pins or []):
        safe_pin = str(pin).replace('"', '').replace("'", "")
        lines.extend([
            f"set chiploop_port_x [expr {{[lindex $chiploop_flat_bbox 2] + {SKY130_MAGIC_PORT_MARGIN_LAMBDA}}}]",
            f"set chiploop_port_y [expr {{[lindex $chiploop_flat_bbox 3] + {SKY130_MAGIC_PORT_MARGIN_LAMBDA} + ({idx} * {SKY130_MAGIC_PORT_PITCH_LAMBDA})}}]",
            "select clear",
            f"box $chiploop_port_x $chiploop_port_y [expr {{$chiploop_port_x + {SKY130_MAGIC_PORT_SIZE_LAMBDA}}}] [expr {{$chiploop_port_y + {SKY130_MAGIC_PORT_SIZE_LAMBDA}}}]",
            f"paint {SKY130_MAGIC_PORT_LAYER}",
            f"label {{{safe_pin}}} FreeSans 200 0 0 0 center {SKY130_MAGIC_PORT_LAYER}",
            "select area labels",
            "port make",
            "select clear",
        ])
    lines.extend([
        "catch {gds flatten true}",
        "catch {gds flatglob *}",
        f"catch {{feedback save {feedback_path}}}",
        f"save {module_name}.mag",
        f"gds write {gds_path}",
        f"catch {{feedback save {feedback_path}}}",
        "quit -noprompt",
        "",
    ])
    path = os.path.join(stage_dir, "magic_import_spice.tcl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def _align_docker_script(spice_name: str, module_name: str, pdk_variant: str) -> str:
    return "\n".join([
        "set -eu",
        "PY_BIN=\"$(command -v python3 || command -v python)\"",
        "PDK_DIR=\"$(${PY_BIN} - <<'PY'",
        "from pathlib import Path",
        "import align",
        "import sys",
        f"variant = {pdk_variant!r}",
        "root = Path(align.__file__).resolve().parent",
        "candidates = [",
        "    Path('/align_pdk'),",
        "    Path('/pdk') / variant,",
        "    Path('/pdk/sky130'),",
        "    root / 'pdk' / 'sky130',",
        "    root / 'pdks' / 'sky130',",
        "    root.parent / 'pdks' / 'sky130',",
        "    root.parent / 'pdk' / 'sky130',",
        "    root.parent.parent / 'pdks' / 'sky130',",
        "    root.parent.parent / 'pdk' / 'sky130',",
        "    Path('/ALIGN-public/pdks/sky130'),",
        "    Path('/ALIGN-public/pdk/sky130'),",
        "    Path('/ALIGN-public/align/pdk/sky130'),",
        "    Path('/align/pdk/sky130'),",
        "    Path('/align/pdks/sky130'),",
        "    Path('/pdks/sky130'),",
        "]",
        "for path in candidates:",
        "    if path.exists() and (path / 'primitive.py').exists():",
        "        print(path)",
        "        sys.exit(0)",
        "search_roots = [root, root.parent, root.parent.parent, Path('/ALIGN-public'), Path('/align'), Path('/usr/local/lib')]",
        "seen = set()",
        "for search_root in search_roots:",
        "    if not search_root.exists() or search_root in seen:",
        "        continue",
        "    seen.add(search_root)",
        "    for primitive in search_root.rglob('primitive.py'):",
        "        parent = primitive.parent",
        "        name = parent.name.lower()",
        "        if name in {'sky130', 'sky130a'} or 'sky130' in str(parent).lower():",
        "            print(parent)",
        "            sys.exit(0)",
        "print('ALIGN Sky130 PDK directory with primitive.py not found in mounted /align_pdk, mounted /pdk, or container image', file=sys.stderr)",
        "sys.exit(2)",
        "PY",
        ")\"",
        "echo \"ALIGN_PDK_DIR=${PDK_DIR}\"",
        (
            "schematic2layout.py /work -p \"${PDK_DIR}\" "
            f"-f {shlex.quote(spice_name)} -s {shlex.quote(module_name)}"
        ),
    ])


def _run_magic_gds(
    state: dict,
    stage_dir: str,
    staged_spice: str,
    module_name: str,
    pdk_variant: str,
    pdk_root_host: str,
    docker_bin: str | None,
    isolated_pins: list[str] | None = None,
    abstract_only: bool = False,
) -> tuple[Any, str, str, str]:
    host_tech, host_tcl = _host_magic_paths(pdk_root_host, pdk_variant)
    if not os.path.exists(host_tech):
        raise RuntimeError(f"Magic GDS generation failed: missing Sky130 Magic tech file at {host_tech}")
    if not os.path.exists(host_tcl):
        raise RuntimeError(f"Magic GDS generation failed: missing Sky130 Magic Tcl file at {host_tcl}")

    magic_bin = shutil.which("magic")
    use_docker = not bool(magic_bin)
    tcl_path = _write_magic_import_tcl(
        stage_dir,
        os.path.basename(staged_spice),
        module_name,
        pdk_variant,
        pdk_root_host,
        use_docker,
        isolated_pins,
        abstract_only=abstract_only,
    )
    if magic_bin:
        host_tcl_text = open(tcl_path, "r", encoding="utf-8").read()
        host_tcl_text = host_tcl_text.replace(f"/pdk/{pdk_variant}/libs.tech/magic/{pdk_variant}.tcl", host_tcl)
        with open(tcl_path, "w", encoding="utf-8") as fh:
            fh.write(host_tcl_text)
        cmd = [magic_bin, "-dnull", "-noconsole", "-T", host_tech, tcl_path]
        tool_mode = "host_magic"
        image = ""
    else:
        if not docker_bin:
            raise RuntimeError("Magic GDS generation failed: Magic is not installed and Docker is not available.")
        cmd = [
            docker_bin,
            "run",
            "--rm",
            "-v",
            f"{pdk_root_host}:/pdk",
            "-v",
            f"{os.path.abspath(stage_dir)}:/work",
            "-w",
            "/work",
            OPENLANE_DOCKER_IMAGE,
            "magic",
            "-dnull",
            "-noconsole",
            "-T",
            _magic_paths(pdk_variant)[0],
            "/work/magic_import_spice.tcl",
        ]
        tool_mode = "docker_magic"
        image = OPENLANE_DOCKER_IMAGE
    cp = run_command(state, "analog_magic_gds", cmd, cwd=stage_dir, timeout_sec=3600)
    return cp, tcl_path, tool_mode, image


def run_agent(state: dict) -> dict:
    print(f"\nRunning {AGENT_NAME}...")
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "analog", "gds")
    os.makedirs(stage_dir, exist_ok=True)

    if not _enabled(state):
        state["analog_gds_generation"] = {"status": "skipped", "reason": "analog_physical_mode_not_generate_sky130_gds"}
        state["status"] = f"{AGENT_NAME}: skipped"
        return state

    module_name = _module_name(state)
    pdk_variant = _resolve_pdk_variant(state)
    pdk_root_host = _resolve_pdk_root_host(state)
    align_pdk_host = _resolve_align_pdk_host(state)
    backend = _gds_backend(state)
    spice_path = state.get("analog_spice_path") or state.get("analog_netlist_path")
    summary: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "pdk": pdk_variant,
        "pdk_root_host": pdk_root_host,
        "align_pdk_host": align_pdk_host,
        "backend": backend,
        "layout_builder": "deterministic_magic_abstract_pins" if backend == "magic" else backend,
        "module": module_name,
        "spice": spice_path,
    }

    if not isinstance(spice_path, str) or not os.path.exists(spice_path):
        summary.update({"status": "failed", "reason": "sky130_spice_missing"})
        _publish_analog_signoff(workflow_id, state, _analog_signoff_summary(summary, invalid_reason="sky130_spice_missing"))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
        state["analog_gds_generation"] = summary
        state["status"] = f"{AGENT_NAME}: failed"
        raise RuntimeError("Analog GDS generation requires a generated or provided Sky130 transistor-level SPICE netlist.")

    align_bin = shutil.which("schematic2layout.py") or shutil.which("align")
    docker_bin = shutil.which("docker")
    spice_base = os.path.basename(spice_path) or f"{module_name}.spice"
    spice_stem, spice_ext = os.path.splitext(spice_base)
    align_spice_name = spice_base if spice_ext.lower() in {".sp", ".cdl"} else f"{spice_stem or module_name}.sp"
    staged_spice = os.path.join(stage_dir, align_spice_name)
    magic_lvs_source_spice = staged_spice
    magic_isolated_pins: list[str] = []
    magic_abstract_only = backend == "magic"
    if backend == "magic":
        _prepare_magic_spice(spice_path, staged_spice)
        with open(staged_spice, "r", encoding="utf-8", errors="ignore") as fh:
            magic_prepared_text = fh.read()
        if magic_abstract_only:
            magic_lvs_source_text, magic_isolated_pins = _abstract_lvs_source_spice(magic_prepared_text)
            magic_import_text = magic_lvs_source_text
        else:
            magic_import_text, magic_lvs_source_text, magic_isolated_pins = _magic_import_and_lvs_source_spice(
                magic_prepared_text,
                _port_specs(state),
            )
        if magic_isolated_pins:
            with open(staged_spice, "w", encoding="utf-8") as fh:
                fh.write(magic_import_text)
            magic_lvs_source_spice = os.path.join(stage_dir, "magic_lvs_source_input.sp")
            with open(magic_lvs_source_spice, "w", encoding="utf-8") as fh:
                fh.write(magic_lvs_source_text)
    elif os.path.abspath(spice_path) != os.path.abspath(staged_spice):
        shutil.copy2(spice_path, staged_spice)
    run_sh = os.path.join(stage_dir, "run_gds.sh")
    if backend == "magic":
        expected_cmd = (
            f"docker run --rm -v {pdk_root_host}:/pdk -v {os.path.abspath(stage_dir)}:/work -w /work "
            f"{OPENLANE_DOCKER_IMAGE} magic -dnull -noconsole -T /pdk/{pdk_variant}/libs.tech/magic/{pdk_variant}.tech "
            "/work/magic_import_spice.tcl"
        )
    elif align_bin:
        host_pdk_arg = _host_align_pdk_arg(state, pdk_variant, pdk_root_host)
        expected_cmd = f"{align_bin} {os.path.abspath(stage_dir)} -p {host_pdk_arg} -f {os.path.basename(staged_spice)} -s {module_name}"
    else:
        docker_script = _align_docker_script(os.path.basename(staged_spice), module_name, pdk_variant)
        align_pdk_mount = f"-v {align_pdk_host}:/align_pdk " if align_pdk_host else ""
        expected_cmd = (
            f"docker run --rm {align_pdk_mount}-v {pdk_root_host}:/pdk -v {os.path.abspath(stage_dir)}:/work -w /work "
            f"{ALIGN_DOCKER_IMAGE} sh -lc {shlex.quote(docker_script)}"
        )
    run_text = "\n".join([
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f'echo "ChipLoop {AGENT_NAME}"',
        f'echo "SPICE={staged_spice}"',
        f'echo "TOP={module_name}"',
        f'echo "PDK={pdk_variant}"',
        f'echo "PDK_ROOT_HOST={pdk_root_host}"',
        f'echo "ALIGN_PDK_HOST={align_pdk_host}"',
        expected_cmd,
        "",
    ])
    with open(run_sh, "w", encoding="utf-8") as fh:
        fh.write(run_text)
    try:
        os.chmod(run_sh, 0o755)
    except Exception:
        pass

    if backend not in {"magic", "align"}:
        summary.update({
            "status": "failed",
            "reason": "unsupported_gds_backend",
            "backend": backend,
        })
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "run_gds.sh", run_text)
        _publish_analog_signoff(workflow_id, state, _analog_signoff_summary(summary, invalid_reason="unsupported_gds_backend"))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
        state["analog_gds_generation"] = summary
        state["status"] = f"{AGENT_NAME}: failed"
        raise RuntimeError(f"Analog GDS generation failed: unsupported backend {backend}")

    if backend == "align" and not align_bin and not docker_bin:
        summary.update({
            "status": "failed",
            "reason": "align_not_installed",
            "run_script": run_sh,
            "note": "No GDS was generated. Install ALIGN/schematic2layout.py on PATH or provide Docker with darpaalign/align-public:latest.",
        })
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "run_gds.sh", run_text)
        _publish_analog_signoff(workflow_id, state, _analog_signoff_summary(summary, invalid_reason="align_not_installed"))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
        state["analog_gds_generation"] = summary
        state["status"] = f"{AGENT_NAME}: failed"
        raise RuntimeError("Analog GDS generation failed: ALIGN/schematic2layout.py is not installed and Docker is not available.")

    if backend == "magic":
        try:
            cp, tcl_path, tool_mode, image = _run_magic_gds(
                state, stage_dir, staged_spice, module_name, pdk_variant, pdk_root_host, docker_bin, magic_isolated_pins, magic_abstract_only
            )
        except RuntimeError as exc:
            summary.update({"status": "failed", "reason": "magic_setup_failed", "error": str(exc)})
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "run_gds.sh", run_text)
            _publish_analog_signoff(workflow_id, state, _analog_signoff_summary(summary, invalid_reason="magic_setup_failed"))
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
            state["analog_gds_generation"] = summary
            state["status"] = f"{AGENT_NAME}: failed"
            raise
    elif align_bin:
        host_pdk_arg = _host_align_pdk_arg(state, pdk_variant, pdk_root_host)
        cmd = [
            align_bin,
            os.path.abspath(stage_dir),
            "-p",
            host_pdk_arg,
            "-f",
            os.path.basename(staged_spice),
            "-s",
            module_name,
        ]
        tool_mode = "host"
        cp = run_command(state, "analog_align_gds", cmd, cwd=stage_dir, timeout_sec=3600)
        tcl_path = ""
        image = ""
    else:
        docker_script = _align_docker_script(os.path.basename(staged_spice), module_name, pdk_variant)
        cmd = [
            docker_bin,
            "run",
            "--rm",
        ]
        if align_pdk_host:
            cmd.extend(["-v", f"{align_pdk_host}:/align_pdk"])
        cmd.extend([
            "-v",
            f"{pdk_root_host}:/pdk",
            "-v",
            f"{os.path.abspath(stage_dir)}:/work",
            "-e",
            f"PDK={pdk_variant}",
            "-e",
            "PDK_ROOT=/pdk",
            "-w",
            "/work",
            ALIGN_DOCKER_IMAGE,
            "sh",
            "-lc",
            docker_script,
        ])
        tool_mode = "docker"
        cp = run_command(state, "analog_align_gds", cmd, cwd=stage_dir, timeout_sec=3600)
        tcl_path = ""
        image = ALIGN_DOCKER_IMAGE if tool_mode == "docker" else ""
    log = (cp.stdout or "") + (cp.stderr or "")
    log_name = "magic_gds.log" if backend == "magic" else "align.log"
    log_path = os.path.join(stage_dir, log_name)
    with open(log_path, "w", encoding="utf-8", errors="ignore") as fh:
        fh.write(log)

    repair_attempted = False
    repair_applied = False
    repair_reason = ""
    repair_layout_issues = []
    lvs_repair_attempted = False
    lvs_repair_applied = False
    lvs_repair_reason = ""
    lvs_repair_layout_issues = []
    forced_failure_reason = ""
    pass1_feedback_problem_count = _magic_feedback_problem_count(log) if backend == "magic" else 0
    pass1_invalid_reason = _magic_layout_invalid(log) if backend == "magic" else ""
    sky130_summary = state.get("analog_sky130_spice") if isinstance(state.get("analog_sky130_spice"), dict) else {}
    can_repair_magic_spice = (
        backend == "magic"
        and pass1_invalid_reason == "magic_feedback_problems"
        and sky130_summary.get("generated") is True
    )
    if can_repair_magic_spice:
        repair_attempted = True
        pass1_log_path = os.path.join(stage_dir, "magic_gds_pass1.log")
        shutil.copy2(log_path, pass1_log_path)
        pass1_spice_path = os.path.join(stage_dir, "magic_input_pass1.sp")
        if os.path.exists(staged_spice):
            shutil.copy2(staged_spice, pass1_spice_path)
        feedback_text = ""
        feedback_path = os.path.join(stage_dir, "magic_feedback.txt")
        if os.path.exists(feedback_path):
            with open(feedback_path, "r", encoding="utf-8", errors="ignore") as fh:
                feedback_text = fh.read()
            shutil.copy2(feedback_path, os.path.join(stage_dir, "magic_feedback_pass1.txt"))
        with open(staged_spice, "r", encoding="utf-8", errors="ignore") as fh:
            original_spice = fh.read()
        repaired_spice = _repair_magic_feedback_spice(
            state,
            module_name=module_name,
            original_spice=original_spice,
            magic_log=log,
            feedback_text=feedback_text,
        )
        if repaired_spice:
            with open(os.path.join(stage_dir, "magic_input_repair.sp"), "w", encoding="utf-8") as fh:
                fh.write(repaired_spice)
            repair_layout_issues = _generated_spice_layout_issues(repaired_spice, _port_specs(state))
            if repair_layout_issues:
                repair_reason = "repair_spice_not_layout_safe"
                forced_failure_reason = repair_reason
            else:
                repair_import_text = _magic_spice_text(repaired_spice)
                repair_import_text, repair_lvs_source_text, magic_isolated_pins = _magic_import_and_lvs_source_spice(
                    repair_import_text,
                    _port_specs(state),
                )
                with open(staged_spice, "w", encoding="utf-8") as fh:
                    fh.write(repair_import_text)
                if magic_isolated_pins:
                    magic_lvs_source_spice = os.path.join(stage_dir, "magic_lvs_source_input.sp")
                    with open(magic_lvs_source_spice, "w", encoding="utf-8") as fh:
                        fh.write(repair_lvs_source_text)
                else:
                    magic_lvs_source_spice = staged_spice
                _preserve_and_clean_magic_layout_outputs(stage_dir, module_name)
                cp, tcl_path, tool_mode, image = _run_magic_gds(
                    state, stage_dir, staged_spice, module_name, pdk_variant, pdk_root_host, docker_bin, magic_isolated_pins, magic_abstract_only
                )
                log = (cp.stdout or "") + (cp.stderr or "")
                with open(log_path, "w", encoding="utf-8", errors="ignore") as fh:
                    fh.write(log)
                repair_applied = _magic_layout_invalid(log) == ""
        else:
            repair_reason = "repair_pass_no_valid_mos_devices"

    gds_path = _find_gds(stage_dir)
    magic_feedback_problem_count = _magic_feedback_problem_count(log) if backend == "magic" else 0
    invalid_reason = forced_failure_reason or (_magic_layout_invalid(log) if backend == "magic" else "")
    if gds_path and not invalid_reason:
        final_gds = os.path.join(stage_dir, f"{module_name}.gds")
        if os.path.abspath(gds_path) != os.path.abspath(final_gds):
            shutil.copy2(gds_path, final_gds)
        summary.update({
            "status": "generated",
            "return_code": cp.returncode,
            "gds": final_gds,
            "log": log_path,
            "tool_mode": tool_mode,
            "image": image,
            "magic_isolated_input_pins": magic_isolated_pins or None,
            "repair_attempted": repair_attempted,
            "repair_applied": repair_applied,
            "pass1_magic_feedback_problem_count": pass1_feedback_problem_count if repair_attempted else None,
        })
        with open(final_gds, "rb") as fh:
            # Store a small text breadcrumb instead of trying to upload binary through text helper.
            pass
        state["analog_macro_gds"] = final_gds
    else:
        reason = invalid_reason or ("magic_gds_not_produced" if backend == "magic" else "align_gds_not_produced")
        summary.update({
            "status": "failed",
            "return_code": cp.returncode,
            "reason": reason,
            "magic_feedback_problem_count": magic_feedback_problem_count,
            "log": log_path,
            "log_tail": _tail(log),
            "tool_mode": tool_mode,
            "image": image,
            "repair_attempted": repair_attempted,
            "repair_applied": repair_applied,
            "repair_reason": repair_reason or None,
            "repair_layout_issues": repair_layout_issues or None,
            "pass1_magic_feedback_problem_count": pass1_feedback_problem_count if repair_attempted else None,
        })

    analog_lvs = None
    if summary.get("status") == "generated" and backend == "magic":
        analog_lvs = _run_analog_lvs(
            state,
            stage_dir=stage_dir,
            module_name=module_name,
            pdk_variant=pdk_variant,
            pdk_root_host=pdk_root_host,
            source_spice=magic_lvs_source_spice,
            docker_bin=docker_bin,
            deterministic_layout=str(summary.get("layout_builder") or "").startswith("deterministic_magic_"),
        )
        pass1_analog_lvs = dict(analog_lvs) if isinstance(analog_lvs, dict) else {}
        if _analog_lvs_should_repair(analog_lvs) and sky130_summary.get("generated") is True:
            lvs_repair_attempted = True
            for src_name, dst_name in (
                ("analog_lvs_magic_extract.log", "analog_lvs_magic_extract_pass1.log"),
                ("analog_lvs_netgen.log", "analog_lvs_netgen_pass1.log"),
                (f"{module_name}_extracted.spice", f"{module_name}_extracted_pass1.spice"),
                (f"{module_name}_lvs_source.spice", f"{module_name}_lvs_source_pass1.spice"),
            ):
                src_path = os.path.join(stage_dir, src_name)
                if os.path.exists(src_path):
                    shutil.copy2(src_path, os.path.join(stage_dir, dst_name))
            pass1_log_path = os.path.join(stage_dir, "magic_gds_lvs_pass1.log")
            if os.path.exists(log_path):
                shutil.copy2(log_path, pass1_log_path)
            pass1_spice_path = os.path.join(stage_dir, "magic_input_lvs_pass1.sp")
            if os.path.exists(staged_spice):
                shutil.copy2(staged_spice, pass1_spice_path)

            with open(magic_lvs_source_spice, "r", encoding="utf-8", errors="ignore") as fh:
                original_spice = fh.read()
            lvs_log_text = ""
            lvs_log_file = analog_lvs.get("log") if isinstance(analog_lvs, dict) else None
            if isinstance(lvs_log_file, str) and os.path.exists(lvs_log_file):
                with open(lvs_log_file, "r", encoding="utf-8", errors="ignore") as fh:
                    lvs_log_text = fh.read()
            extract_log_text = ""
            extract_log_file = analog_lvs.get("extract_log") if isinstance(analog_lvs, dict) else None
            if isinstance(extract_log_file, str) and os.path.exists(extract_log_file):
                with open(extract_log_file, "r", encoding="utf-8", errors="ignore") as fh:
                    extract_log_text = fh.read()
            extracted_text = ""
            extracted_file = analog_lvs.get("extracted_spice") if isinstance(analog_lvs, dict) else None
            if isinstance(extracted_file, str) and os.path.exists(extracted_file):
                with open(extracted_file, "r", encoding="utf-8", errors="ignore") as fh:
                    extracted_text = fh.read()

            port_specs = _port_specs(state)
            lvs_extra_isolated_pins = _port_short_output_pins(analog_lvs, port_specs)
            lvs_repair_strategy = ""
            if lvs_extra_isolated_pins:
                repaired_spice = _remove_magic_output_driver_pins(original_spice, lvs_extra_isolated_pins)
                lvs_repair_strategy = "deterministic_output_supply_short_repair"
            else:
                repaired_spice = _repair_lvs_mismatch_spice(
                    state,
                    module_name=module_name,
                    original_spice=original_spice,
                    lvs_summary=analog_lvs,
                    lvs_log=lvs_log_text,
                    extract_log=extract_log_text,
                    extracted_spice=extracted_text,
                )
            if repaired_spice:
                repaired_spice = _normalize_subckt_bus_pins(repaired_spice)
                repaired_spice = _canonicalize_generated_supply_usage(repaired_spice, port_specs)
                repaired_spice = _canonicalize_generated_input_gate_fanout(repaired_spice, port_specs)
                with open(os.path.join(stage_dir, "magic_input_lvs_repair.sp"), "w", encoding="utf-8") as fh:
                    fh.write(repaired_spice)
                if _material_spice_signature(repaired_spice) == _material_spice_signature(original_spice):
                    lvs_repair_reason = "lvs_repair_no_material_change"
                    analog_lvs = {
                        **analog_lvs,
                        "repair_attempted": True,
                        "repair_applied": False,
                        "repair_reason": lvs_repair_reason,
                    }
                else:
                    lvs_repair_layout_issues = _generated_spice_layout_issues(repaired_spice, port_specs)
                    if lvs_extra_isolated_pins:
                        ignored = {f"output_pin_not_driven:{pin}" for pin in lvs_extra_isolated_pins}
                        lvs_repair_layout_issues = [issue for issue in lvs_repair_layout_issues if issue not in ignored]
                if lvs_repair_layout_issues:
                    lvs_repair_reason = "lvs_repair_spice_not_layout_safe"
                    analog_lvs = {
                        **analog_lvs,
                        "repair_attempted": True,
                        "repair_applied": False,
                        "repair_reason": lvs_repair_reason,
                        "repair_layout_issues": lvs_repair_layout_issues,
                    }
                elif not lvs_repair_reason:
                    repair_import_text = _magic_spice_text(repaired_spice)
                    repair_import_text, repair_lvs_source_text, magic_isolated_pins = _magic_import_and_lvs_source_spice(
                        repair_import_text,
                        port_specs,
                        lvs_extra_isolated_pins,
                    )
                    with open(staged_spice, "w", encoding="utf-8") as fh:
                        fh.write(repair_import_text)
                    if magic_isolated_pins:
                        magic_lvs_source_spice = os.path.join(stage_dir, "magic_lvs_source_input.sp")
                        with open(magic_lvs_source_spice, "w", encoding="utf-8") as fh:
                            fh.write(repair_lvs_source_text)
                    else:
                        magic_lvs_source_spice = staged_spice
                    _preserve_and_clean_magic_layout_outputs(stage_dir, module_name)
                    cp, tcl_path, tool_mode, image = _run_magic_gds(
                        state, stage_dir, staged_spice, module_name, pdk_variant, pdk_root_host, docker_bin, magic_isolated_pins, magic_abstract_only
                    )
                    log = (cp.stdout or "") + (cp.stderr or "")
                    with open(log_path, "w", encoding="utf-8", errors="ignore") as fh:
                        fh.write(log)
                    invalid_reason = _magic_layout_invalid(log)
                    gds_path = _find_gds(stage_dir)
                    if gds_path and not invalid_reason:
                        final_gds = os.path.join(stage_dir, f"{module_name}.gds")
                        if os.path.abspath(gds_path) != os.path.abspath(final_gds):
                            shutil.copy2(gds_path, final_gds)
                        summary.update({
                            "status": "generated",
                            "return_code": cp.returncode,
                            "gds": final_gds,
                            "log": log_path,
                            "tool_mode": tool_mode,
                            "image": image,
                            "magic_isolated_input_pins": magic_isolated_pins or None,
                        })
                        state["analog_macro_gds"] = final_gds
                        analog_lvs = _run_analog_lvs(
                            state,
                            stage_dir=stage_dir,
                            module_name=module_name,
                            pdk_variant=pdk_variant,
                            pdk_root_host=pdk_root_host,
                            source_spice=magic_lvs_source_spice,
                            docker_bin=docker_bin,
                            deterministic_layout=str(summary.get("layout_builder") or "").startswith("deterministic_magic_"),
                        )
                        analog_lvs = {
                            **analog_lvs,
                            "repair_attempted": True,
                            "repair_applied": analog_lvs.get("status") == "clean",
                            "repair_strategy": lvs_repair_strategy or None,
                            "pass1": {
                                "status": pass1_analog_lvs.get("status"),
                                "counts": pass1_analog_lvs.get("counts"),
                                "failure_class": pass1_analog_lvs.get("failure_class"),
                            },
                        }
                        lvs_repair_applied = analog_lvs.get("status") == "clean"
                        if not lvs_repair_applied and not lvs_repair_reason:
                            pass1_failure = pass1_analog_lvs.get("failure_class")
                            pass2_failure = analog_lvs.get("failure_class")
                            pass1_counts = pass1_analog_lvs.get("counts") if isinstance(pass1_analog_lvs.get("counts"), dict) else {}
                            pass2_counts = analog_lvs.get("counts") if isinstance(analog_lvs.get("counts"), dict) else {}
                            if (
                                pass1_failure == "port_short"
                                and pass2_failure == "port_short"
                                and pass1_counts.get("source_devices") == pass2_counts.get("source_devices")
                                and pass1_counts.get("extracted_devices") == pass2_counts.get("extracted_devices")
                            ):
                                lvs_repair_reason = "magic_netlist_to_layout_port_short_not_repaired"
                            elif pass2_failure:
                                lvs_repair_reason = f"lvs_repair_still_{pass2_failure}"
                            else:
                                lvs_repair_reason = "lvs_repair_still_mismatch"
                            analog_lvs = {
                                **analog_lvs,
                                "repair_reason": lvs_repair_reason,
                            }
                    else:
                        lvs_repair_reason = invalid_reason or "lvs_repair_gds_not_produced"
                        summary.update({
                            "status": "failed",
                            "return_code": cp.returncode,
                            "reason": lvs_repair_reason,
                            "magic_feedback_problem_count": _magic_feedback_problem_count(log),
                            "log": log_path,
                            "log_tail": _tail(log),
                            "tool_mode": tool_mode,
                            "image": image,
                        })
                        analog_lvs = {
                            **analog_lvs,
                            "repair_attempted": True,
                            "repair_applied": False,
                            "repair_reason": lvs_repair_reason,
                        }
            else:
                lvs_repair_reason = "lvs_repair_pass_no_valid_mos_devices"
                analog_lvs = {
                    **analog_lvs,
                    "repair_attempted": True,
                    "repair_applied": False,
                    "repair_reason": lvs_repair_reason,
                }
        elif isinstance(analog_lvs, dict):
            analog_lvs = {
                **analog_lvs,
                "repair_attempted": False,
                "repair_applied": False,
            }
        summary["analog_lvs"] = analog_lvs
        summary["lvs_repair_attempted"] = lvs_repair_attempted
        summary["lvs_repair_applied"] = lvs_repair_applied
        summary["lvs_repair_reason"] = lvs_repair_reason or None
        summary["lvs_repair_layout_issues"] = lvs_repair_layout_issues or None

    if backend == "magic" and isinstance(analog_lvs, dict) and summary.get("status") == "generated":
        final_gds = summary.get("gds") if isinstance(summary.get("gds"), str) else ""
        lvs_status = str(analog_lvs.get("status") or "").strip().lower()
        if lvs_status == "clean":
            if final_gds:
                _promote_clean_analog_gds(state, final_gds)
            summary["lvs_gate_status"] = "clean"
        else:
            _demote_unclean_analog_gds(state, final_gds or None)
            summary.update({
                "status": "failed",
                "reason": "analog_lvs_not_clean",
                "lvs_gate_status": "failed",
                "lvs_gate_reason": lvs_status or "analog_lvs_not_run",
            })

    analog_klayout_drc = None
    if backend == "magic" and summary.get("status") == "generated" and isinstance(summary.get("gds"), str):
        analog_klayout_drc = _run_analog_klayout_drc(
            state,
            stage_dir=stage_dir,
            module_name=module_name,
            pdk_variant=pdk_variant,
            pdk_root_host=pdk_root_host,
            gds_path=summary["gds"],
            docker_bin=docker_bin,
        )
        summary["analog_klayout_drc"] = analog_klayout_drc
        drc_status = str(analog_klayout_drc.get("status") or "").strip().lower()
        if drc_status in {"violations_found", "failed"} or (analog_klayout_drc.get("violations") or 0) > 0:
            _demote_unclean_analog_gds(state, summary.get("gds"))
            summary.update({
                "status": "failed",
                "reason": "analog_klayout_drc_not_clean" if drc_status == "violations_found" else "analog_klayout_drc_failed",
                "drc_gate_status": "failed",
                "drc_gate_reason": drc_status or "analog_klayout_drc_not_clean",
            })

    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "run_gds.sh", run_text)
    if backend == "magic" and tcl_path and os.path.exists(tcl_path):
        with open(tcl_path, "r", encoding="utf-8") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_import_spice.tcl", fh.read())
    if backend == "magic" and os.path.exists(staged_spice):
        with open(staged_spice, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_input.sp", fh.read())
    pass1_spice_path = os.path.join(stage_dir, "magic_input_pass1.sp")
    if backend == "magic" and os.path.exists(pass1_spice_path):
        with open(pass1_spice_path, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_input_pass1.sp", fh.read())
    repair_spice_path = os.path.join(stage_dir, "magic_input_repair.sp")
    if backend == "magic" and os.path.exists(repair_spice_path):
        with open(repair_spice_path, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_input_repair.sp", fh.read())
    lvs_repair_spice_path = os.path.join(stage_dir, "magic_input_lvs_repair.sp")
    if backend == "magic" and os.path.exists(lvs_repair_spice_path):
        with open(lvs_repair_spice_path, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_input_lvs_repair.sp", fh.read())
    feedback_path = os.path.join(stage_dir, "magic_feedback.txt")
    if backend == "magic" and os.path.exists(feedback_path):
        with open(feedback_path, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_feedback.txt", fh.read())
    pass1_feedback_path = os.path.join(stage_dir, "magic_feedback_pass1.txt")
    if backend == "magic" and os.path.exists(pass1_feedback_path):
        with open(pass1_feedback_path, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_feedback_pass1.txt", fh.read())
    pass1_log_path = os.path.join(stage_dir, "magic_gds_pass1.log")
    if backend == "magic" and os.path.exists(pass1_log_path):
        with open(pass1_log_path, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_gds_pass1.log", fh.read())
    pass1_lvs_gds_log_path = os.path.join(stage_dir, "magic_gds_lvs_pass1.log")
    if backend == "magic" and os.path.exists(pass1_lvs_gds_log_path):
        with open(pass1_lvs_gds_log_path, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "magic_gds_lvs_pass1.log", fh.read())
    extract_lvs_tcl = os.path.join(stage_dir, "magic_extract_lvs.tcl")
    if backend == "magic" and os.path.exists(extract_lvs_tcl):
        with open(extract_lvs_tcl, "r", encoding="utf-8", errors="ignore") as fh:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/signoff", "magic_extract_lvs.tcl", fh.read())
    for artifact_name in (
        "analog_lvs_magic_extract.log",
        "analog_lvs_magic_extract_pass1.log",
        "analog_lvs_netgen.log",
        "analog_lvs_netgen_pass1.log",
        "analog_klayout_drc.log",
        "analog_klayout_drc.xml",
        f"{module_name}_extracted.spice",
        f"{module_name}_extracted_pass1.spice",
        f"{module_name}_lvs_source.spice",
        f"{module_name}_lvs_source_pass1.spice",
    ):
        artifact_path = os.path.join(stage_dir, artifact_name)
        if os.path.exists(artifact_path):
            with open(artifact_path, "r", encoding="utf-8", errors="ignore") as fh:
                save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/signoff", artifact_name, fh.read())
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", log_name, log)
    _publish_analog_signoff(
        workflow_id,
        state,
        _analog_signoff_summary(
            summary,
            log_path=log_path,
            log=log,
            gds_path=summary.get("gds") if isinstance(summary.get("gds"), str) else gds_path,
            invalid_reason=invalid_reason,
            analog_lvs=analog_lvs,
        ),
    )
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "analog/gds", "analog_gds_summary.json", json.dumps(summary, indent=2))
    state["analog_gds_generation"] = summary
    state["status"] = f"{AGENT_NAME}: {summary['status']}"
    if summary["status"] != "generated":
        detail = summary.get("log_tail") or ""
        label = "Magic" if backend == "magic" else "ALIGN"
        raise RuntimeError(
            f"Analog GDS generation failed: {summary.get('reason') or 'gds_not_produced'}"
            + (f"\n{label} log tail:\n{detail}" if detail else "")
        )
    return state
