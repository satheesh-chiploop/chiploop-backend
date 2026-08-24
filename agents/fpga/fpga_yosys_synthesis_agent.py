import json
import os
import re
import subprocess
from functools import lru_cache
from .fpga_common import board_config, fpga_dir, manifest_update, publish_json, run_cmd, write_json, write_text


def _architecture_synth_options(board: dict, help_text: str = "") -> list[str]:
    family = str(board.get("family") or "").lower()
    yosys_family = str(board.get("yosys_family") or "").strip()
    if family == "nexus" and yosys_family and "-family" in help_text:
        return ["-family", yosys_family]
    return []


def _yosys_cell_metrics(json_path: str, board: dict) -> dict:
    resources = board.get("resources") or {}
    metrics = {
        "logical_cells_used": 0,
        "flip_flops": 0,
        "combinational_cells": 0,
        "lut4_cells": 0,
        "cell_type_counts": {},
        "logical_cells_available": resources.get("logic_cells"),
        "logic_utilization_percent": None,
        "block_ram_primitive": resources.get("block_ram_primitive"),
        "block_ram_blocks_used": 0,
        "block_ram_blocks_available": resources.get("block_ram_blocks"),
        "block_ram_bits_available": resources.get("block_ram_bits"),
        "block_ram_utilization_percent": None,
    }
    try:
        with open(json_path, "r", encoding="utf-8", errors="ignore") as handle:
            data = json.load(handle)
    except Exception:
        return metrics
    type_counts: dict[str, int] = {}
    modules = data.get("modules") if isinstance(data, dict) else {}
    if isinstance(modules, dict):
        for module in modules.values():
            cells = module.get("cells") if isinstance(module, dict) else {}
            if not isinstance(cells, dict):
                continue
            for cell in cells.values():
                cell_type = str((cell or {}).get("type") or "unknown")
                type_counts[cell_type] = type_counts.get(cell_type, 0) + 1
    def is_flip_flop(cell_type: str) -> bool:
        return (
            "DFF" in cell_type
            or cell_type.startswith("SB_DFF")
            or cell_type == "TRELLIS_FF"
            or cell_type.startswith("FD1P3")
        )
    ff_count = sum(
        count for cell_type, count in type_counts.items()
        if is_flip_flop(cell_type)
    )
    lut_count = type_counts.get("SB_LUT4", 0) + type_counts.get("LUT4", 0)
    combo_count = sum(
        count for cell_type, count in type_counts.items()
        if not is_flip_flop(cell_type)
        and cell_type not in {"IB", "OB", "IBUF", "OBUF", "IOBUF", "VHI", "VLO", "VCC", "GND"}
        and not cell_type.startswith("$specify")
    )
    fabric_cell_types = {
        "SB_LUT4",
        "SB_CARRY",
        "SB_DFF",
        "SB_DFFE",
        "SB_DFFR",
        "SB_DFFS",
        "SB_DFFES",
        "SB_DFFER",
        "LUT4",
        "TRELLIS_FF",
        "CCU2",
        "CCU2C",
    }
    fabric_cell_count = sum(
        count for cell_type, count in type_counts.items()
        if cell_type in fabric_cell_types or cell_type.startswith("FD1P3") or cell_type.startswith("WIDEFN")
    )
    total_mapped_cells = sum(type_counts.values())
    block_ram_primitive = str(resources.get("block_ram_primitive") or "")
    block_ram_blocks_used = sum(
        count for cell_type, count in type_counts.items()
        if block_ram_primitive and cell_type.upper().endswith(block_ram_primitive.upper())
    )
    # Yosys reports mapped primitives before packing. Keep logic-cell estimate
    # FPGA-oriented instead of counting internal/specify helper cells.
    logical_used = lut_count + ff_count
    available = metrics["logical_cells_available"]
    metrics.update({
        "logical_cells_used": logical_used,
        "flip_flops": ff_count,
        "combinational_cells": combo_count,
        "lut4_cells": lut_count,
        "carry_cells": type_counts.get("SB_CARRY", 0) + type_counts.get("CCU2", 0) + type_counts.get("CCU2C", 0),
        "fabric_mapped_cells": fabric_cell_count,
        "total_mapped_cells": total_mapped_cells,
        "cell_type_counts": type_counts,
        "block_ram_blocks_used": block_ram_blocks_used,
    })
    if available:
        metrics["logic_utilization_percent"] = round((logical_used / float(available)) * 100.0, 3)
    block_ram_available = resources.get("block_ram_blocks")
    if block_ram_available:
        metrics["block_ram_utilization_percent"] = round(
            (block_ram_blocks_used / float(block_ram_available)) * 100.0, 3
        )
    return metrics


