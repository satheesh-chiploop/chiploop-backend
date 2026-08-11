import os
import re

from .fpga_common import board_config, fpga_dir, manifest_update, publish_json, run_cmd, write_text

AGENT_NAME = "FPGA RTL-to-Netlist Equivalence Agent"


def _progress(state: dict, message: str) -> None:
    callback = state.get("_progress_callback")
    if callable(callback):
        try:
            callback(message)
        except Exception:
            pass


def _library_reads(family: str) -> list[str]:
    return {
        "ice40": ["read_verilog -sv +/ice40/cells_sim.v"],
        "ecp5": ["read_verilog -sv +/ecp5/cells_sim.v", "read_verilog -sv +/ecp5/cells_bb.v"],
        "nexus": ["read_verilog -sv +/nexus/cells_sim.v", "read_verilog -sv +/nexus/cells_xtra.v"],
        "gowin": ["read_verilog -sv +/gowin/cells_sim.v", "read_verilog -sv +/gowin/cells_xtra.v"],
    }.get(family, [])


def _induction_depths(depth: int) -> list[int]:
    # Synthesis LEC proves a transformation of the same sequential machine; it
    # is not an unbounded functional-property proof. One bounded induction
    # depth avoids repeating an increasingly expensive proof over large FPGA
    # register banks.
    return [max(1, min(depth, 32))]


def _proof_script(rtl_files: list[str], netlist: str, top: str, family: str, depths: list[int]) -> str:
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
        # FPGA netlists often encode power-up state in technology primitive
        # attributes while the source uses Verilog ``initial`` assignments.
        # Treat unknown state bits consistently during sequential proof, as
        # the ASIC LEC flow already does, instead of reporting false
        # non-equivalence solely from representation-specific X semantics.
        "equiv_simple -undef -seq 20",
    ])
    lines.extend(f"equiv_induct -undef -seq {depth}" for depth in depths)
    lines.append("equiv_status -assert")
    return "\n".join(lines) + "\n"


def _unproven_points(log: str, proven: bool) -> int | None:
    if proven:
        return 0
    matches = re.findall(r"(\d+) unproven \$equiv cells", log, re.IGNORECASE)
    return int(matches[-1]) if matches else None


def _run_proof(state: dict, out_dir: str, name: str, gold_files: list[str], gate_netlist: str,
               top: str, family: str, depths: list[int]) -> dict:
    script_path = os.path.abspath(os.path.join(out_dir, f"{name}.ys"))
    log_path = os.path.abspath(os.path.join(out_dir, f"{name}.log"))
    write_text(script_path, _proof_script(gold_files, gate_netlist, top, family, depths))
    timeout_seconds = max(30, min(int(state.get("fpga_lec_timeout_seconds") or 180), 600))
    result = run_cmd(["yosys", "-s", script_path], cwd=out_dir, log_path=log_path, timeout=timeout_seconds, state=state)
    log = open(log_path, "r", encoding="utf-8", errors="ignore").read() if os.path.exists(log_path) else ""
    proven = bool(result.get("ok"))
    unproven = _unproven_points(log, proven)
    status = "pass" if proven else "inconclusive" if unproven else "fail"
    proof = {
        "status": status, "proven": proven, "gold": gold_files,
        "gate": gate_netlist, "script": script_path, "log": log_path,
        "command": result, "unproven_points": unproven, "timeout_seconds": timeout_seconds,
    }
    if not proven:
        proof["failure_kind"] = "proof_incomplete" if unproven else "tool_error"
        proof["reason"] = (
            f"Yosys could not prove {unproven} equivalence points after induction depths "
            f"{', '.join(str(value) for value in depths)}."
            if unproven else
            result.get("stderr_tail") or result.get("stdout_tail") or result.get("error") or "Yosys equivalence proof failed."
        )
    return proof


