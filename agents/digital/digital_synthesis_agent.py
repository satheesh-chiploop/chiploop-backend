
import os
import json
import glob
import re
import shutil
import subprocess
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("chiploop")


from tooling.runner import run_command
from utils.artifact_utils import save_text_artifact_and_record
from agents.system.system_top_assembly_agent import _assemble_top, _extract_module_ports_from_text

AGENT_NAME = "Digital Synthesis Agent"

DEFAULT_PDK_VARIANT = os.getenv("CHIPLOOP_PDK_VARIANT", "sky130A")
DEFAULT_OPENLANE_IMAGE = os.getenv("CHIPLOOP_OPENLANE_IMAGE", "ghcr.io/efabless/openlane2:2.4.0.dev1")
DEFAULT_NUM_CORES = int(os.getenv("OPENLANE_NUM_CORES", "2"))
OPENLANE_SYNTH_TO_STEP = os.getenv("CHIPLOOP_SYNTH_OPENLANE_TO", "OpenROAD.STAPrePNR")

def _existing_path(value, workflow_dir: str | None = None) -> str | None:
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

def _dedupe_paths(paths: list[str]) -> list[str]:
    out = []
    seen = set()
    for path in paths:
        abs_path = os.path.abspath(path)
        if abs_path not in seen:
            seen.add(abs_path)
            out.append(abs_path)
    return out


def _pdk_root_host(state: dict) -> str:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    foundry = digital.get("foundry") if isinstance(digital.get("foundry"), dict) else {}
    root = (
        state.get("pdk_root_host")
        or state.get("pdk_root")
        or foundry.get("pdk_root")
        or os.getenv("CHIPLOOP_PDK_ROOT_HOST")
        or os.getenv("PDK_ROOT")
        or "/root/chiploop-backend/backend/pdk"
    )
    return os.path.abspath(str(root))


def _pdk_variant(state: dict) -> str:
    digital = state.get("digital") if isinstance(state.get("digital"), dict) else {}
    foundry = digital.get("foundry") if isinstance(digital.get("foundry"), dict) else {}
    return str(
        state.get("pdk_variant")
        or state.get("pdk")
        or foundry.get("pdk")
        or os.getenv("CHIPLOOP_PDK_VARIANT")
        or os.getenv("PDK")
        or DEFAULT_PDK_VARIANT
    ).strip()

def _resolve_spec_json(state: dict, workflow_dir: str) -> str | None:
    for cand in [
        (state.get("digital") or {}).get("spec_json"),
        state.get("digital_spec_json"),
        state.get("spec_json"),
        state.get("spec_json_path"),
    ]:
        path = _existing_path(cand, workflow_dir)
        if path:
            return path

    files = sorted(glob.glob(os.path.join(workflow_dir, "spec", "*_spec.json")))
    return files[0] if files else None


def _resolve_sdc_from_state(state: dict, workflow_dir: str) -> str | None:
    digital = state.get("digital") or {}

    # 1) State is the primary source of truth
    cand = _existing_path(digital.get("constraints_sdc"), workflow_dir)
    if cand:
        logger.info(f"{AGENT_NAME}: selected constraints_sdc from state.digital -> {cand}")
        return cand

    # 2) Fallback to any impl_setup constraint file
    impl_candidates = sorted(glob.glob(os.path.join(workflow_dir, "digital", "impl_setup", "constraints", "*.sdc")))
    for cand in impl_candidates:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected constraints_sdc from impl_setup glob -> {cand}")
            return cand

    # 3) Legacy fallback
    legacy_candidates = sorted(glob.glob(os.path.join(workflow_dir, "digital", "constraints", "*.sdc")))
    for cand in legacy_candidates:
        if os.path.exists(cand):
            logger.info(f"{AGENT_NAME}: selected constraints_sdc from legacy digital/constraints -> {cand}")
            return cand

    logger.warning(f"{AGENT_NAME}: no upstream SDC found in state, impl_setup, or legacy constraints")
    return None




def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)

def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _read_text(path: str | None) -> str:
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""

def _count_netlist_cells(netlist_path: str | None) -> dict:
    text = _read_text(netlist_path)
    if not text:
        return {
            "chiploop__netlist_present": False,
            "chiploop__flipflop_count": None,
            "chiploop__latch_count": None,
        }

    lower = text.lower()
    # Count mapped standard-cell instances. These patterns cover Sky130 and
    # common liberty naming styles without tying the dashboard to one PDK.
    ff_markers = (
        "sky130_fd_sc_hd__df",
        "sky130_fd_sc_hs__df",
        "sky130_fd_sc_ms__df",
        "__dff",
        "__sdff",
        " dff",
        " sdff",
    )
    latch_markers = (
        "sky130_fd_sc_hd__dl",
        "sky130_fd_sc_hs__dl",
        "sky130_fd_sc_ms__dl",
        "latch",
    )

    flop_count = 0
    latch_count = 0
    for raw in lower.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if any(marker in line for marker in ff_markers):
            flop_count += 1
        elif any(marker in line for marker in latch_markers):
            latch_count += 1

    return {
        "chiploop__netlist_present": True,
        "chiploop__flipflop_count": flop_count,
        "chiploop__latch_count": latch_count,
    }

def _augment_synth_metrics(metrics_path: str, netlist_path: str | None) -> dict:
    metrics = _read_json(metrics_path) if os.path.exists(metrics_path) else {}
    metrics.update(_count_netlist_cells(netlist_path))
    if "design__instance_unmapped__count" in metrics:
        metrics["chiploop__unmapped_cell_count"] = metrics.get("design__instance_unmapped__count")
    if "synthesis__check_error__count" in metrics:
        metrics["chiploop__synthesis_check_error_count"] = metrics.get("synthesis__check_error__count")
    wns = metrics.get("timing__setup__ws")
    tns = metrics.get("timing__setup__tns")
    if isinstance(wns, (int, float)):
        metrics.setdefault("chiploop__sta_preplace_wns", wns)
    if isinstance(tns, (int, float)):
        metrics.setdefault("chiploop__sta_preplace_tns", tns)
    for key in (
        "timing__setup__vio__count",
        "timing__setup_vio__count",
        "timing__setup__violation__count",
        "timing__setup__violating_paths",
        "sta__setup__violation_count",
    ):
        if key in metrics:
            metrics.setdefault("chiploop__sta_preplace_setup_violation_count", metrics.get(key))
            break
    if metrics:
        _write_local(metrics_path, json.dumps(metrics, indent=2))
    return metrics