def _constant_range_size(range_text: str) -> int | None:
    match = re.fullmatch(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", range_text.strip())
    if not match:
        return None
    return abs(int(match.group(1)) - int(match.group(2))) + 1


def _rtl_memory_intent(rtl_files: list[str], threshold_bits: int = 4096) -> dict:
    """Find substantial constant-size unpacked RTL arrays that should use native RAM."""
    declarations = []
    pattern = re.compile(
        r"\b(?:reg|logic)\s*(\[[^\]]+\])?\s*([A-Za-z_][A-Za-z0-9_$]*)\s*(\[[^\]]+\])\s*;"
    )
    for path in rtl_files:
        try:
            source = open(path, "r", encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        source = re.sub(r"/\*.*?\*/|//[^\r\n]*", "", source, flags=re.DOTALL)
        for match in pattern.finditer(source):
            width = _constant_range_size(match.group(1) or "[0:0]")
            depth = _constant_range_size(match.group(3))
            bits = width * depth if width is not None and depth is not None else None
            declarations.append({
                "file": os.path.abspath(path),
                "name": match.group(2),
                "width": width,
                "depth": depth,
                "bits": bits,
                "requires_block_ram": bits is not None and bits >= threshold_bits,
            })
    return {
        "threshold_bits": threshold_bits,
        "declarations": declarations,
        "requires_block_ram": any(item["requires_block_ram"] for item in declarations),
        "estimated_bits": sum(item["bits"] or 0 for item in declarations),
    }


def _source_memory_optimized_away(netlist: str, memory_intent: dict, metrics: dict) -> bool:
    """Prove a declared array is absent rather than silently mapped to FFs."""
    declarations = memory_intent.get("declarations") if isinstance(memory_intent.get("declarations"), list) else []
    names = [str(item.get("name") or "") for item in declarations if isinstance(item, dict) and item.get("name")]
    if not names or not os.path.exists(netlist):
        return False
    try:
        with open(netlist, "r", encoding="utf-8") as handle:
            serialized = json.dumps(json.load(handle))
    except (OSError, ValueError, TypeError):
        return False
    if any(name in serialized for name in names):
        return False
    # A register implementation needs at least one FF per retained bit. This
    # prevents an expensive renamed FF array from being classified as removed.
    declared_bits = int(memory_intent.get("estimated_bits") or 0)
    realized_ffs = int(metrics.get("flip_flops") or 0)
    return declared_bits > 0 and realized_ffs < declared_bits


@lru_cache(maxsize=4)
def _yosys_help(synth_cmd: str) -> str:
    try:
        result = subprocess.run(
            ["yosys", "-Q", "-p", f"help {synth_cmd}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return f"{result.stdout or ''}\n{result.stderr or ''}"
    except Exception:
        return ""


@lru_cache(maxsize=1)
def _yosys_version() -> str | None:
    try:
        result = subprocess.run(["yosys", "-V"], capture_output=True, text=True, timeout=15, check=False)
        return (result.stdout or result.stderr or "").strip() or None
    except Exception:
        return None


def _yosys_effort_policy(state: dict, synth_cmd: str, help_text: str | None = None) -> dict:
    mode = str(state.get("fpga_closure_mode") or "balanced").strip().lower()
    mode = mode if mode in {"balanced", "advanced"} else "balanced"
    available = help_text if help_text is not None else _yosys_help(synth_cmd)
    options: list[str] = []
    requested = ["technology_mapping"]
    strategy = "baseline"
    if state.get("fpga_yosys_retime"):
        strategy = "retime"
        requested.extend(["disable_abc9", "retime"])
        # Current synth_ice40 enables ABC9 by default, while retiming is
        # explicitly incompatible with ABC9. Keep this as a separate strategy.
        if "-noabc9" in available and "-retime" in available:
            options.extend(["-noabc9", "-retime"])
    elif state.get("fpga_yosys_flatten"):
        strategy = "flatten"
        requested.append("flatten")
        if "-flatten" in available:
            options.append("-flatten")
    else:
        requested.append("preserve_hierarchy")
        if "-noflatten" in available:
            options.append("-noflatten")
    return {
        "mode": mode,
        "goal": "high_timing_effort" if mode == "advanced" else "balanced_timing",
        "strategy": strategy,
        "requested": requested,
        "effective_options": options,
        "capability_checked": True,
        "tool_version": _yosys_version() if help_text is None else None,
        "flatten": bool(state.get("fpga_yosys_flatten")),
        "retime": bool(state.get("fpga_yosys_retime")),
    }


def run_agent(state: dict) -> dict:
    agent = "FPGA Yosys Synthesis Agent"
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    out_dir = fpga_dir(state, "synth")
    board = board_config(state)
    rtl_files = fpga.get("rtl_files") or []
    top = fpga.get("top_module") or state.get("top_module")
    family = str(board.get("family") or "ice40").lower()
    synth_cmd = {
        "ecp5": "synth_ecp5",
        "nexus": "synth_nexus",
        "gowin": "synth_gowin",
    }.get(family, "synth_ice40")
    json_path = os.path.abspath(f"{out_dir}/{top or 'top'}_{family}.json")
    verilog_netlist_path = os.path.abspath(f"{out_dir}/{top or 'top'}_{family}_netlist.v")
    equivalence_netlist_path = os.path.abspath(f"{out_dir}/{top or 'top'}_generic_equivalence_netlist.v")
    mapped_equivalence_netlist_path = os.path.abspath(f"{out_dir}/{top or 'top'}_{family}_mapped_equivalence_netlist.v")
    script_path = os.path.abspath(f"{out_dir}/synth_{family}.ys")
    log_path = os.path.abspath(f"{out_dir}/yosys_synth.log")
    help_text = _yosys_help(synth_cmd)
    effort_policy = _yosys_effort_policy(state, synth_cmd, help_text)
    summary = {
        "agent": agent,
        "status": "blocked",
        "top_module": top,
        "rtl_file_count": len(rtl_files),
        "json_netlist": json_path,
        "verilog_netlist": verilog_netlist_path,
        "equivalence_netlist": equivalence_netlist_path,
        "mapped_equivalence_netlist": mapped_equivalence_netlist_path,
        "closure_iteration": int(state.get("fpga_synthesis_closure_iteration_index") or 0),
        "flatten_enabled": bool(state.get("fpga_yosys_flatten")),
        "tool_effort": effort_policy,
    }
    memory_intent = _rtl_memory_intent(
        [str(path) for path in rtl_files],
        max(1, int(state.get("fpga_block_memory_threshold_bits") or 4096)),
    )
    summary["memory_intent"] = memory_intent
    if not board.get("supported", True):
        summary["error"] = board.get("unsupported_reason") or "Selected FPGA target is unavailable."
        publish_json(state, agent, "synth", "fpga_synthesis_summary.json", summary)
        state["status"] = summary["error"]
        return state

    if not rtl_files or not top:
        summary["error"] = "Missing RTL files or top module from FPGA handoff ingest."
        publish_json(state, agent, "synth", "fpga_synthesis_summary.json", summary)
        state["status"] = summary["error"]
        return state
    steps = [f"read_verilog -sv {path}" for path in rtl_files]
    steps.extend([
        f"hierarchy -check -top {top}",
        "design -save lec_source",
        "proc; opt; memory; opt_clean",
        f"write_verilog -noattr {equivalence_netlist_path}",
        "design -reset",
        "design -load lec_source",
    ])
    if state.get("fpga_yosys_flatten"):
        steps.append("hierarchy -check")
        steps.append("flatten")
    synth_options = " ".join(_architecture_synth_options(board, help_text) + effort_policy["effective_options"])
    steps.append(f"{synth_cmd} -top {top} {synth_options} -json {json_path}".replace("  ", " "))
    # Keep attributes in the formal checkpoint. Some FPGA families encode
    # power-up and technology semantics in attributes which -noattr removes.
    steps.append(f"write_verilog {mapped_equivalence_netlist_path}")
    steps.append(f"write_verilog -noattr {verilog_netlist_path}")
    script = "\n".join(steps) + "\n"
    write_text(script_path, script)
    result = run_cmd(["yosys", "-s", script_path], cwd=out_dir, log_path=log_path, timeout=600, state=state)
    summary.update({"status": "completed" if result["ok"] and os.path.exists(json_path) else "failed", "command": result})
    if os.path.exists(json_path):
        summary.update(_yosys_cell_metrics(json_path, board))
    native_ram_required = bool(memory_intent.get("requires_block_ram"))
    native_ram_supported = bool(((board.get("resources") or {}).get("block_ram_primitive")))
    native_ram_mapped = int(summary.get("block_ram_blocks_used") or 0) > 0
    memory_optimized_away = bool(
        summary["status"] == "completed" and native_ram_required and not native_ram_mapped
        and _source_memory_optimized_away(json_path, memory_intent, summary)
    )
    native_ram_gate_enforced = native_ram_required and native_ram_supported and not memory_optimized_away
    summary["memory_mapping_gate"] = {
        "status": "not_applicable_optimized_away" if memory_optimized_away else "pass" if not native_ram_gate_enforced or native_ram_mapped else "fail",
        "enforced": native_ram_gate_enforced,
        "required": native_ram_required,
        "supported": native_ram_supported,
        "mapped": native_ram_mapped,
        "source_memory_optimized_away": memory_optimized_away,
        "primitive": summary.get("block_ram_primitive"),
    }
    if summary["status"] == "completed" and summary["memory_mapping_gate"]["status"] == "fail":
        summary["status"] = "failed"
        summary["error"] = (
            "RTL contains substantial memory arrays, but synthesis did not map them to the "
            f"selected board's native {summary.get('block_ram_primitive') or 'block RAM'} primitive."
        )
    if not os.path.exists(json_path):
        summary["error"] = "Yosys did not produce the FPGA JSON netlist."
    publish_json(state, agent, "synth", "fpga_synthesis_summary.json", summary)
    manifest_update(state, "synthesis", summary)
    manifest_update(state, "yosys_json", json_path if os.path.exists(json_path) else None)
    manifest_update(state, "yosys_verilog_netlist", verilog_netlist_path if os.path.exists(verilog_netlist_path) else None)
    manifest_update(state, "yosys_equivalence_netlist", equivalence_netlist_path if os.path.exists(equivalence_netlist_path) else None)
    manifest_update(state, "yosys_mapped_equivalence_netlist", mapped_equivalence_netlist_path if os.path.exists(mapped_equivalence_netlist_path) else None)
    if summary["status"] == "failed":
        state["status"] = "FPGA synthesis failed."
    return state