def run_agent(state: dict) -> dict:
    fpga = state.get("fpga") if isinstance(state.get("fpga"), dict) else {}
    enabled = bool(state.get("run_fpga_lec", True))
    required = bool(state.get("require_fpga_lec", True))
    top = str(fpga.get("top_module") or state.get("top_module") or "")
    rtl_files = [str(path) for path in fpga.get("rtl_files") or [] if os.path.exists(str(path))]
    synthesis = fpga.get("synthesis") if isinstance(fpga.get("synthesis"), dict) else {}
    generic_netlist = str(synthesis.get("equivalence_netlist") or fpga.get("yosys_equivalence_netlist") or "")
    mapped_netlist = str(
        synthesis.get("mapped_equivalence_netlist")
        or fpga.get("yosys_mapped_equivalence_netlist")
        or synthesis.get("verilog_netlist")
        or fpga.get("yosys_verilog_netlist")
        or ""
    )
    family = str(board_config(state).get("family") or "ice40").lower()
    depth = max(1, min(int(state.get("fpga_lec_induct_depth") or 12), 128))
    induction_depths = _induction_depths(depth)
    out_dir = fpga_dir(state, "lec")
    summary = {
        "agent": AGENT_NAME, "status": "disabled" if not enabled else "blocked",
        "enabled": enabled, "required": required, "tool": "Yosys",
        "comparison": "two_stage_rtl_generic_and_generic_mapped_equivalence", "top_module": top,
        "family": family, "rtl_file_count": len(rtl_files), "netlist": mapped_netlist or None,
        "generic_netlist": generic_netlist or None, "mapped_netlist": mapped_netlist or None,
        "induction_depth": depth, "induction_depths_attempted": induction_depths,
        "unproven_points": None,
    }
    if not enabled:
        summary["reason"] = "FPGA LEC disabled by user."
    elif (synthesis.get("status") != "completed" or not top or not rtl_files
          or not os.path.exists(generic_netlist) or not os.path.exists(mapped_netlist)):
        summary["reason"] = "LEC requires completed synthesis, source RTL, and both generic and FPGA-mapped netlists."
    else:
        _progress(state, f"FPGA LEC proof 1/2 started: RTL to generic synthesis netlist (timeout {max(30, min(int(state.get('fpga_lec_timeout_seconds') or 180), 600))}s).")
        generic_proof = _run_proof(state, out_dir, "fpga_rtl_to_generic_lec", rtl_files, generic_netlist, top, "", induction_depths)
        _progress(state, f"FPGA LEC proof 1/2 finished with status {generic_proof['status']}.")
        # A failed RTL-to-generic proof already blocks the chain. Do not spend
        # another full timeout proving a mapped netlist whose golden source has
        # not been established.
        if generic_proof["proven"]:
            _progress(state, f"FPGA LEC proof 2/2 started: generic to {family} mapped netlist.")
            mapped_proof = _run_proof(state, out_dir, "fpga_generic_to_mapped_lec", [generic_netlist], mapped_netlist, top, family, induction_depths)
            _progress(state, f"FPGA LEC proof 2/2 finished with status {mapped_proof['status']}.")
        else:
            mapped_proof = {
                "status": "blocked", "proven": False, "gold": [generic_netlist],
                "gate": mapped_netlist, "unproven_points": None,
                "failure_kind": "upstream_proof_failed",
                "reason": "Mapped-netlist LEC was not started because RTL-to-generic LEC did not pass.",
            }
            _progress(state, "FPGA LEC proof 2/2 skipped because proof 1 did not pass.")
        proven = bool(generic_proof["proven"] and mapped_proof["proven"])
        failed_proof = generic_proof if generic_proof["status"] != "pass" else mapped_proof
        unproven_points = sum(int(proof.get("unproven_points") or 0) for proof in (generic_proof, mapped_proof)) or None
        summary.update({
            "status": "pass" if proven else "inconclusive" if unproven_points else "fail",
            "generic_lec": generic_proof, "mapped_lec": mapped_proof,
            "generic_proven": generic_proof["proven"], "mapped_proven": mapped_proof["proven"],
            "unproven_points": unproven_points,
            "proven": proven,
        })
        if not proven:
            summary.update(failure_kind=failed_proof.get("failure_kind"), reason=failed_proof.get("reason"))
    publish_json(state, AGENT_NAME, "lec", "fpga_lec_summary.json", summary)
    manifest_update(state, "lec", summary)
    state["fpga_lec"] = summary
    if required and enabled and summary["status"] != "pass":
        raise RuntimeError(f"FPGA RTL-to-netlist LEC did not pass: {summary.get('reason') or summary['status']}")
    return state
