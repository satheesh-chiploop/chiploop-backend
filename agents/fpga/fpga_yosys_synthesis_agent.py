import json
import os
from .fpga_common import board_config, fpga_dir, manifest_update, publish_json, run_cmd, write_json, write_text


def _yosys_cell_metrics(json_path: str, board: dict) -> dict:
    metrics = {
        "logical_cells_used": 0,
        "flip_flops": 0,
        "combinational_cells": 0,
        "lut4_cells": 0,
        "cell_type_counts": {},
        "logical_cells_available": (board.get("resources") or {}).get("logic_cells"),
        "logic_utilization_percent": None,
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
    ff_count = sum(count for cell_type, count in type_counts.items() if "DFF" in cell_type or cell_type.startswith("SB_DFF"))
    lut_count = type_counts.get("SB_LUT4", 0)
    combo_count = sum(
        count for cell_type, count in type_counts.items()
        if cell_type not in {"SB_DFF", "SB_DFFE", "SB_DFFR", "SB_DFFS", "SB_DFFES", "SB_DFFER"} and "DFF" not in cell_type
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
    }
    fabric_cell_count = sum(count for cell_type, count in type_counts.items() if cell_type in fabric_cell_types)
    total_mapped_cells = sum(type_counts.values())
    # Yosys reports mapped primitives before packing. Keep logic-cell estimate
    # FPGA-oriented instead of counting internal/specify helper cells.
    logical_used = lut_count + ff_count
    available = metrics["logical_cells_available"]
    metrics.update({
        "logical_cells_used": logical_used,
        "flip_flops": ff_count,
        "combinational_cells": combo_count,
        "lut4_cells": lut_count,
        "carry_cells": type_counts.get("SB_CARRY", 0),
        "fabric_mapped_cells": fabric_cell_count,
        "total_mapped_cells": total_mapped_cells,
        "cell_type_counts": type_counts,
    })
    if available:
        metrics["logic_utilization_percent"] = round((logical_used / float(available)) * 100.0, 3)
    return metrics


def run_agent(state: dict) -> dict:
    agent = "FPGA Yosys Synthesis Agent"
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    out_dir = fpga_dir(state, "synth")
    board = board_config(state)
    rtl_files = fpga.get("rtl_files") or []
    top = fpga.get("top_module") or state.get("top_module")
    json_path = os.path.abspath(f"{out_dir}/{top or 'top'}_ice40.json")
    script_path = os.path.abspath(f"{out_dir}/synth_ice40.ys")
    log_path = os.path.abspath(f"{out_dir}/yosys_synth.log")
    summary = {
        "agent": agent,
        "status": "blocked",
        "top_module": top,
        "rtl_file_count": len(rtl_files),
        "json_netlist": json_path,
        "closure_iteration": int(state.get("fpga_synthesis_closure_iteration_index") or 0),
        "flatten_enabled": bool(state.get("fpga_yosys_flatten")),
    }
    if not rtl_files or not top:
        summary["error"] = "Missing RTL files or top module from FPGA handoff ingest."
        publish_json(state, agent, "synth", "fpga_synthesis_summary.json", summary)
        state["status"] = summary["error"]
        return state
    steps = [f"read_verilog -sv {path}" for path in rtl_files]
    if state.get("fpga_yosys_flatten"):
        steps.append("hierarchy -check")
        steps.append("flatten")
    steps.append(f"synth_ice40 -top {top} -json {json_path}")
    script = "\n".join(steps) + "\n"
    write_text(script_path, script)
    result = run_cmd(["yosys", "-s", script_path], cwd=out_dir, log_path=log_path, timeout=600)
    summary.update({"status": "completed" if result["ok"] and os.path.exists(json_path) else "failed", "command": result})
    if os.path.exists(json_path):
        summary.update(_yosys_cell_metrics(json_path, board))
    if not os.path.exists(json_path):
        summary["error"] = "Yosys did not produce the FPGA JSON netlist."
    publish_json(state, agent, "synth", "fpga_synthesis_summary.json", summary)
    manifest_update(state, "synthesis", summary)
    manifest_update(state, "yosys_json", json_path if os.path.exists(json_path) else None)
    if summary["status"] == "failed":
        state["status"] = "FPGA synthesis failed."
    return state