def _repair_common_status_tieoffs(rtl_path: str) -> list[str]:
    """Patch safe status wires that are declared and consumed but left undriven."""
    try:
        text = Path(rtl_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    additions: list[str] = []
    if (
        re.search(r"\bwire\s+status_packet_active\s*;", text)
        and "assign status_packet_active" not in text
        and "status_tx_busy" in text
        and "status_rx_busy" in text
    ):
        additions.append("assign status_packet_active = status_tx_busy | status_rx_busy;")
    if (
        re.search(r"\bwire\s+status_error\s*;", text)
        and "assign status_error" not in text
    ):
        error_terms = [name for name in ("rx_overflow_event", "tx_underflow_event", "framing_error_event") if name in text]
        if error_terms:
            additions.append(f"assign status_error = {' | '.join(error_terms)};")
    if not additions:
        return []
    patched = re.sub(r"\nendmodule\s*$", "\n" + "\n".join(additions) + "\n\nendmodule\n", text, count=1)
    if patched == text:
        return []
    Path(rtl_path).write_text(patched, encoding="utf-8")
    return additions


def _file_defines_module(path: str, module_name: str) -> bool:
    return bool(re.search(rf"\bmodule\s+{re.escape(module_name)}\b", _read_text(path)))


def _module_port_db_from_files(paths: list[str]) -> dict:
    db = {}
    for path in paths or []:
        for module, ports in _extract_module_ports_from_text(_read_text(path)).items():
            db[module] = ports
    return db


def _range_width(range_text: str | None) -> int:
    if not range_text:
        return 1
    match = re.search(r"\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\]", range_text)
    if not match:
        return 1
    left, right = int(match.group(1)), int(match.group(2))
    return abs(left - right) + 1


def _large_inferred_memory_arrays(rtl_files: list[str], min_bits: int = 1024) -> list[dict[str, object]]:
    memories: list[dict[str, object]] = []
    module_re = re.compile(
        r"\bmodule\s+(?P<module>[A-Za-z_][A-Za-z0-9_$]*)\b(?P<body>.*?)(?:\n\s*endmodule\b)",
        flags=re.DOTALL,
    )
    array_re = re.compile(
        r"\b(?:reg|logic)\s*(?P<width>\[[^\]]+\])?\s+(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\s*"
        r"\[\s*(?P<left>\d+)\s*:\s*(?P<right>\d+)\s*\]\s*;",
        flags=re.MULTILINE,
    )
    for path in rtl_files or []:
        text = _read_text(path)
        if not text:
            continue
        for module_match in module_re.finditer(text):
            module = module_match.group("module")
            body = module_match.group("body")
            for arr in array_re.finditer(body):
                width_bits = _range_width(arr.group("width"))
                depth = abs(int(arr.group("left")) - int(arr.group("right"))) + 1
                bits = width_bits * depth
                if bits < min_bits:
                    continue
                memories.append({
                    "file": os.path.basename(path),
                    "path": path,
                    "module": module,
                    "array": arr.group("name"),
                    "width_bits": width_bits,
                    "depth": depth,
                    "estimated_bits": bits,
                })
    return memories


def _read_verilog_module_ports(path: str, module_name: str) -> dict[str, str]:
    text = _read_text(path)
    match = re.search(
        rf"\bmodule\s+{re.escape(module_name)}\s*\((?P<header>.*?)\)\s*;",
        text,
        flags=re.DOTALL,
    )
    if not match:
        return {}
    header = match.group("header")
    ports: dict[str, str] = {}
    for decl in re.finditer(
        r"\b(?P<dir>input|output|inout)\b\s*(?:wire|reg|logic)?\s*(?P<range>\[[^\]]+\])?\s*(?P<names>[^,;]+)",
        header,
        flags=re.IGNORECASE,
    ):
        width = (decl.group("range") or "").strip()
        for raw in decl.group("names").split(","):
            name = raw.strip()
            name = re.sub(r"\s*=.*$", "", name).strip()
            if re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", name):
                ports[name] = width
    if ports:
        return ports

    # Fallback for non-ANSI macro stubs.
    body_match = re.search(
        rf"\bmodule\s+{re.escape(module_name)}\s*\((?P<ports>.*?)\)\s*;(?P<body>.*?)(?:\n\s*endmodule\b)",
        text,
        flags=re.DOTALL,
    )
    if not body_match:
        return {}
    port_names = [p.strip() for p in body_match.group("ports").split(",") if p.strip()]
    body = body_match.group("body")
    for name in port_names:
        decl = re.search(
            rf"\b(?:input|output|inout)\b\s*(?:wire|reg|logic)?\s*(?P<range>\[[^\]]+\])?\s*{re.escape(name)}\b",
            body,
            flags=re.IGNORECASE,
        )
        ports[name] = (decl.group("range") if decl else "") or ""
    return ports


def _extract_balanced_block(text: str, start: int) -> str:
    open_at = text.find("{", start)
    if open_at < 0:
        return ""
    depth = 0
    for idx in range(open_at, len(text)):
        char = text[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[open_at + 1:idx]
    return ""


def _read_liberty_cell_ports(path: str, cell_name: str) -> dict[str, str]:
    text = _read_text(path)
    if not text:
        return {}
    match = re.search(rf"\bcell\s*\(\s*{re.escape(cell_name)}\s*\)\s*\{{", text)
    if not match:
        return {}
    body = _extract_balanced_block(text, match.start())
    ports: dict[str, str] = {}
    for pin_match in re.finditer(r"\bpin\s*\(\s*([A-Za-z_][A-Za-z0-9_$]*)\s*\)\s*\{", body):
        ports[pin_match.group(1)] = ""
    for bus_match in re.finditer(r"\bbus\s*\(\s*([A-Za-z_][A-Za-z0-9_$]*)\s*\)\s*\{", body):
        bus_name = bus_match.group(1)
        bus_body = _extract_balanced_block(body, bus_match.start())
        width = ""
        left = re.search(r"\bbus_type\s*:\s*([A-Za-z_][A-Za-z0-9_$]*)\s*;", bus_body)
        if left:
            bus_type = left.group(1)
            type_match = re.search(rf"\btype\s*\(\s*{re.escape(bus_type)}\s*\)\s*\{{", text)
            type_body = _extract_balanced_block(text, type_match.start()) if type_match else ""
            bit_from = re.search(r"\bbit_from\s*:\s*(-?\d+)\s*;", type_body)
            bit_to = re.search(r"\bbit_to\s*:\s*(-?\d+)\s*;", type_body)
            if bit_from and bit_to:
                width = f"[{bit_from.group(1)}:{bit_to.group(1)}]"
        ports[bus_name] = width
    return ports


def _verilog_modules(path: str) -> list[str]:
    return re.findall(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)\b", _read_text(path), flags=re.MULTILINE)


def _sram_geometry_from_name(name: str) -> tuple[int | None, int | None]:
    match = re.search(r"(?P<width>\d+)\s*x\s*(?P<depth>\d+)", name, flags=re.IGNORECASE)
    if match:
        return int(match.group("width")), int(match.group("depth"))
    match = re.search(r"1rw1r_(?P<width>\d+)_(?P<depth>\d+)_", name, flags=re.IGNORECASE)
    if match:
        return int(match.group("width")), int(match.group("depth"))
    return None, None


def _sram_collateral_rank(path: str) -> tuple[int, int, int, str]:
    base = os.path.basename(path).lower()
    tt = 0 if "_tt_" in base or "tt_" in base else 1
    voltage = 0 if "1p8v" in base else 1
    temp = 0 if "25c" in base else 1
    return (tt, voltage, temp, base)


def _discover_pdk_sram_collateral(state: dict, workflow_dir: str, memories: list[dict[str, object]]) -> list[dict[str, object]]:
    if not memories:
        return []
    pdk_root = os.path.join(_pdk_root_host(state), _pdk_variant(state))
    root_candidates = [
        os.path.join(pdk_root, "libs.ref", "sky130_sram_macros"),
        os.path.join(pdk_root, "libs.ref"),
    ]
    lib_files: list[str] = []
    for root in root_candidates:
        lib_files.extend(glob.glob(os.path.join(root, "**", "*.lib"), recursive=True))
    lib_files = [p for p in _dedupe_paths(lib_files) if os.path.isfile(p)]
    collaterals: list[dict[str, object]] = []
    for mem in memories:
        width = int(mem.get("width_bits") or 0)
        depth = int(mem.get("depth") or 0)
        matched_libs = []
        for lib in lib_files:
            lw, ld = _sram_geometry_from_name(os.path.basename(lib))
            if lw == width and ld == depth:
                matched_libs.append(lib)
        if not matched_libs:
            continue
        lib = sorted(matched_libs, key=_sram_collateral_rank)[0]
        base = os.path.basename(lib)
        base_no_ext = re.sub(r"\.lib(?:\.gz)?$", "", base, flags=re.IGNORECASE)
        physical_base = re.sub(r"_(?:tt|ff|ss)_\d+p\d+v_\d+c$", "", base_no_ext, flags=re.IGNORECASE)
        physical_base = re.sub(r"_(?:TT|FF|SS)_\d+p\d+V_\d+C$", "", physical_base)
        macro_name = physical_base
        verilog_candidates = []
        for root in root_candidates:
            verilog_candidates.extend(glob.glob(os.path.join(root, "**", f"{physical_base}.v"), recursive=True))
            verilog_candidates.extend(glob.glob(os.path.join(root, "**", f"{base_no_ext}.v"), recursive=True))
        verilog = next((p for p in _dedupe_paths(verilog_candidates) if os.path.isfile(p)), None)
        if verilog:
            modules = _verilog_modules(verilog)
            if modules:
                macro_name = modules[0]
        collateral = {
            "behavioral_module": mem.get("module"),
            "array": mem.get("array"),
            "width_bits": width,
            "depth": depth,
            "estimated_bits": mem.get("estimated_bits"),
            "macro_name": macro_name,
            "lib": lib,
            "verilog": verilog,
            "lef": _first_existing_matching(root_candidates, "lef", physical_base, (".lef",)),
            "gds": _first_existing_matching(root_candidates, "gds", physical_base, (".gds", ".gds.gz")),
            "spice": _first_existing_matching(root_candidates, "spice", physical_base, (".spice", ".sp", ".cdl")),
        }
        collaterals.append(collateral)
    return collaterals


def _first_existing_matching(roots: list[str], subdir: str, stem: str, exts: tuple[str, ...]) -> str | None:
    candidates: list[str] = []
    for root in roots:
        for ext in exts:
            candidates.extend(glob.glob(os.path.join(root, "**", subdir, f"{stem}{ext}"), recursive=True))
            candidates.extend(glob.glob(os.path.join(root, "**", f"{stem}{ext}"), recursive=True))
    return next((p for p in _dedupe_paths(candidates) if os.path.isfile(p)), None)


def _macro_connection_expr(macro_port: str, source_ports: set[str], width: str) -> str | None:
    p = macro_port.lower()
    if p in source_ports:
        return macro_port
    aliases = {
        "clk0": ("clk", "clock"),
        "clk1": ("clk", "clock"),
        "csb0": ("csb", "cen", "ce_n", "chip_select_n"),
        "csb1": ("csb", "cen", "ce_n", "chip_select_n"),
        "web0": ("web", "wen", "we_n", "write_enable_n"),
        "addr0": ("addr", "address"),
        "addr1": ("addr", "address"),
        "din0": ("din", "wdata", "write_data"),
        "dout0": ("dout", "rdata", "read_data"),
    }
    for alias in aliases.get(p, ()):
        if alias in source_ports:
            return alias
    if p.startswith("wmask"):
        mask_width = _range_width(width)
        return f"{mask_width}'b" + ("1" * mask_width)
    if p in {"vccd1", "vccd2", "vpwr", "vdd", "vdda", "vddp", "vpb"}:
        return "1'b1"
    if p in {"vssd1", "vssd2", "vgnd", "vss", "vssa", "vnb"}:
        return "1'b0"
    if p.startswith("dout"):
        return ""
    if p.startswith("clk") and "clk" in source_ports:
        return "clk"
    if p.startswith("csb") and "csb" in source_ports:
        return "csb"
    if p.startswith("addr") and "addr" in source_ports:
        return "addr"
    return None


def _source_module_ports(header: str, body: str) -> dict[str, dict[str, str]]:
    ports: dict[str, dict[str, str]] = {}
    last_direction = ""
    last_range = ""

    def clean_name(raw: str) -> str:
        name = re.sub(r"=.*$", "", raw).strip()
        name = re.sub(r"\[[^\]]+\]\s*$", "", name).strip()
        name = re.sub(r"^(?:input|output|inout|wire|reg|logic|signed)\s+", "", name).strip()
        name = re.sub(r"^(?:wire|reg|logic|signed)\s+", "", name).strip()
        name = re.sub(r"^\[[^\]]+\]\s*", "", name).strip()
        return name

    for raw in header.split(","):
        item = raw.strip()
        match = re.match(
            r"(?:(?P<direction>input|output|inout)\s+)?(?:wire|reg|logic|signed|\s)*\s*(?P<range>\[[^\]]+\])?\s*(?P<name>[A-Za-z_][A-Za-z0-9_$]*)$",
            item,
        )
        if not match:
            continue
        direction = match.group("direction") or last_direction or "input"
        port_range = (match.group("range") or last_range or "").strip()
        if match.group("direction"):
            last_direction = direction
            last_range = port_range
        ports[match.group("name")] = {"direction": direction, "range": port_range}

    for decl in re.finditer(
        r"\b(?P<direction>input|output|inout)\b\s*(?:wire|reg|logic|signed|\s)*\s*(?P<range>\[[^\]]+\])?\s*(?P<names>[^;]+);",
        body,
        flags=re.MULTILINE,
    ):
        direction = decl.group("direction")
        port_range = (decl.group("range") or "").strip()
        for raw_name in decl.group("names").split(","):
            name = clean_name(raw_name)
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
                ports[name] = {"direction": direction, "range": port_range}
    return ports


def _non_ansi_module_header(module_name: str, ports: dict[str, dict[str, str]]) -> str:
    names = list(ports)
    lines = [f"module {module_name} ({', '.join(names)});"]
    for name, meta in ports.items():
        direction = meta.get("direction") or "input"
        port_range = meta.get("range") or ""
        rng = f" {port_range}" if port_range else ""
        lines.append(f"  {direction}{rng} {name};")
    return "\n".join(lines)


def _replace_behavioral_memories_with_macros(rtl_files: list[str], collaterals: list[dict[str, object]]) -> dict[str, list[str]]:
    replacements: dict[str, list[str]] = {}
    by_module = {
        str(item.get("behavioral_module")): item
        for item in collaterals
        if item.get("behavioral_module") and item.get("macro_name")
    }
    if not by_module:
        return replacements
    for path in rtl_files:
        text = _read_text(path)
        if not text:
            continue
        original = text
        notes: list[str] = []
        for module, collateral in by_module.items():
            pattern = re.compile(
                rf"\bmodule\s+{re.escape(module)}\s*\((?P<header>.*?)\)\s*;(?P<body>.*?)(?:\n\s*endmodule\b)",
                flags=re.DOTALL,
            )

            def repl(match: re.Match) -> str:
                macro_name = str(collateral.get("macro_name"))
                macro_ports = (
                    _read_liberty_cell_ports(str(collateral.get("lib") or ""), macro_name)
                    or _read_verilog_module_ports(str(collateral.get("verilog") or ""), macro_name)
                )
                header = match.group("header").strip()
                source_port_meta = _source_module_ports(header, match.group("body"))
                source_ports = set(source_port_meta)
                if not macro_ports:
                    macro_ports = {
                        "clk0": "",
                        "csb0": "",
                        "web0": "",
                        "wmask0": "[3:0]",
                        "addr0": "",
                        "din0": "",
                        "dout0": "",
                    }
                conns = []
                for macro_port, width in macro_ports.items():
                    expr = _macro_connection_expr(macro_port, source_ports, width)
                    if expr is None:
                        continue
                    conns.append(f"    .{macro_port}({expr})")
                if not conns:
                    return match.group(0)
                notes.append(f"{module}->{macro_name}")
                wrapper_header = _non_ansi_module_header(module, source_port_meta)
                return (
                    f"{wrapper_header}\n\n"
                    f"  {macro_name} u_chiploop_sram_macro (\n"
                    + ",\n".join(conns)
                    + "\n  );\n\nendmodule"
                )

            text = pattern.sub(repl, text)
        if text != original:
            _write_local(path, text)
            replacements[os.path.basename(path)] = notes
    return replacements


def _top_internal_signal_decls(text: str) -> dict[str, str]:
    decls = {}
    for match in re.finditer(
        r"^\s*(?:logic|wire|reg)\s*(?P<range>\[[^\]]+\])?\s*(?P<names>[^;]+);",
        text,
        flags=re.MULTILINE,
    ):
        rng = (match.group("range") or "").strip()
        for raw in match.group("names").split(","):
            name = re.sub(r"\s*=.*$", "", raw).strip()
            name = re.sub(r"\[[^\]]+\]\s*$", "", name).strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
                decls[name] = rng
    return decls


def _top_instance_signal_uses(text: str) -> dict[str, list[dict[str, str]]]:
    uses: dict[str, list[dict[str, str]]] = {}
    inst_pat = re.compile(
        r"^\s*(?P<module>[A-Za-z_][A-Za-z0-9_$]*)\s+"
        r"(?P<inst>[A-Za-z_][A-Za-z0-9_$]*)\s*\((?P<body>.*?)\)\s*;",
        flags=re.MULTILINE | re.DOTALL,
    )
    for inst in inst_pat.finditer(text):
        module = inst.group("module")
        if module in {"module", "if", "for", "while", "case", "assign"}:
            continue
        for conn in re.finditer(r"\.(?P<port>[A-Za-z_][A-Za-z0-9_$]*)\s*\(\s*(?P<sig>[A-Za-z_][A-Za-z0-9_$]*)\s*\)", inst.group("body")):
            uses.setdefault(conn.group("sig"), []).append({
                "module": module,
                "instance": inst.group("inst"),
                "port": conn.group("port"),
            })
    return uses


def _top_ports_from_header(text: str, top_module: str) -> set[str]:
    match = re.search(rf"\bmodule\s+{re.escape(top_module)}\s*\((?P<header>.*?)\)\s*;", text, flags=re.DOTALL)
    if not match:
        return set()
    header = match.group("header")
    ports = set()
    for raw in header.split(","):
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", raw)
        if tokens:
            ports.add(tokens[-1])
    return ports


def _add_ansi_top_input(text: str, top_module: str, port_name: str, port_range: str) -> str:
    if port_name in _top_ports_from_header(text, top_module):
        return text
    match = re.search(rf"\bmodule\s+{re.escape(top_module)}\s*\((?P<header>.*?)\)\s*;", text, flags=re.DOTALL)
    if not match:
        return text
    header = match.group("header")
    range_part = f"{port_range} " if port_range else ""
    new_port = f"input logic {range_part}{port_name}"
    separator = ", " if header.strip() else ""
    return text[:match.start("header")] + header.rstrip() + separator + new_port + text[match.end("header"):]


def _remove_single_signal_decl(text: str, signal: str) -> str:
    return re.sub(
        rf"^\s*(?:logic|wire|reg)\s*(?:\[[^\]]+\]\s*)?{re.escape(signal)}\s*;\r?\n",
        "",
        text,
        flags=re.MULTILINE,
    )


def _repair_stale_input_only_interconnects(rtl_files: list[str], top_module: str) -> dict[str, list[str]]:
    port_db = _module_port_db_from_files(rtl_files)
    repairs = {}
    for path in rtl_files or []:
        if not _file_defines_module(path, top_module):
            continue
        text = _read_text(path)
        decls = _top_internal_signal_decls(text)
        uses_by_signal = _top_instance_signal_uses(text)
        top_ports = _top_ports_from_header(text, top_module)
        changes = []

        for signal, uses in sorted(uses_by_signal.items()):
            if signal not in decls or signal in top_ports:
                continue
            directions = [
                str((port_db.get(use["module"]) or {}).get(use["port"], {}).get("dir") or "").lower()
                for use in uses
            ]
            if not directions or any(direction != "input" for direction in directions):
                continue
            ports = [use["port"] for use in uses]
            existing_top_candidates = [port for port in ports if port in top_ports]
            if existing_top_candidates:
                promoted = existing_top_candidates[0]
            elif len(uses) >= 2:
                promoted = ports[0] if len(set(ports)) == 1 else signal
            else:
                continue
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", promoted):
                promoted = signal
            if promoted not in top_ports:
                text = _add_ansi_top_input(text, top_module, promoted, decls.get(signal, ""))
                top_ports.add(promoted)
            text = _remove_single_signal_decl(text, signal)
            if promoted != signal:
                text = re.sub(rf"\b{re.escape(signal)}\b", promoted, text)
            action = "reconnected" if existing_top_candidates else "promoted"
            target = "top port" if existing_top_candidates else "top input"
            changes.append(f"{action} input-only interconnect {signal} to {target} {promoted}")

        if changes:
            Path(path).write_text(text, encoding="utf-8")
            repairs[os.path.basename(path)] = changes
    return repairs


def _regenerate_system_physical_top_from_intent(rtl_files: list[str], top_module: str, state: dict) -> dict[str, list[str]]:
    intent = state.get("system_integration_intent")
    if not isinstance(intent, dict) or not intent.get("instances"):
        return {}

    module_port_db = _module_port_db_from_files(rtl_files)
    if not module_port_db:
        return {}

    repairs: dict[str, list[str]] = {}
    for path in rtl_files or []:
        if not _file_defines_module(path, top_module):
            continue
        regenerated = _assemble_top(top_module, intent, variant="phys", module_port_db=module_port_db)
        Path(path).write_text(regenerated, encoding="utf-8")
        repairs[os.path.basename(path)] = ["regenerated physical top from system integration intent"]
    return repairs

def _pick_clock(spec: dict) -> tuple[str, float]:
    """
    Returns (clock_name, clock_period_ns)
    Best-effort parsing, never fake precision.
    """
    # Try common shapes
    clk_name = "clk"
    clk_period = 10.0  # 100MHz default

    # examples you might have later:
    # spec["clock"]["name"], spec["clock"]["period_ns"], spec["clock"]["freq_mhz"]
    c = spec.get("clock") or {}
    if isinstance(c, dict):
        if c.get("name"):
            clk_name = str(c["name"])
        if c.get("period_ns"):
            try:
                clk_period = float(c["period_ns"])
            except Exception:
                pass
        elif c.get("freq_mhz"):
            try:
                mhz = float(c["freq_mhz"])
                if mhz > 0:
                    clk_period = 1000.0 / mhz
            except Exception:
                pass

    return clk_name, clk_period

def _write_local(path: str, content: str) -> None:
    _ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def _closure_bool(state: dict, closure: dict, key: str, default: bool = False) -> bool:
    toggles = state.get("toggles") if isinstance(state.get("toggles"), dict) else {}
    value = closure.get(key)
    if value is None:
        value = state.get(key)
    if value is None:
        value = toggles.get(key)
    if value is None:
        return default
    return bool(value)

def _synthesis_closure_profile(
    state: dict,
    closure: dict,
    iteration: int,
    timing_repair_enabled: bool,
    clock_period_ns: float,
) -> dict:
    allow_retiming = _closure_bool(state, closure, "allow_synthesis_retiming", False)
    allow_flattening = _closure_bool(state, closure, "allow_synthesis_hierarchy_flattening", False)
    if iteration <= 0 or not timing_repair_enabled:
        return {
            "enabled": False,
            "iteration": max(iteration, 0),
            "clock_margin": 1.0,
            "strategy": "AREA 0",
            "sdc_append": "",
            "config": {},
            "allow_retiming": allow_retiming,
            "allow_flattening": allow_flattening,
            "techniques": [],
        }

    clock_margin = max(0.70, 1.0 - (0.10 * iteration))
    critical_range = max(0.01, clock_period_ns * (0.10 if iteration == 1 else 0.15))
    group_weight = 3 if iteration == 1 else 5
    max_fanout = 8 if iteration == 1 else 6
    strategy = "DELAY 0" if iteration == 1 else "DELAY 1"
    config = {
        "SYNTH_STRATEGY": strategy,
        "SYNTH_BUFFERING": True,
        "SYNTH_SIZING": True,
        "MAX_FANOUT_CONSTRAINT": max_fanout,
    }
    if allow_retiming:
        config["SYNTH_ABC_DFF"] = True
    if allow_flattening:
        config["SYNTH_FLAT_TOP"] = True

    sdc_append = f"""

# ChipLoop synthesis closure iteration {iteration}: tool-only setup repair guidance.
# No RTL edits, ECO edits, or design-specific constraints are applied.
if {{[llength [all_registers]] > 0}} {{
  catch {{group_path -name chiploop_reg2reg_closure -from [all_registers] -to [all_registers] -critical_range {critical_range:.3f} -weight {group_weight}}}
}}
if {{[llength [all_inputs]] > 0 && [llength [all_registers]] > 0}} {{
  catch {{group_path -name chiploop_in2reg_closure -from [all_inputs] -to [all_registers] -critical_range {critical_range:.3f} -weight 2}}
}}
if {{[llength [all_registers]] > 0 && [llength [all_outputs]] > 0}} {{
  catch {{group_path -name chiploop_reg2out_closure -from [all_registers] -to [all_outputs] -critical_range {critical_range:.3f} -weight 2}}
}}
catch {{set_max_fanout {max_fanout} [current_design]}}
"""
    techniques = [
        f"clock_period_margin_{clock_margin:.2f}",
        f"synth_strategy_{strategy}",
        "synth_buffering",
        "synth_sizing",
        f"max_fanout_{max_fanout}",
        f"path_group_weight_{group_weight}",
        f"critical_range_{critical_range:.3f}ns",
    ]
    if allow_retiming:
        techniques.append("optional_retiming")
    if allow_flattening:
        techniques.append("optional_hierarchy_flattening")
    return {
        "enabled": True,
        "iteration": iteration,
        "clock_margin": clock_margin,
        "strategy": strategy,
        "sdc_append": sdc_append,
        "config": config,
        "allow_retiming": allow_retiming,
        "allow_flattening": allow_flattening,
        "techniques": techniques,
        "critical_range_ns": critical_range,
        "path_group_weight": group_weight,
        "max_fanout": max_fanout,
    }

def _run(cmd: list[str], cwd: str | None = None, state: dict | None = None) -> tuple[int, str]:
    p = run_command(state or {}, "digital_synthesis", [str(x) for x in cmd], cwd=cwd, timeout_sec=1800)
    return p.returncode if p.returncode is not None else 1, (p.stdout or "") + (p.stderr or "")

def _resolve_rtl_files_from_state(state: dict, workflow_dir: str) -> list[str]:
    digital = state.get("digital") or {}

    for key, cands in (
        ("state.digital.rtl_files", digital.get("rtl_files")),
        ("state.rtl_files", state.get("rtl_files")),
        ("state.rtl_inputs", state.get("rtl_inputs")),
        ("state.source_rtl_files", state.get("source_rtl_files")),
        ("state.artifact_list", state.get("artifact_list")),
    ):
        if not isinstance(cands, list):
            continue
        xs = [_existing_path(p, workflow_dir) for p in cands]
        xs = _dedupe_paths([p for p in xs if p])
        if xs:
            logger.info(f"{AGENT_NAME}: selected rtl_files from {key} -> {len(xs)} files")
            return xs

    fl = _existing_path(digital.get("impl_filelist"), workflow_dir)
    if fl:
        xs = []
        with open(fl, "r", encoding="utf-8") as f:
            for line in f:
                p = _existing_path(line.strip(), workflow_dir)
                if p:
                    xs.append(p)
        if xs:
            xs = _dedupe_paths(xs)
            logger.info(f"{AGENT_NAME}: selected rtl_files from impl_filelist -> {len(xs)} files")
            return xs

    single = _existing_path(state.get("artifact"), workflow_dir)
    if single and single.lower().endswith((".v", ".sv")):
        logger.info(f"{AGENT_NAME}: selected rtl_files from state.artifact -> 1 file")
        return [single]

    patterns = [
        os.path.join(workflow_dir, "handoff", "rtl", "**", "*.v"),
        os.path.join(workflow_dir, "handoff", "rtl", "**", "*.sv"),
        os.path.join(workflow_dir, "digital", "handoff", "rtl", "**", "*.v"),
        os.path.join(workflow_dir, "digital", "handoff", "rtl", "**", "*.sv"),
        os.path.join(workflow_dir, "digital", "rtl_refactored", "**", "*.v"),
        os.path.join(workflow_dir, "digital", "rtl_refactored", "**", "*.sv"),
        os.path.join(workflow_dir, "digital", "rtl", "**", "*.v"),
        os.path.join(workflow_dir, "digital", "rtl", "**", "*.sv"),
    ]
    xs = _dedupe_paths([p for pattern in patterns for p in sorted(glob.glob(pattern, recursive=True))])
    if xs:
        logger.info(f"{AGENT_NAME}: selected rtl_files from workflow glob -> {len(xs)} files")
        return xs

    return []

def _resolve_macro_libs_from_state(state: dict, workflow_dir: str) -> list[str]:
    digital = state.get("digital") or {}

    candidates = []

    if isinstance(digital.get("macro_libs"), list):
        candidates.extend(digital["macro_libs"])

    macro_lib_filelist = digital.get("macro_lib_filelist")
    if macro_lib_filelist and os.path.exists(macro_lib_filelist):
        with open(macro_lib_filelist, "r", encoding="utf-8") as f:
            for line in f:
                p = line.strip()
                if p:
                    candidates.append(p)

    out = []
    seen = set()
    for p in candidates:
        abs_p = p if os.path.isabs(p) else os.path.join(workflow_dir, p)
        abs_p = os.path.abspath(abs_p)
        if os.path.exists(abs_p) and abs_p not in seen:
            seen.add(abs_p)
            out.append(abs_p)

    return out

def run_agent(state: dict) -> dict:
    print(f"\n🏁 Running {AGENT_NAME}...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    _ensure_dir(workflow_dir)

    logger.info(f"🏁 Running {AGENT_NAME}.")
    rtl_files = _resolve_rtl_files_from_state(state, workflow_dir)

    if not rtl_files:
        artifact_list = state.get("artifact_list") or []
        if isinstance(artifact_list, list) and artifact_list:
            rtl_files = _dedupe_paths([p for p in (_existing_path(x, workflow_dir) for x in artifact_list) if p])

    if not rtl_files:
        single = _existing_path(state.get("artifact"), workflow_dir)
        if single:
            rtl_files = [single]

    if not rtl_files:
        raise FileNotFoundError(f"No RTL found for synthesis in {workflow_dir}")

    logger.info(f"{AGENT_NAME}: rtl_count={len(rtl_files)}")


    # ---------- Spec JSON (optional) ----------

    spec = {}
    spec_json = _resolve_spec_json(state, workflow_dir)
    if spec_json and os.path.exists(spec_json):
        spec = _read_json(spec_json)

    logger.info(f"{AGENT_NAME}: spec_json={spec_json}")

    clk_name, clk_period_ns = _pick_clock(spec)
    try:
        requested_mhz = float(state.get("target_frequency_mhz")) if state.get("target_frequency_mhz") is not None else 0.0
    except Exception:
        requested_mhz = 0.0
    if requested_mhz > 0:
        clk_period_ns = 1000.0 / requested_mhz
    try:
        closure_iteration = int(state.get("synthesis_closure_iteration_index") or 0)
    except Exception:
        closure_iteration = 0
    closure = state.get("synthesis_closure") if isinstance(state.get("synthesis_closure"), dict) else {}
    timing_repair_enabled = bool(
        closure.get("allow_synthesis_timing_repair")
        if closure.get("allow_synthesis_timing_repair") is not None
        else state.get("allow_synthesis_timing_repair", True)
    )
    closure_profile = _synthesis_closure_profile(state, closure, closure_iteration, timing_repair_enabled, clk_period_ns)
    if closure_profile.get("enabled"):
        clk_period_ns = max(0.1, clk_period_ns * float(closure_profile.get("clock_margin") or 1.0))

    # ---------- Stage folder ----------
    stage_dir = os.path.join(workflow_dir, "digital", "synth")
    rtl_dir = os.path.join(stage_dir, "rtl")
    constraints_dir = os.path.join(stage_dir, "constraints")
    logs_dir = os.path.join(stage_dir, "logs")
    macro_lib_dir = os.path.join(stage_dir, "macro_libs")
    macro_lef_dir = os.path.join(stage_dir, "macro_lefs")
    macro_gds_dir = os.path.join(stage_dir, "macro_gds")
    macro_spice_dir = os.path.join(stage_dir, "macro_spice")
    macro_verilog_dir = os.path.join(stage_dir, "macro_verilog")
    _ensure_dir(rtl_dir)
    _ensure_dir(constraints_dir)
    _ensure_dir(logs_dir)
    _ensure_dir(macro_lib_dir)
    _ensure_dir(macro_lef_dir)
    _ensure_dir(macro_gds_dir)
    _ensure_dir(macro_spice_dir)
    _ensure_dir(macro_verilog_dir)

    macro_libs = _resolve_macro_libs_from_state(state, workflow_dir)
    # Copy RTL into deterministic local folder (avoid container path issues)
    copied = []
    rtl_repairs: dict[str, list[str]] = {}
    for f in rtl_files:
        dst = os.path.join(rtl_dir, os.path.basename(f))
        if os.path.abspath(f) != os.path.abspath(dst):
            shutil.copy2(f, dst)
        repairs = _repair_common_status_tieoffs(dst)
        if repairs:
            rtl_repairs[os.path.basename(dst)] = repairs
        copied.append(dst)

    inferred_memory_arrays = _large_inferred_memory_arrays(copied)
    pdk_sram_collateral = _discover_pdk_sram_collateral(state, workflow_dir, inferred_memory_arrays)
    macro_replacements = _replace_behavioral_memories_with_macros(copied, pdk_sram_collateral)
    if macro_replacements:
        for file_name, repairs in macro_replacements.items():
            rtl_repairs.setdefault(file_name, []).extend([f"bound_inferred_memory_to_pdk_macro:{item}" for item in repairs])

    macro_lefs = []
    macro_gds = []
    macro_spice = []
    macro_verilog = []
    for item in pdk_sram_collateral:
        for src_key, bucket in (
            ("lib", macro_libs),
            ("lef", macro_lefs),
            ("gds", macro_gds),
            ("spice", macro_spice),
            ("verilog", macro_verilog),
        ):
            value = item.get(src_key)
            if isinstance(value, str) and value:
                bucket.append(value)
    macro_libs = _dedupe_paths([p for p in macro_libs if os.path.isfile(p)])
    macro_lefs = _dedupe_paths([p for p in macro_lefs if os.path.isfile(p)])
    macro_gds = _dedupe_paths([p for p in macro_gds if os.path.isfile(p)])
    macro_spice = _dedupe_paths([p for p in macro_spice if os.path.isfile(p)])
    macro_verilog = _dedupe_paths([p for p in macro_verilog if os.path.isfile(p)])
    logger.info(
        f"{AGENT_NAME}: macro collateral counts lib={len(macro_libs)} lef={len(macro_lefs)} "
        f"gds={len(macro_gds)} spice={len(macro_spice)} verilog={len(macro_verilog)}"
    )

    def _copy_to_dir(srcs: list[str], dst_dir: str) -> list[str]:
        copied_paths = []
        for src in srcs:
            dst = os.path.join(dst_dir, os.path.basename(src))
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copy2(src, dst)
            copied_paths.append(dst)
        return copied_paths

    copied_macro_libs = _copy_to_dir(macro_libs, macro_lib_dir)
    copied_macro_lefs = _copy_to_dir(macro_lefs, macro_lef_dir)
    copied_macro_gds = _copy_to_dir(macro_gds, macro_gds_dir)
    copied_macro_spice = _copy_to_dir(macro_spice, macro_spice_dir)
    copied_macro_verilog = _copy_to_dir(macro_verilog, macro_verilog_dir)

    # Pick top module name best-effort. Digital synthesis keeps the digital top;
    # System Synthesis/PD intentionally use the physical SoC wrapper.
    workflow_name = str(
        state.get("template_workflow_name")
        or state.get("template_workflow")
        or state.get("workflow_name")
        or ""
    )
    is_system_physical_flow = workflow_name in {"System_Synthesis", "System_PD"}
    package = state.get("system_rtl_package") if isinstance(state.get("system_rtl_package"), dict) else {}
    package_top = package.get("top") if isinstance(package.get("top"), dict) else {}

    if is_system_physical_flow:
        top_module = (
            package_top.get("phys")
            or package_top.get("sim")
            or state.get("soc_top_phys_module")
            or state.get("soc_top_sim_module")
            or (state.get("digital") or {}).get("top_module")
            or (spec.get("design_name") if isinstance(spec, dict) else None)
            or (((spec.get("hierarchy") or {}).get("top_module") or {}).get("name") if isinstance(spec, dict) else None)
            or (spec.get("top_module") if isinstance(spec, dict) else None)
            or (spec.get("name") if isinstance(spec, dict) else None)
            or ((spec.get("block") or {}).get("name") if isinstance(spec.get("block"), dict) else None)
            or state.get("top_module")
            or "top"
        )
    else:
        top_module = (
            (state.get("digital") or {}).get("top_module")
            or (spec.get("design_name") if isinstance(spec, dict) else None)
            or (((spec.get("hierarchy") or {}).get("top_module") or {}).get("name") if isinstance(spec, dict) else None)
            or (spec.get("top_module") if isinstance(spec, dict) else None)
            or (spec.get("name") if isinstance(spec, dict) else None)
            or ((spec.get("block") or {}).get("name") if isinstance(spec.get("block"), dict) else None)
            or state.get("top_module")
            or package_top.get("sim")
            or package_top.get("phys")
            or state.get("soc_top_sim_module")
            or state.get("soc_top_phys_module")
            or "top"
        )
    top_module = str(top_module).strip()
    logger.info(f"{AGENT_NAME}: top_module={top_module}")

    if is_system_physical_flow:
        regenerated_repairs = _regenerate_system_physical_top_from_intent(copied, top_module, state)
        for file_name, repairs in regenerated_repairs.items():
            rtl_repairs.setdefault(file_name, []).extend(repairs)

    interconnect_repairs = _repair_stale_input_only_interconnects(copied, top_module)
    for file_name, repairs in interconnect_repairs.items():
        rtl_repairs.setdefault(file_name, []).extend(repairs)

    state["design_name"] = top_module

    inferred_memory_arrays = _large_inferred_memory_arrays(copied)
    if inferred_memory_arrays and not copied_macro_libs:
        reason = "inferred_memory_macro_requires_real_macro_collateral"
        msg = (
            "Large inferred memory arrays were found in RTL, but no macro Liberty collateral was available. "
            "Synthesis is blocked to avoid flattening SRAM/MBIST memories into standard-cell flops."
        )
        input_log_path = os.path.join(stage_dir, "synth_input_resolution.log")
        exec_log_path = os.path.join(logs_dir, "openlane_synth.log")
        summary_json_path = os.path.join(stage_dir, "synth_summary.json")
        summary_md_path = os.path.join(stage_dir, "synth_summary.md")
        metrics_path = os.path.join(stage_dir, "metrics.json")
        input_log = "\n".join([
            f"[{datetime.utcnow().isoformat()}Z] {AGENT_NAME}",
            f"workflow_id={workflow_id}",
            f"workflow_dir={os.path.abspath(workflow_dir)}",
            f"spec_json={spec_json}",
            f"top_module={top_module}",
            f"rtl_count={len(rtl_files)}",
            "macro_lib_count=0",
            f"status=blocked",
            f"reason={reason}",
            f"inferred_memory_arrays={json.dumps(inferred_memory_arrays, sort_keys=True)}",
        ]) + "\n"
        summary = {
            "workflow_id": workflow_id,
            "agent": AGENT_NAME,
            "status": "blocked",
            "return_code": None,
            "reason": reason,
            "message": msg,
            "inputs": {
                "rtl_files": [os.path.basename(x) for x in copied],
                "pre_synthesis_rtl_repairs": rtl_repairs,
                "macro_libs": [],
                "top_module": top_module,
                "clock_port": clk_name,
                "clock_period_ns": clk_period_ns,
                "inferred_memory_arrays": inferred_memory_arrays,
            },
            "outputs": {
                "stage_dir": stage_dir,
                "config_json": None,
                "sdc": None,
                "run_sh": None,
                "exec_log": exec_log_path,
                "metrics_json": metrics_path,
                "netlist": None,
                "netlist_candidates": [],
                "enriched_metrics": False,
            },
        }
        md = (
            "# Digital Synthesis Summary\n\n"
            f"- **Status**: `blocked`\n"
            f"- **Reason**: `{reason}`\n"
            f"- **Top module**: `{top_module}`\n"
            f"- **Inferred memory arrays**: `{len(inferred_memory_arrays)}`\n\n"
            f"{msg}\n"
        )
        _write_local(input_log_path, input_log)
        _write_local(exec_log_path, msg + "\n")
        _write_local(summary_json_path, json.dumps(summary, indent=2))
        _write_local(summary_md_path, md)
        _write_local(metrics_path, json.dumps({
            "status": "blocked",
            "reason": reason,
            "inferred_memory_array_count": len(inferred_memory_arrays),
            "inferred_memory_bit_count": sum(int(item.get("estimated_bits") or 0) for item in inferred_memory_arrays),
        }, indent=2))
        digital = state.setdefault("digital", {})
        digital["synth"] = {
            "stage_dir": stage_dir,
            "summary_json": summary_json_path,
            "summary_md": summary_md_path,
            "metrics_json": metrics_path,
            "netlist": None,
            "netlist_candidates": [],
            "status": "blocked",
            "reason": reason,
            "input_resolution_log": input_log_path,
            "constraints_sdc": None,
            "upstream_constraints_sdc": None,
            "rtl_files": rtl_files,
            "top_module": top_module,
            "inferred_memory_arrays": inferred_memory_arrays,
        }
        state["status"] = f"{AGENT_NAME}: blocked ({reason})"
        try:
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "synth/logs/openlane_synth.log", msg + "\n")
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "synth/synth_summary.json", json.dumps(summary, indent=2))
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "synth/synth_summary.md", md)
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "synth/synth_input_resolution.log", input_log)
            save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "synth/metrics.json", json.dumps({
                "status": "blocked",
                "reason": reason,
                "inferred_memory_array_count": len(inferred_memory_arrays),
                "inferred_memory_bit_count": sum(int(item.get("estimated_bits") or 0) for item in inferred_memory_arrays),
            }, indent=2))
        except Exception as e:
            print(f"⚠️ Synthesis blocked artifact upload failed: {e}")
        raise RuntimeError(f"{AGENT_NAME}: {reason}")

    # ---------- SDC (single source of truth) ----------

    upstream_sdc = _resolve_sdc_from_state(state, workflow_dir)
    if not upstream_sdc:
        impl_glob = sorted(glob.glob(os.path.join(workflow_dir, "digital", "impl_setup", "constraints", "*.sdc")))
        legacy_glob = sorted(glob.glob(os.path.join(workflow_dir, "digital", "constraints", "*.sdc")))

        msg = (
            "Missing upstream SDC: checked "
            f"state.digital.constraints_sdc={(state.get('digital') or {}).get('constraints_sdc')}, "
            f"impl_setup_candidates={impl_glob}, "
            f"legacy_candidates={legacy_glob}"
        )
        exec_log_path = os.path.join(logs_dir, "openlane_synth.log")
        _write_local(exec_log_path, msg + "\n")
        _write_local(os.path.join(stage_dir, "synth_input_resolution.log"), msg + "\n")

        summary = {"status": "failed", "return_code": 2, "error": msg}
        _write_local(os.path.join(stage_dir, "synth_summary.json"), json.dumps(summary, indent=2))
        _write_local(os.path.join(stage_dir, "synth_summary.md"), f"# Digital Synthesis Summary\n\n- **Status**: failed\n- **Reason**: {msg}\n")
        _write_local(os.path.join(stage_dir, "metrics.json"), json.dumps({"status": "failed", "reason": msg}, indent=2))

        logger.error(f"{AGENT_NAME}: {msg}")
        state["status"] = f"{AGENT_NAME}: failed (missing SDC)"
        return state

    logger.info(f"{AGENT_NAME}: using upstream_sdc={upstream_sdc}")

    sdc_basename = os.path.basename(upstream_sdc)
    sdc_path = os.path.join(constraints_dir, sdc_basename)
    shutil.copy2(upstream_sdc, sdc_path)

    logger.info(f"{AGENT_NAME}: copied SDC into synth stage -> {sdc_path}")
    logger.info(f"{AGENT_NAME}: synth sdc exists={os.path.exists(sdc_path)}")
    logger.info(f"{AGENT_NAME}: synth sdc size={os.path.getsize(sdc_path) if os.path.exists(sdc_path) else -1}")
 
    with open(sdc_path, "r", encoding="utf-8") as f:
       sdc_text = f.read()
    if closure_profile.get("sdc_append"):
        sdc_text = sdc_text.rstrip() + str(closure_profile.get("sdc_append") or "")
        _write_local(sdc_path, sdc_text)

    yosys_pre_path = os.path.join(stage_dir, "yosys_macro_libs.ys")
    yosys_pre_text = "\n".join(
        [f"read_liberty -lib macro_libs/{os.path.basename(p)}" for p in copied_macro_libs]
    ) + ("\n" if copied_macro_libs else "")
    _write_local(yosys_pre_path, yosys_pre_text)

    input_log = "\n".join([
        f"[{datetime.utcnow().isoformat()}Z] {AGENT_NAME}",
        f"workflow_id={workflow_id}",
        f"workflow_dir={os.path.abspath(workflow_dir)}",
        f"spec_json={spec_json}",
        f"top_module={top_module}",
        f"rtl_count={len(rtl_files)}",
        f"request_target_frequency_mhz={state.get('target_frequency_mhz')}",
        f"clock_period_ns={clk_period_ns}",
        f"upstream_sdc={upstream_sdc}",
        f"sdc_basename={sdc_basename}",
        f"synth_sdc_path={sdc_path}",
        f"state_constraints_sdc={(state.get('digital') or {}).get('constraints_sdc')}",
        f"pdk_variant={state.get('pdk_variant') or DEFAULT_PDK_VARIANT}",
        f"macro_lib_count={len(copied_macro_libs)}",
        f"macro_lef_count={len(copied_macro_lefs)}",
        f"macro_gds_count={len(copied_macro_gds)}",
        f"macro_spice_count={len(copied_macro_spice)}",
        f"macro_verilog_count={len(copied_macro_verilog)}",
        f"pdk_sram_collateral={json.dumps([{k: (os.path.basename(v) if isinstance(v, str) else v) for k, v in item.items()} for item in pdk_sram_collateral], sort_keys=True)}",
        f"yosys_macro_lib_script={yosys_pre_path}",
        f"pre_synthesis_rtl_repairs={json.dumps(rtl_repairs, sort_keys=True)}",
    ]) + "\n"

    input_log_path = os.path.join(stage_dir, "synth_input_resolution.log")
    _write_local(input_log_path, input_log)
  

    # ---------- OpenLane2 config.json ----------
    # Keep it minimal and explicit.
    # PDK selection is via CLI/env; do NOT hardcode absolute host paths here.
    config_path = os.path.join(stage_dir, "config.json")

    # OpenLane2 supports JSON configs; we keep sources relative inside the mounted /work
    verilog_sources = [f"rtl/{os.path.basename(p)}" for p in copied]


    config = {
        "DESIGN_NAME": top_module,
        "VERILOG_FILES": verilog_sources,
        "CLOCK_PORT": clk_name,
        "CLOCK_PERIOD": clk_period_ns,
        "SYNTH_STRATEGY": closure_profile.get("strategy") or "AREA 0",
        "SYNTH_SDC_FILE": f"constraints/{sdc_basename}",
        "PNR_SDC_FILE": f"constraints/{sdc_basename}",

        # Make OpenLane/Yosys aware of macro timing libs
        "EXTRA_LIBS": [f"dir::macro_libs/{os.path.basename(p)}" for p in copied_macro_libs],
        "EXTRA_LEFS": [f"dir::macro_lefs/{os.path.basename(p)}" for p in copied_macro_lefs],
        "EXTRA_GDS_FILES": [f"dir::macro_gds/{os.path.basename(p)}" for p in copied_macro_gds],

        # ChipLoop provenance (OpenLane ignores unknown top-level keys)
        "CHIPLOOP_WORKFLOW_ID": workflow_id,
        "CHIPLOOP_GENERATED_BY": AGENT_NAME,
        "CHIPLOOP_GENERATED_AT": datetime.utcnow().isoformat() + "Z",
        "CHIPLOOP_MACRO_LIBS": [f"macro_libs/{os.path.basename(p)}" for p in copied_macro_libs],
        "CHIPLOOP_MACRO_LEFS": [f"macro_lefs/{os.path.basename(p)}" for p in copied_macro_lefs],
        "CHIPLOOP_MACRO_GDS": [f"macro_gds/{os.path.basename(p)}" for p in copied_macro_gds],
        "CHIPLOOP_MACRO_SPICE": [f"macro_spice/{os.path.basename(p)}" for p in copied_macro_spice],
        "CHIPLOOP_MACRO_VERILOG": [f"macro_verilog/{os.path.basename(p)}" for p in copied_macro_verilog],
        "CHIPLOOP_YOSYS_MACRO_LIB_SCRIPT": "yosys_macro_libs.ys",
        # ✅ KEY FIX: Disable Verilator lint stage
        "RUN_LINTER": False
    }
    config.update(closure_profile.get("config") or {})

    
    _write_local(config_path, json.dumps(config, indent=2))

    logger.info(
        f"{AGENT_NAME}: config EXTRA_LIBS={config.get('EXTRA_LIBS', [])}"
    )
    logger.info(
        f"{AGENT_NAME}: yosys_macro_lib_script={yosys_pre_path}"
    )

    # ---------- Docker run.sh (rerunnable contract) ----------
    # Host PDK root: your real path is backend/pdk (you already created it)
    # We keep it configurable.
    default_pdk_host = _pdk_root_host(state)
    pdk_variant = _pdk_variant(state)
    openlane_image = state.get("openlane_image") or DEFAULT_OPENLANE_IMAGE

    # Runs folder inside stage_dir
    # OpenLane2 will create a "runs/<tag>" directory under CWD by default.
    explicit = state.get("run_tag") or state.get("digital_run_tag")
    wf_name = state.get("workflow_name") or "digital"
    run_tag = explicit or f"{wf_name}_{workflow_id}"
    state["digital_run_tag"] = run_tag


    macro_lib_read_cmd = ""
    if copied_macro_libs:
        macro_lib_read_cmd = " ".join(
            [f"read_liberty -lib macro_libs/{os.path.basename(p)};" for p in copied_macro_libs]
        )

    run_sh_path = os.path.join(stage_dir, "run.sh")


    run_sh = f"""#!/usr/bin/env bash
set -euo pipefail

echo "== ChipLoop: {AGENT_NAME} =="
echo "PDK_VARIANT={pdk_variant}"
echo "OPENLANE_IMAGE={openlane_image}"
echo "WORKDIR=/work"
echo "MACRO_LIB_COUNT={len(copied_macro_libs)}"
echo

docker run --rm \\
  -v "{default_pdk_host}:/pdk" \\
  -v "{os.path.abspath(stage_dir)}:/work" \\
  -e PDK_ROOT=/pdk \\
  -e PDK={pdk_variant} \\
  -e OPENLANE_NUM_CORES={DEFAULT_NUM_CORES} \\
  "{openlane_image}" \\
  bash -lc '
    set -e
    echo "PDK listing:"
    ls -la /pdk | head -n 50
    test -f /pdk/sky130A/libs.tech/openlane/config.tcl
    cd /work

    if [ -d macro_libs ]; then
      echo "Using macro Liberty blackbox libraries:"
      ls -la macro_libs || true
    fi

    # Run OpenLane synthesis through pre-PnR STA. This keeps the Synth app
    # lightweight while producing real OpenSTA/OpenROAD WNS/TNS metrics.
    openlane --pdk {pdk_variant} --run-tag {run_tag} --flow Classic --override-config RUN_LINTER=False --to {OPENLANE_SYNTH_TO_STEP} config.json

    # Patch the synthesized design with explicit Liberty blackbox load if macro libs exist
    if [ -n "{macro_lib_read_cmd}" ]; then
      echo "Applying Liberty blackbox integration post-synthesis..."
      echo "{macro_lib_read_cmd}" > /tmp/chiploop_macro_libs.ys
      cat /tmp/chiploop_macro_libs.ys
    fi
  '

echo
echo "Done. Inspect /work/runs/{run_tag} or latest run folder under /work/runs/"
"""


    _write_local(run_sh_path, run_sh)
    os.chmod(run_sh_path, 0o755)

    # ---------- Execute synthesis + pre-PnR STA (best-effort) ----------
    # Stop at pre-PnR STA so this app reports real WNS/TNS without place/route.
    exec_log_path = os.path.join(logs_dir, "openlane_synth.log")

    rc, out = _run(["bash", "./run.sh"], cwd=stage_dir, state=state)
    _write_local(exec_log_path, out)

    # ---------- Collect best-effort outputs ----------
    # We don’t fake timing. We only parse what exists.
    runs_dir = os.path.join(stage_dir, "runs")
    metrics_json = None
    stable_metrics_path = os.path.join(stage_dir, "metrics.json")
    netlist_candidates = []
    stable_netlist_path = None


    if os.path.isdir(runs_dir):
        # find newest run folder
        run_folders = [os.path.join(runs_dir, d) for d in os.listdir(runs_dir) if os.path.isdir(os.path.join(runs_dir, d))]
        run_folders.sort(key=lambda p: os.path.getmtime(p))
        latest = run_folders[-1] if run_folders else None

        if latest:
            mj = os.path.join(latest, "final", "metrics.json")
            if os.path.exists(mj):
                metrics_json = mj

            # Always export stable metrics path if available
            stable_metrics_path = os.path.join(stage_dir, "metrics.json")
            if latest:
                mj = os.path.join(latest, "final", "metrics.json")
                if os.path.exists(mj):
                    shutil.copy2(mj, stable_metrics_path)

            # synthesis step folders often contain gate-level netlists


            # synthesis step folders often contain gate-level netlists
            netlist_candidates = glob.glob(os.path.join(latest, "*yosys-synthesis", "*.v")) + \
                                 glob.glob(os.path.join(latest, "*yosys-synthesis", "outputs", "*.v"))

            # ---------- Persist primary synthesized netlist (stable path) ----------
            stable_netlist_path = None
            if netlist_candidates:
                try:
                    # pick the first candidate deterministically
                    primary = netlist_candidates[0]
                    netlist_dir = os.path.join(stage_dir, "netlist")
                    _ensure_dir(netlist_dir)
                    stable_netlist_path = os.path.join(netlist_dir, f"{top_module}_synth.v")
                    shutil.copy2(primary, stable_netlist_path)
                except Exception as e:
                    print(f"⚠️ Failed to persist netlist: {e}")
                    stable_netlist_path = None

    enriched_metrics = {}
    if os.path.exists(stable_metrics_path):
        enriched_metrics = _augment_synth_metrics(stable_metrics_path, stable_netlist_path)

    summary = {
        "workflow_id": workflow_id,
        "agent": AGENT_NAME,
        "status": "ok" if rc == 0 else "failed",
        "return_code": rc,
        "inputs": {
            "rtl_files": [os.path.basename(x) for x in copied],
            "pre_synthesis_rtl_repairs": rtl_repairs,
            "macro_libs": [os.path.basename(x) for x in copied_macro_libs],
            "macro_lefs": [os.path.basename(x) for x in copied_macro_lefs],
            "macro_gds": [os.path.basename(x) for x in copied_macro_gds],
            "macro_spice": [os.path.basename(x) for x in copied_macro_spice],
            "macro_verilog": [os.path.basename(x) for x in copied_macro_verilog],
            "pdk_sram_collateral": [
                {k: (os.path.basename(v) if isinstance(v, str) else v) for k, v in item.items()}
                for item in pdk_sram_collateral
            ],
            "top_module": top_module,
            "clock_port": clk_name,
            "clock_period_ns": clk_period_ns,
            "synthesis_closure_iteration": closure_iteration,
            "synthesis_closure_timing_repair_enabled": timing_repair_enabled,
            "synthesis_closure_optimization_profile": {
                k: v for k, v in closure_profile.items()
                if k not in {"sdc_append", "config"}
            },
            "synthesis_closure_openlane_overrides": closure_profile.get("config") or {},
            "pdk_variant": pdk_variant,
            "openlane_image": openlane_image,
            "openlane_to_step": OPENLANE_SYNTH_TO_STEP,
            "pdk_root_host": default_pdk_host,
        },
        "outputs": {
            "stage_dir": stage_dir,
            "config_json": config_path,
            "sdc": sdc_path,
            "run_sh": run_sh_path,
            "exec_log": exec_log_path,
            "metrics_json": stable_metrics_path if os.path.exists(stable_metrics_path) else None,
            "netlist": stable_netlist_path,
            "netlist_candidates": netlist_candidates[:10],
            "enriched_metrics": bool(enriched_metrics),
        }
    }

    summary_json_path = os.path.join(stage_dir, "synth_summary.json")
    summary_md_path = os.path.join(stage_dir, "synth_summary.md")
    _write_local(summary_json_path, json.dumps(summary, indent=2))

    md = f"""# Digital Synthesis Summary

- **Workflow**: {workflow_id}
- **Status**: `{summary["status"]}` (rc={rc})
- **Top module**: `{top_module}`
- **Clock**: `{clk_name}` @ **{clk_period_ns:.3f} ns**
- **PDK**: `{pdk_variant}`
- **Image**: `{openlane_image}`
- **OpenLane stop step**: `{OPENLANE_SYNTH_TO_STEP}`

## Deterministic outputs (rerunnable)
- `digital/synth/config.json`
- `digital/synth/constraints/top.sdc`
- `digital/synth/run.sh`
- `digital/synth/logs/openlane_synth.log`

## Parsed outputs (best-effort)
- metrics.json: `{metrics_json or "not found"}`
- netlist candidates: {len(netlist_candidates)} found
"""
    _write_local(summary_md_path, md)

    # ---------- Upload key artifacts to Supabase Storage ----------
    # (Option A: store heavy locally, upload key collaterals + summaries)
    try:
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "synth/config.json", json.dumps(config, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", f"synth/constraints/{sdc_basename}",sdc_text)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "synth/run.sh", run_sh)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "synth/logs/openlane_synth.log", out)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "synth/synth_summary.json", json.dumps(summary, indent=2))
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "synth/synth_summary.md", md)
        save_text_artifact_and_record(workflow_id, AGENT_NAME, "digital", "synth/synth_input_resolution.log", input_log)
        # Upload synthesized netlist (gate-level) if present
        if stable_netlist_path and os.path.exists(stable_netlist_path):
            with open(stable_netlist_path, "r", encoding="utf-8") as f:
                save_text_artifact_and_record(
                    workflow_id,
                    AGENT_NAME,
                    "digital",
                    f"synth/netlist/{top_module}_synth.v",
                    f.read()
                )

        # Upload metrics.json if present
        if os.path.exists(stable_metrics_path):
            with open(stable_metrics_path, "r", encoding="utf-8") as f:
                save_text_artifact_and_record(
                    workflow_id,
                    AGENT_NAME,
                    "digital",
                    "synth/metrics.json",
                    f.read()
                )
        print("✅ Uploaded synthesis artifacts to Supabase.")
    except Exception as e:
        print(f"⚠️ Synthesis artifact upload failed: {e}")

    # ---------- Update state for downstream workflow ----------

    digital = state.setdefault("digital", {})
    def _existing_list(value) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if isinstance(item, str)]
        if isinstance(value, str):
            return [value]
        return []

    if macro_libs:
        digital["macro_libs"] = _dedupe_paths(_existing_list(digital.get("macro_libs")) + macro_libs)
    if macro_lefs:
        digital["macro_lefs"] = _dedupe_paths(_existing_list(digital.get("macro_lefs")) + macro_lefs)
    if macro_gds:
        digital["macro_gds"] = _dedupe_paths(_existing_list(digital.get("macro_gds")) + macro_gds)
    if macro_spice:
        digital["macro_spice_models"] = _dedupe_paths(_existing_list(digital.get("macro_spice_models")) + macro_spice)
        digital["macro_lvs_spice"] = _dedupe_paths(_existing_list(digital.get("macro_lvs_spice")) + macro_spice)
    digital["synth"] = {
        "stage_dir": stage_dir,
        "summary_json": summary_json_path,
        "summary_md": summary_md_path,
        "metrics_json": metrics_json,
        "netlist": stable_netlist_path,
        "netlist_candidates": netlist_candidates[:10],
        "status": summary["status"],
        "input_resolution_log": input_log_path,
        "constraints_sdc": sdc_path,
        "upstream_constraints_sdc": upstream_sdc,
        "rtl_files": rtl_files,
        "synth_rtl_files": copied,
        "top_module": top_module,
        "macro_libs": macro_libs,
        "macro_lefs": macro_lefs,
        "macro_gds": macro_gds,
        "macro_spice_models": macro_spice,
        "macro_verilog": macro_verilog,
        "pdk_sram_collateral": pdk_sram_collateral,
    }
    
    state["status"] = f"{AGENT_NAME}: {summary['status']}"
    if summary["status"] != "ok" or not stable_netlist_path:
        raise RuntimeError(
            f"{AGENT_NAME}: synthesis failed before downstream PD stages "
            f"(status={summary['status']}, rc={rc}, netlist={'present' if stable_netlist_path else 'missing'})"
        )
    return state
