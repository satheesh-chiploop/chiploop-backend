import glob
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from tooling.runner import run_command, tool_path
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Digital Logic Equivalence Check Agent"


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_text(path: str, content: str) -> None:
    _ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _existing_path(value: Any, workflow_dir: str | None = None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    candidates = [raw]
    if workflow_dir and not os.path.isabs(raw):
        candidates.insert(0, os.path.join(workflow_dir, raw))
    for cand in candidates:
        try:
            cand = os.path.abspath(cand)
            if os.path.exists(cand):
                return cand
        except (TypeError, ValueError, OSError):
            continue
    return None


def _dedupe(paths: list[str]) -> list[str]:
    out = []
    seen = set()
    for path in paths:
        ap = os.path.abspath(path)
        if ap not in seen:
            seen.add(ap)
            out.append(ap)
    return out


def _read_json(path: str | None) -> dict:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _pdk_root_host(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    foundry = digital.get("foundry") if isinstance(digital.get("foundry"), dict) else {}
    foundry_path = _existing_path(os.path.join(workflow_dir, "digital", "foundry", "foundry_profile.json"))
    foundry_file = _read_json(foundry_path)
    candidates = [
        state.get("pdk_root_host"),
        state.get("pdk_root"),
        foundry.get("pdk_root"),
        foundry_file.get("pdk_root"),
        os.getenv("CHIPLOOP_PDK_ROOT_HOST"),
        os.getenv("PDK_ROOT"),
        "/root/chiploop-backend/backend/pdk",
        "backend/pdk",
    ]
    for cand in candidates:
        path = _existing_path(cand)
        if path:
            return path
    return None


def _pdk_variant(state: dict, workflow_dir: str) -> str:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    foundry = digital.get("foundry") if isinstance(digital.get("foundry"), dict) else {}
    foundry_path = _existing_path(os.path.join(workflow_dir, "digital", "foundry", "foundry_profile.json"))
    foundry_file = _read_json(foundry_path)
    value = (
        state.get("pdk_variant")
        or state.get("pdk")
        or foundry.get("pdk")
        or foundry_file.get("pdk")
        or os.getenv("CHIPLOOP_PDK_VARIANT")
        or os.getenv("PDK")
        or "sky130A"
    )
    return str(value).strip() or "sky130A"


def _prefer_active_pdk(paths: list[str], pdk_variant: str) -> list[str]:
    preferred = [p for p in paths if f"{os.sep}{pdk_variant}{os.sep}" in os.path.abspath(p)]
    return preferred or paths


def _liberty_candidates(state: dict, workflow_dir: str) -> list[str]:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    explicit: list[str] = []
    for value in (
        state.get("liberty"),
        state.get("stdcell_liberty"),
        state.get("timing_liberty"),
        digital.get("liberty"),
        digital.get("stdcell_liberty"),
    ):
        path = _existing_path(value, workflow_dir)
        if path:
            explicit.append(path)

    root = _pdk_root_host(state, workflow_dir)
    pdk_variant = _pdk_variant(state, workflow_dir)
    discovered: list[str] = []
    if root:
        patterns = [
            os.path.join(root, pdk_variant, "libs.ref", "sky130_fd_sc_hd", "lib", "sky130_fd_sc_hd__tt_025C_1v80.lib"),
            os.path.join(root, "**", "sky130_fd_sc_hd__tt_025C_1v80.lib"),
            os.path.join(root, "**", "sky130_fd_sc_hd__tt*.lib"),
            os.path.join(root, "**", "*tt*025C*1v80*.lib"),
            os.path.join(root, "**", "*.lib"),
        ]
        for pattern in patterns:
            discovered.extend(glob.glob(pattern, recursive=True)[:10])
            if discovered:
                break
    return _dedupe(explicit + _prefer_active_pdk(discovered, pdk_variant))


def _stdcell_verilog_candidates(state: dict, workflow_dir: str) -> list[str]:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    explicit: list[str] = []
    for value in (
        state.get("stdcell_verilog"),
        state.get("stdcell_model"),
        digital.get("stdcell_verilog"),
        digital.get("stdcell_model"),
    ):
        path = _existing_path(value, workflow_dir)
        if path:
            explicit.append(path)

    root = _pdk_root_host(state, workflow_dir)
    pdk_variant = _pdk_variant(state, workflow_dir)
    discovered: list[str] = []
    if root:
        direct = [
            os.path.join(root, pdk_variant, "libs.ref", "sky130_fd_sc_hd", "verilog", "sky130_fd_sc_hd.v"),
        ]
        direct_existing = [path for path in direct if os.path.exists(path)]
        if direct_existing:
            return _dedupe(explicit + direct_existing)
        patterns = [
            os.path.join(root, "**", "cells_sim.v"),
            os.path.join(root, "**", "cells.functional.v"),
            os.path.join(root, "**", "sky130_fd_sc_hd__*.functional.v"),
            os.path.join(root, "**", "sky130_fd_sc_hd__*.behavioral.v"),
            os.path.join(root, "**", "libs.ref", "sky130_fd_sc_hd", "verilog", "*.v"),
            os.path.join(root, "**", "sky130_fd_sc_hd.v"),
            os.path.join(root, "**", "sky130_fd_sc_hd__*.v"),
        ]
        for pattern in patterns:
            discovered.extend(glob.glob(pattern, recursive=True)[:200])
            if discovered:
                break
    discovered = [p for p in _prefer_active_pdk(discovered, pdk_variant) if "blackbox" not in os.path.basename(p).lower()]
    discovered = [p for p in discovered if os.path.basename(p).lower() != "primitives.v"]
    return _dedupe(explicit + discovered)


def _referenced_modules(verilog_path: str | None) -> list[str]:
    if not verilog_path:
        return []
    try:
        text = Path(verilog_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    refs: list[str] = []
    for match in re.finditer(r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s+(?:#\s*\(|[A-Za-z_][A-Za-z0-9_$]*\s*\()", text, flags=re.MULTILINE):
        name = match.group(1)
        if name in {"module", "endmodule", "assign", "always", "if", "for", "generate"}:
            continue
        refs.append(name)
    return list(dict.fromkeys(refs))


def _module_names_in_files(paths: list[str]) -> set[str]:
    names: set[str] = set()
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        names.update(re.findall(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text, flags=re.MULTILINE))
    return names


def _module_name_in_file(path: str | None) -> str | None:
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    match = re.search(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text, flags=re.MULTILINE)
    return match.group(1) if match else None


def _referenced_sky130_cells(gate: str | None) -> list[str]:
    return [name for name in _referenced_modules(gate) if name.startswith(("sky130_fd_sc_", "sky130_ef_sc_"))]


def _referenced_non_stdcell_modules(gate: str | None, top: str) -> list[str]:
    skip = {top, "module", "endmodule", "assign", "always", "if", "for", "generate"}
    return [
        name
        for name in _referenced_modules(gate)
        if name not in skip and not name.startswith(("sky130_fd_sc_", "sky130_ef_sc_"))
    ]


def _module_ports_from_text(text: str, module_name: str) -> list[tuple[str, str]]:
    module_match = re.search(
        rf"\bmodule\s+{re.escape(module_name)}\s*\((?P<header>.*?)\)\s*;",
        text,
        flags=re.DOTALL,
    )
    if not module_match:
        return []
    ports: dict[str, str] = {}
    for decl in re.finditer(
        r"\b(?P<direction>input|output|inout)\b\s*(?:wire|reg|logic)?\s*(?:\[[^\]]+\]\s*)?(?P<names>[^;,\)]+(?:\s*,\s*[^;,\)]+)*)",
        text,
        flags=re.MULTILINE,
    ):
        direction = decl.group("direction")
        for raw_name in decl.group("names").split(","):
            name = re.sub(r"=.*$", "", raw_name).strip()
            name = re.sub(r"^(?:wire|reg|logic)\s+", "", name).strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
                ports[name] = direction
    ordered: list[tuple[str, str]] = []
    for raw_name in module_match.group("header").split(","):
        name = raw_name.strip()
        inline_match = re.match(r"(?:(input|output|inout)\s+)?(?:wire|reg|logic\s+)?(?:\[[^\]]+\]\s*)?([A-Za-z_][A-Za-z0-9_$]*)$", name)
        if inline_match:
            port_name = inline_match.group(2)
            ordered.append((port_name, inline_match.group(1) or ports.get(port_name, "input")))
    return ordered


def _macro_blackbox_stubs(gate: str | None, rtl_files: list[str], stage_dir: str, top: str) -> tuple[list[str], set[str]]:
    macro_names = set(_referenced_non_stdcell_modules(gate, top))
    if not macro_names:
        return [], set()
    stubs: list[str] = []
    replaced: set[str] = set()
    input_dir = os.path.join(stage_dir, "input")
    _ensure_dir(input_dir)
    for path in rtl_files:
        module_name = _module_name_in_file(path)
        if not module_name or module_name not in macro_names:
            continue
        ports = _module_ports_from_text(_read_text(path), module_name)
        port_names = [name for name, _direction in ports]
        lines = [
            "// Auto-generated blackbox stub for preserved macro LEC.",
            "(* blackbox *)",
            f"module {module_name}({', '.join(port_names)});",
        ]
        for name, direction in ports:
            lines.append(f"  {direction} {name};")
        lines.append("endmodule")
        stub_path = os.path.join(input_dir, f"{module_name}_blackbox.v")
        _write_text(stub_path, "\n".join(lines) + "\n")
        stubs.append(stub_path)
        replaced.add(path)
    return stubs, replaced


def _prepare_golden_rtl_for_yosys(rtl_files: list[str], gate: str | None, stage_dir: str, top: str) -> tuple[list[str], list[str]]:
    stubs, replaced = _macro_blackbox_stubs(gate, rtl_files, stage_dir, top)
    if not stubs:
        return rtl_files, []
    return [path for path in rtl_files if path not in replaced] + stubs, stubs


def _parse_sky130_instances(netlist_text: str) -> dict[str, set[str]]:
    cells: dict[str, set[str]] = {}
    pattern = re.compile(
        r"(?P<cell>sky130_(?:fd|ef)_sc_hd__[A-Za-z0-9_]+)\s+"
        r"(?P<inst>(?:\\[^\s(]+|[A-Za-z_][A-Za-z0-9_$]*))\s*"
        r"\((?P<ports>.*?)\);",
        flags=re.DOTALL,
    )
    for match in pattern.finditer(netlist_text):
        pins = cells.setdefault(match.group("cell"), set())
        for item in re.finditer(r"\.(?P<pin>[A-Za-z0-9_]+)\s*\(", match.group("ports")):
            pins.add(item.group("pin"))
    return cells


def _cell_base(cell: str) -> str:
    return re.sub(r"_\d+$", "", re.sub(r"^sky130_(?:fd|ef)_sc_hd__", "", cell))


def _output_pins(pins: set[str]) -> list[str]:
    return [pin for pin in ("X", "Y", "Q", "Q_N") if pin in pins]


def _input_pins(pins: set[str]) -> list[str]:
    return sorted(pin for pin in pins if pin not in set(_output_pins(pins)))


def _pin_expr(pin: str) -> str:
    return f"~{pin}" if pin.endswith("_N") else pin


def _gate_expr(gate: str, pins: list[str]) -> str:
    terms = [_pin_expr(pin) for pin in pins]
    if not terms:
        return "1'b0"
    joiner = " & " if gate in {"and", "nand"} else " | "
    expr = "(" + joiner.join(terms) + ")"
    return f"~{expr}" if gate in {"nand", "nor"} else expr


def _compound_expr(cell_base: str, pins: set[str]) -> str | None:
    inverted = cell_base.endswith("i")
    base = cell_base[:-1] if inverted else cell_base
    group_letters = "ABCDE"
    if base.startswith("a"):
        final_joiner = " | "
        group_joiner = " & "
        spec = "".join(ch for ch in base[1:].rstrip("o") if ch.isdigit())
    elif base.startswith("o"):
        final_joiner = " & "
        group_joiner = " | "
        spec = "".join(ch for ch in base[1:].rstrip("a") if ch.isdigit())
    else:
        return None
    if not spec:
        return None
    groups: list[str] = []
    for group_idx, count in enumerate(spec[:len(group_letters)]):
        letter = group_letters[group_idx]
        group_terms: list[str] = []
        for pin_idx in range(1, int(count) + 1):
            pin = f"{letter}{pin_idx}"
            if pin in pins:
                group_terms.append(pin)
            elif f"{pin}_N" in pins:
                group_terms.append(f"{pin}_N")
        if group_terms:
            groups.append("(" + group_joiner.join(_pin_expr(pin) for pin in group_terms) + ")")
    if not groups:
        return None
    expr = "(" + final_joiner.join(groups) + ")"
    return f"~{expr}" if inverted else expr


def _cell_assign_expr(cell_base: str, pins: set[str]) -> str | None:
    if cell_base.startswith(("inv", "clkinv")):
        return "~A" if "A" in pins else None
    if cell_base.startswith("buf") or cell_base.startswith("clkbuf") or cell_base.startswith("dly"):
        return "A" if "A" in pins else None
    if cell_base.startswith("conb"):
        return None
    if cell_base.startswith("xor2"):
        return "(A ^ B)" if {"A", "B"}.issubset(pins) else None
    if cell_base.startswith("xnor2"):
        return "~(A ^ B)" if {"A", "B"}.issubset(pins) else None
    if cell_base.startswith("mux2"):
        return "(S ? A1 : A0)" if {"A0", "A1", "S"}.issubset(pins) else None
    for prefix in ("nand", "nor", "and", "or"):
        if cell_base.startswith(prefix):
            return _gate_expr(prefix, _input_pins(pins))
    return _compound_expr(cell_base, pins)


def _is_physical_only_cell(cell_base: str) -> bool:
    return cell_base.startswith((
        "fill",
        "decap",
        "tap",
        "tapvpwrvgnd",
        "bufinv",
        "clkbuf",
        "clkinv",
        "endcap",
        "diode",
        "diod",
        "welltap",
    ))


def _generated_stdcell_model(netlist: str | None, stage_dir: str) -> str | None:
    text = _read_text(netlist)
    if not text:
        return None
    cells = _parse_sky130_instances(text)
    if not cells:
        return None
    lines = [
        "// Auto-generated functional SKY130 cell wrappers for Yosys LEC.",
        "// Generated from the synthesized netlist's referenced cell/pin set.",
    ]
    unsupported: list[str] = []
    for cell in sorted(cells):
        pins = cells[cell]
        base = _cell_base(cell)
        inputs = _input_pins(pins)
        outputs = _output_pins(pins)
        if not outputs:
            if _is_physical_only_cell(base):
                lines.append(f"module {cell}({', '.join(inputs)});")
                for pin in inputs:
                    lines.append(f"  input {pin};")
                lines.append("endmodule")
                lines.append("")
            else:
                unsupported.append(cell)
            continue
        body: list[str] = []
        decls = [f"input {pin}" for pin in inputs] + [f"output {pin}" for pin in outputs]
        if base.startswith(("dfrtp", "dfxtp", "sdfrtp")):
            if "CLK" not in pins or "D" not in pins or "Q" not in pins:
                unsupported.append(cell)
                continue
            else:
                body.append("  reg q_reg;")
                if "RESET_B" in pins:
                    body.append("  always @(posedge CLK or negedge RESET_B) begin")
                    body.append("    if (!RESET_B) q_reg <= 1'b0;")
                    body.append("    else q_reg <= D;")
                    body.append("  end")
                else:
                    body.append("  always @(posedge CLK) q_reg <= D;")
                body.append("  assign Q = q_reg;")
                if "Q_N" in pins:
                    body.append("  assign Q_N = ~q_reg;")
        else:
            if base.startswith("conb"):
                if "HI" in pins:
                    body.append("  assign HI = 1'b1;")
                if "LO" in pins:
                    body.append("  assign LO = 1'b0;")
                expr = "handled"
            else:
                expr = _cell_assign_expr(base, pins)
            if expr is None:
                unsupported.append(cell)
                continue
            elif expr != "handled":
                out = "X" if "X" in pins else ("Y" if "Y" in pins else outputs[0])
                body.append(f"  assign {out} = {expr};")
                if "Q_N" in pins and out == "Q":
                    body.append("  assign Q_N = ~Q;")
        lines.append(f"module {cell}({', '.join([*inputs, *outputs])});")
        for decl in decls:
            lines.append(f"  {decl};")
        lines.extend(body)
        lines.append("endmodule")
        lines.append("")
    if unsupported:
        lines.append("// Unsupported cells intentionally left absent so Yosys reports unresolved models:")
        for cell in sorted(set(unsupported)):
            lines.append(f"// - {cell}")
    out = os.path.join(stage_dir, "input", "stdcell_functional_wrappers.v")
    _write_text(out, "\n".join(lines) + "\n")
    return out


def _missing_stdcell_models(gate: str | None, existing_models: list[str]) -> list[str]:
    referenced = _referenced_sky130_cells(gate)
    modeled_by_pdk = _module_names_in_files(existing_models)
    return [cell for cell in referenced if cell not in modeled_by_pdk]


def _prepare_stdcell_models_for_yosys(stdcell_verilog: list[str], stage_dir: str) -> list[str]:
    prepared: list[str] = []
    input_dir = os.path.join(stage_dir, "input")
    _ensure_dir(input_dir)
    for idx, path in enumerate(stdcell_verilog):
        text = _read_text(path)
        if not text:
            continue
        # SKY130 simulation models use `UNIT_DELAY for gate-level timing.
        # LEC needs functional behavior only, so strip delay tokens for Yosys.
        sanitized = text.replace("`UNIT_DELAY", "")
        if sanitized != text:
            out = os.path.join(input_dir, f"stdcell_model_{idx}_{os.path.basename(path)}")
            _write_text(out, sanitized)
            prepared.append(out)
        else:
            prepared.append(path)
    return prepared


def _rtl_sources(state: dict, workflow_dir: str) -> list[str]:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    candidates: list[str] = []
    for value in (
        state.get("rtl_files"),
        state.get("rtl_inputs"),
        state.get("source_rtl_files"),
        digital.get("rtl_files"),
    ):
        if isinstance(value, list):
            candidates.extend([p for p in (_existing_path(x, workflow_dir) for x in value) if p])
    if not candidates:
        for pattern in (
            "handoff/rtl/**/*.v",
            "handoff/rtl/**/*.sv",
            "digital/handoff/rtl/**/*.v",
            "digital/handoff/rtl/**/*.sv",
            "digital/rtl/**/*.v",
            "digital/rtl/**/*.sv",
        ):
            candidates.extend(glob.glob(os.path.join(workflow_dir, pattern), recursive=True))
    synth_dir = os.path.join(workflow_dir, "digital", "synth")
    return [p for p in _dedupe(candidates) if not os.path.abspath(p).startswith(os.path.abspath(synth_dir))]


def _synth_netlist(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    synth = digital.get("synth") if isinstance(digital.get("synth"), dict) else {}
    for cand in (
        synth.get("netlist"),
        digital.get("synth_netlist"),
        state.get("synth_netlist"),
    ):
        path = _existing_path(cand, workflow_dir)
        if path:
            return path
    hits = sorted(glob.glob(os.path.join(workflow_dir, "digital", "synth", "netlist", "*.v")))
    return hits[0] if hits else None


def _top_module(state: dict, rtl_files: list[str], netlist: str | None) -> str:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    synth = digital.get("synth") if isinstance(digital.get("synth"), dict) else {}
    for value in (synth.get("top_module"), digital.get("top_module"), state.get("top_module")):
        if isinstance(value, str) and value.strip() and value.strip() != "imported_from_arch2rtl":
            return value.strip()
    for path in [netlist, *rtl_files]:
        if not path:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        match = re.search(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text, flags=re.MULTILINE)
        if match:
            return match.group(1)
    return "top"


def _lec_induct_depth() -> int:
    raw = os.getenv("CHIPLOOP_LEC_INDUCT_DEPTH", "").strip()
    if not raw:
        return 256
    try:
        value = int(raw)
    except ValueError:
        return 256
    return max(4, min(value, 1024))


def _yosys_script(golden: list[str], gate: str, top: str, stdcell_verilog: list[str], gate_ignore_ports: list[str] | None = None) -> str:
    induct_depth = _lec_induct_depth()
    read_golden = "\n".join(f"read_verilog -sv {json.dumps(path)}" for path in golden)
    read_models = "\n".join(f"read_verilog -sv -D FUNCTIONAL {json.dumps(path)}" for path in stdcell_verilog)
    ignored_gate_ports = [port for port in (gate_ignore_ports or []) if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", port)]
    delete_gate_ports = ""
    if ignored_gate_ports:
        delete_gate_ports = "delete -port " + " ".join(f"w:{port}" for port in ignored_gate_ports) + "\n"
    return f"""# Auto-generated by {AGENT_NAME}
{read_golden}
hierarchy -check -top {top}
proc; opt; memory; opt
async2sync
flatten
splitnets -ports
opt_clean
rename -top gold
design -stash gold

{read_models}
read_verilog -sv {json.dumps(gate)}
hierarchy -check -top {top}
proc; opt; memory; opt
async2sync
flatten
splitnets -ports
opt_clean
{delete_gate_ports}\
rename -top gate
design -stash gate

design -copy-from gold -as gold gold
design -copy-from gate -as gate gate
equiv_make gold gate equiv
hierarchy -top equiv
equiv_simple -seq 20
equiv_induct -undef -seq {induct_depth}
equiv_simple -seq 50
equiv_status -assert
"""


def _classify(returncode: int | None, log: str, tool_available: bool) -> tuple[str, int]:
    if not tool_available:
        return "tool_unavailable", 0
    text = log.lower()
    if returncode == 0 and "equivalence successfully proven" in text:
        return "pass", 0
    unproven = 0
    total_match = re.search(r"found a total of\s+(\d+)\s+unproven", text)
    if total_match:
        unproven = int(total_match.group(1))
    else:
        counts = [int(match.group(1)) for match in re.finditer(r"(\d+)\s+unproven", text)]
        if counts:
            unproven = counts[-1]
    if "syntax error" in text and ("sky130_fd_sc" in text or "stdcell" in text or "primitive" in text):
        return "inconclusive_stdcell_model_parse_error", unproven
    if "syntax error" in text:
        return "inconclusive_yosys_parse_error", unproven
    if "equivalence checking failed" in text or "assert failed" in text:
        return "fail", unproven or 1
    if "no sat model available" in text:
        return "inconclusive_missing_sat_models", unproven
    if "is not part of the design" in text or ("module `" in text and "referenced" in text):
        return "inconclusive_unresolved_cells", unproven
    if "blackbox" in text and ("sat" in text or "equiv" in text):
        return "inconclusive_missing_sat_models", unproven
    return "inconclusive", unproven


def _failure_reason(verdict: str, *, rtl_files: list[str], netlist: str | None, yosys: str | None, liberty_files: list[str], stdcell_verilog: list[str], missing_cells: list[str], unproven: int) -> str:
    if verdict == "pass":
        return "equivalence_proven"
    if not yosys:
        return "yosys_not_available_in_tool_profile"
    if not rtl_files:
        return "missing_rtl_inputs"
    if not netlist:
        return "missing_synthesized_netlist"
    if not stdcell_verilog:
        return "missing_standard_cell_verilog_models"
    if missing_cells:
        return "standard_cell_verilog_models_incomplete"
    if unproven:
        return "equivalence_points_unproven"
    if verdict == "inconclusive_stdcell_model_parse_error":
        return "stdcell_verilog_model_parse_error"
    if verdict == "inconclusive_yosys_parse_error":
        return "yosys_parse_error"
    if verdict.startswith("inconclusive"):
        return "yosys_inconclusive_see_lec_log"
    if verdict == "fail":
        return "rtl_gate_mismatch_or_unsupported_sequential_equivalence"
    return verdict


def run_agent(state: dict) -> dict:
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = os.path.abspath(state.get("workflow_dir") or f"backend/workflows/{workflow_id}")
    stage_dir = os.path.join(workflow_dir, "digital", "lec")
    logs_dir = os.path.join(stage_dir, "logs")
    _ensure_dir(logs_dir)

    rtl_files = _rtl_sources(state, workflow_dir)
    netlist = _synth_netlist(state, workflow_dir)
    top = _top_module(state, rtl_files, netlist)
    yosys = tool_path("yosys", state)
    liberty_files = _liberty_candidates(state, workflow_dir)
    stdcell_verilog = _stdcell_verilog_candidates(state, workflow_dir)
    generated_stdcell_model = _generated_stdcell_model(netlist, stage_dir)
    yosys_stdcell_verilog = [generated_stdcell_model] if generated_stdcell_model else _prepare_stdcell_models_for_yosys(stdcell_verilog, stage_dir)
    missing_stdcell_models = _missing_stdcell_models(netlist, yosys_stdcell_verilog)
    if generated_stdcell_model:
        model_strategy = "generated_functional_wrappers_from_gate_netlist"
    elif stdcell_verilog:
        model_strategy = "pdk_stdcell_verilog"
    else:
        model_strategy = "none"

    script_path = os.path.join(stage_dir, "yosys_lec.ys")
    log_path = os.path.join(logs_dir, "yosys_lec.log")
    report_path = os.path.join(stage_dir, "lec_report.md")
    summary_path = os.path.join(stage_dir, "lec_summary.json")

    prepared_rtl_files, golden_macro_stubs = _prepare_golden_rtl_for_yosys(rtl_files, netlist, stage_dir, top) if rtl_files and netlist else (rtl_files, [])
    has_required_inputs = bool(prepared_rtl_files and netlist and yosys and yosys_stdcell_verilog and not missing_stdcell_models)
    if rtl_files and netlist:
        script = _yosys_script(prepared_rtl_files, netlist, top, yosys_stdcell_verilog)
    else:
        script = "# Missing RTL or synthesized netlist; LEC not run.\n"
    _write_text(script_path, script)

    rc = None
    log = ""
    if has_required_inputs:
        proc = run_command(state, "logic_equivalence", ["yosys", "-s", script_path], cwd=stage_dir, timeout_sec=900)
        rc = proc.returncode
        log = (proc.stdout or "") + (proc.stderr or "")
    elif not yosys:
        log = "Yosys executable was not found in the active ChipLoop tool profile.\n"
    elif not yosys_stdcell_verilog:
        log = "Missing real standard-cell Verilog simulation/functional models for synthesized gate netlist LEC.\n"
    elif missing_stdcell_models:
        log = "Standard-cell Verilog model coverage is incomplete for synthesized gate netlist LEC.\nMissing cells:\n" + "\n".join(missing_stdcell_models) + "\n"
    else:
        log = "Missing RTL or synthesized netlist for LEC.\n"
    _write_text(log_path, log)

    verdict, unproven = _classify(rc, log, bool(yosys))
    if not rtl_files or not netlist:
        verdict = "incomplete_inputs"
    if not yosys_stdcell_verilog:
        verdict = "missing_stdcell_models"
    if missing_stdcell_models:
        verdict = "incomplete_stdcell_models"
    failure_reason = _failure_reason(
        verdict,
        rtl_files=rtl_files,
        netlist=netlist,
        yosys=yosys,
        liberty_files=liberty_files,
        stdcell_verilog=yosys_stdcell_verilog,
        missing_cells=missing_stdcell_models,
        unproven=unproven,
    )

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": verdict,
        "tool": "yosys",
        "tool_available": bool(yosys),
        "return_code": rc,
        "top_module": top,
        "rtl_files": [os.path.basename(p) for p in rtl_files],
        "yosys_rtl_files": [os.path.basename(p) for p in prepared_rtl_files],
        "golden_macro_blackbox_stubs": [os.path.basename(path) for path in golden_macro_stubs],
        "synth_netlist": os.path.basename(netlist) if netlist else None,
        "liberty_files": [os.path.basename(p) for p in liberty_files],
        "liberty_file_count": len(liberty_files),
        "liberty_usage": "discovered_not_required_for_verilog_model_lec",
        "stdcell_verilog_files": [os.path.basename(p) for p in stdcell_verilog],
        "stdcell_verilog_file_count": len(stdcell_verilog),
        "yosys_stdcell_verilog_files": [os.path.basename(p) for p in yosys_stdcell_verilog],
        "generated_stdcell_model": os.path.basename(generated_stdcell_model) if generated_stdcell_model else None,
        "stdcell_model_strategy": model_strategy,
        "missing_stdcell_models": missing_stdcell_models,
        "missing_stdcell_model_count": len(missing_stdcell_models),
        "unproven_points": unproven,
        "failure_reason": failure_reason,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "artifacts": {
            "script": "digital/lec/yosys_lec.ys",
            "log": "digital/lec/logs/yosys_lec.log",
            "summary": "digital/lec/lec_summary.json",
            "report": "digital/lec/lec_report.md",
            "generated_stdcell_model": "digital/lec/input/stdcell_functional_wrappers.v" if generated_stdcell_model else None,
            "golden_macro_blackbox_stubs": [f"digital/lec/input/{os.path.basename(path)}" for path in golden_macro_stubs],
        },
    }
    report = "\n".join([
        "# Logic Equivalence Check",
        "",
        f"- Status: `{verdict}`",
        f"- Tool: `yosys`",
        f"- Top module: `{top}`",
        f"- RTL files: `{len(rtl_files)}`",
        f"- Synth netlist: `{os.path.basename(netlist) if netlist else 'missing'}`",
        f"- Liberty files discovered: `{len(liberty_files)}`",
        f"- Standard-cell Verilog models loaded: `{len(yosys_stdcell_verilog)}`",
        f"- Standard-cell model strategy: `{model_strategy}`",
        f"- Missing standard-cell models: `{len(missing_stdcell_models)}`",
        f"- Unproven points: `{unproven}`",
        f"- Return code: `{rc}`",
        f"- Failure reason: `{failure_reason}`",
        "",
        "If this is inconclusive, inspect `digital/lec/logs/yosys_lec.log` and `digital/lec/lec_summary.json` for unsupported cells, black boxes, or reset/initial-state assumptions.",
    ]) + "\n"
    if missing_stdcell_models:
        report += "\n## Missing Standard-Cell Models\n\n" + "\n".join(f"- `{cell}`" for cell in missing_stdcell_models) + "\n"
    _write_text(summary_path, json.dumps(summary, indent=2))
    _write_text(report_path, report)

    if generated_stdcell_model:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lec/input/stdcell_functional_wrappers.v", _read_text(generated_stdcell_model))
    for stub_path in golden_macro_stubs:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"lec/input/{os.path.basename(stub_path)}", _read_text(stub_path))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lec/yosys_lec.ys", script)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lec/logs/yosys_lec.log", log)
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lec/lec_summary.json", json.dumps(summary, indent=2))
    save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "lec/lec_report.md", report)

    digital = state.setdefault("digital", {})
    digital["lec"] = {
        "status": verdict,
        "summary_json": summary_path,
        "report_md": report_path,
        "log": log_path,
        "script": script_path,
    }
    state["status"] = f"{AGENT_NAME}: {verdict}"
    return state
