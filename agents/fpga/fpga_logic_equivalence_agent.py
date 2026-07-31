import os
import re

from .fpga_common import board_config, fpga_dir, manifest_update, publish_json, run_cmd, write_text

AGENT_NAME = "FPGA RTL-to-Netlist Equivalence Agent"


def _library_reads(family: str) -> list[str]:
    return {
        "ice40": ["read_verilog -sv +/ice40/cells_sim.v"],
        "ecp5": ["read_verilog -sv +/ecp5/cells_sim.v", "read_verilog -sv +/ecp5/cells_bb.v"],
        "nexus": ["read_verilog -sv +/nexus/cells_sim.v", "read_verilog -sv +/nexus/cells_xtra.v"],
        "gowin": ["read_verilog -sv +/gowin/cells_sim.v", "read_verilog -sv +/gowin/cells_xtra.v"],
    }.get(family, [])


def _proof_script(rtl_files: list[str], netlist: str, top: str, family: str, depth: int) -> str:
    lines = [*(f"read_verilog -sv {path}" for path in rtl_files), f"prep -flatten -top {top}", f"rename {top} gold", "design -stash gold", "design -reset"]
    lines.extend(_library_reads(family))
    lines.extend([
        f"read_verilog -sv {netlist}",
        f"prep -flatten -top {top}",
        f"rename {top} gate",
        "design -stash gate",
        "design -reset",
        "design -copy-from gold gold",
        "design -copy-from gate *",
        "equiv_make gold gate equiv",
        "hierarchy -top equiv",
        "equiv_simple",
        f"equiv_induct -seq {depth}",
        "equiv_status -assert",
    ])
    return "\n".join(lines) + "\n"


def run_agent(state: dict) -> dict:
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    enabled = bool(state.get("run_fpga_lec", True))
    required = bool(state.get("require_fpga_lec", True))
    top = str(fpga.get("top_module") or state.get("top_module") or "")
    rtl_files = [str(path) for path in fpga.get("rtl_files") or [] if os.path.exists(str(path))]
    synthesis = fpga.get("synthesis") if isinstance(fpga.get("synthesis"), dict) else {}
    netlist = str(synthesis.get("verilog_netlist") or fpga.get("yosys_verilog_netlist") or "")
    family = str(board_config(state).get("family") or "ice40").lower()
    depth = max(1, min(int(state.get("fpga_lec_induct_depth") or 12), 128))
    out_dir = fpga_dir(state, "lec")
    script_path = os.path.abspath(os.path.join(out_dir, "fpga_rtl_to_netlist_lec.ys"))
    log_path = os.path.abspath(os.path.join(out_dir, "fpga_rtl_to_netlist_lec.log"))
    summary = {
        "agent": AGENT_NAME, "status": "disabled" if not enabled else "blocked",
        "enabled": enabled, "required": required, "tool": "Yosys",
        "comparison": "approved_rtl_vs_synthesis_netlist", "top_module": top,
        "family": family, "rtl_file_count": len(rtl_files), "netlist": netlist or None,
        "induction_depth": depth, "script": script_path, "log": log_path,
        "unproven_points": None,
    }
    if not enabled:
        summary["reason"] = "FPGA LEC disabled by user."
    elif synthesis.get("status") != "completed" or not top or not rtl_files or not os.path.exists(netlist):
        summary["reason"] = "LEC requires completed FPGA synthesis, source RTL, top module, and structural Verilog netlist."
    else:
        write_text(script_path, _proof_script(rtl_files, netlist, top, family, depth))
        result = run_cmd(["yosys", "-s", script_path], cwd=out_dir, log_path=log_path, timeout=900, state=state)
        log = open(log_path, "r", encoding="utf-8", errors="ignore").read() if os.path.exists(log_path) else ""
        match = re.search(r"(\d+) unproven \$equiv cells", log, re.IGNORECASE)
        summary.update({
            "status": "pass" if result.get("ok") else "fail", "command": result,
            "unproven_points": int(match.group(1)) if match else (0 if result.get("ok") else None),
            "proven": bool(result.get("ok")),
        })
        if not result.get("ok"):
            summary["reason"] = result.get("stderr_tail") or result.get("stdout_tail") or result.get("error") or "Yosys equivalence proof failed."
    publish_json(state, AGENT_NAME, "lec", "fpga_lec_summary.json", summary)
    manifest_update(state, "lec", summary)
    state["fpga_lec"] = summary
    if required and enabled and summary["status"] != "pass":
        raise RuntimeError(f"FPGA RTL-to-netlist LEC did not pass: {summary.get('reason') or summary['status']}")
    return state
