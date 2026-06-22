"""
ChipLoop Verification & Validation Agents (Cocotb + Verilator + SymbiYosys)

Design goals:
- No hardcoding of clocks/resets/ports: derive from `digital_spec_json` (Digital Spec Agent output)
- Be regression-friendly: Verilator + Cocotb, reproducible seeds
- Keep artifacts small & useful: generate drop-in files + a concise JSON report
- Fail gracefully if tools are missing (still generate configs/templates)

State contract (expected keys, but all optional):
- workflow_id: str
- workflow_dir: str (default: backend/workflows/<workflow_id>)
- spec_json: str (path to Digital Spec Agent JSON; default: <workflow_dir>/digital/spec.json)
- rtl_files: list[str] optional; otherwise we scan workflow_dir for .v/.sv
- top_module: str optional; otherwise from spec.top_module.name or inferred
- dut_lang: "verilog"|"systemverilog" optional
"""

import os
import re
import json
import shutil
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record
from tooling.runner import run_command, tool_path


def _now() -> str:
    return datetime.now().isoformat()


def _log(path: str, msg: str) -> None:
    print(msg)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{_now()}] {msg}\n")


def _safe_read_json(path: str) -> Dict[str, Any]:
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _ensure_dirs(workflow_id: str, workflow_dir: str) -> Tuple[str, str]:
    os.makedirs(workflow_dir, exist_ok=True)
    os.makedirs("artifact", exist_ok=True)
    return workflow_id, workflow_dir


def _collect_rtl_files(workflow_dir: str) -> List[str]:
    exts = (".v", ".sv", ".vh", ".svh")
    rtl: List[str] = []
    for root, _, files in os.walk(workflow_dir):
        for fn in files:
            if fn.lower().endswith(exts):
                rtl.append(os.path.join(root, fn))
    rtl.sort()
    return rtl


def _pick_top_module(spec: Dict[str, Any], rtl_files: List[str], state_top: Optional[str]) -> str:
    if state_top:
        return state_top
    top = (spec.get("top_module") or {}).get("name")
    if isinstance(top, str) and top.strip():
        return top.strip()
    hierarchy = spec.get("hierarchy") if isinstance(spec.get("hierarchy"), dict) else {}
    htop = hierarchy.get("top_module")
    if isinstance(htop, dict):
        name = htop.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    elif isinstance(htop, str) and htop.strip():
        return htop.strip()
    mod_re = re.compile(r"^\s*module\s+([a-zA-Z_][a-zA-Z0-9_$]*)\b")
    for f in rtl_files:
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    m = mod_re.match(line)
                    if m:
                        return m.group(1)
        except Exception:
            continue
    return "top"


def _ports_from_spec(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    top = spec.get("top_module")
    if isinstance(top, dict) and isinstance(top.get("ports"), list):
        return [p for p in top["ports"] if isinstance(p, dict)]
    hierarchy = spec.get("hierarchy") if isinstance(spec.get("hierarchy"), dict) else {}
    htop = hierarchy.get("top_module")
    if isinstance(htop, dict) and isinstance(htop.get("ports"), list):
        return [p for p in htop["ports"] if isinstance(p, dict)]
    ports = spec.get("ports")
    if isinstance(ports, list):
        return [p for p in ports if isinstance(p, dict)]
    return []


def _infer_clocks_resets(spec: Dict[str, Any], ports: List[Dict[str, Any]]) -> Tuple[List[str], List[Dict[str, Any]]]:
    clocks: List[str] = []
    resets: List[Dict[str, Any]] = []

    clk_spec = spec.get("clocks") or (spec.get("clocking") or {}).get("clocks")
    if isinstance(clk_spec, list):
        for c in clk_spec:
            if isinstance(c, dict) and c.get("name"):
                clocks.append(c["name"])
            elif isinstance(c, str):
                clocks.append(c)

    rst_spec = spec.get("resets") or (spec.get("reset") or {})
    if isinstance(rst_spec, list):
        for r in rst_spec:
            if isinstance(r, dict) and r.get("name"):
                resets.append(r)
            elif isinstance(r, str):
                resets.append({"name": r})
    elif isinstance(rst_spec, dict):
        if rst_spec.get("name"):
            resets.append(rst_spec)

    if not clocks:
        for p in ports:
            nm = str(p.get("name", ""))
            if re.search(r"(?:^|_)(clk|clock)(?:$|_)", nm, re.IGNORECASE):
                clocks.append(nm)

    if not resets:
        for p in ports:
            nm = str(p.get("name", ""))
            if re.search(r"(?:^|_)(rst|reset)(?:$|_)", nm, re.IGNORECASE):
                resets.append({"name": nm})

    clocks = [c for c in clocks if isinstance(c, str) and c.strip()]
    clocks = list(dict.fromkeys(clocks))

    norm_resets: List[Dict[str, Any]] = []
    for r in resets:
        if isinstance(r, dict) and r.get("name"):
            name = str(r.get("name"))
            rr = {
                "name": name,
                "active_low": bool(
                    r.get("active_low", False)
                    or re.search(r"(?:^|_)(rst|reset|por)_n$", name, re.IGNORECASE)
                    or str(r.get("polarity", "")).lower() in ("active_low", "low", "0")
                ),
                "async": bool(
                    str(r.get("type", "")).lower() in ("async", "asynchronous")
                    or r.get("async", False)
                ),
            }
            norm_resets.append(rr)

    seen = set()
    uniq: List[Dict[str, Any]] = []
    for rr in norm_resets:
        if rr["name"] not in seen:
            seen.add(rr["name"])
            uniq.append(rr)

    return clocks, uniq


def _which(binname: str) -> Optional[str]:
    return tool_path(binname)


def _write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _record_text(workflow_id: str, agent_name: str, subdir: str, filename: str, content: str) -> Optional[str]:
    try:
        return save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir=subdir,
            filename=filename,
            content=content,
        )
    except Exception:
        return None


def _formal_solver_from_state(state: Dict[str, Any]) -> str:
    toolchain = state.get("toolchain") if isinstance(state.get("toolchain"), dict) else {}
    solver = str(toolchain.get("formal_solver") or state.get("formal_solver") or "z3").strip().lower()
    return solver if solver in {"z3", "boolector"} else "z3"


def _gen_sby(
    top: str,
    rtl_files: List[str],
    clk: Optional[str],
    rst: Optional[Dict[str, Any]],
    solver: str = "z3",
    base_dir: Optional[str] = None,
    depth: int = 20,
) -> str:
    rel_files = [
        os.path.relpath(os.path.abspath(f), os.path.abspath(base_dir)) if base_dir else os.path.relpath(f)
        for f in rtl_files[:200]
    ]
    files_block = "\n".join(rel_files)
    # SymbiYosys copies [files] into the generated src/ directory before
    # running Yosys there. The script must read those local copied names, not
    # the original paths relative to the .sby file.
    read_cmds = "\n".join([f"read_verilog -sv {os.path.basename(rf)}" for rf in rel_files])

    clock_comment = f"clock {clk}" if clk else "clock <clk> (no clock detected)"
    reset_comment = ""
    if rst and rst.get("name"):
        reset_comment = f"reset: {rst['name']} active_low={rst.get('active_low', False)} async={rst.get('async', False)}"

    return f"""# Auto-generated SymbiYosys config for {top}

[options]
mode bmc
depth {max(int(depth), 1)}
multiclock on

[engines]
smtbmc {solver}

[script]
{read_cmds}
prep -top {top}

[files]
{files_block}

# {clock_comment}
# {reset_comment}
"""


def _gen_formal_props(top: str, clk: Optional[str], rst: Optional[Dict[str, Any]]) -> str:
    clk_name = clk or "clk"
    rst_decl = ""
    if rst and rst.get("name"):
        rst_decl = f"input logic {rst['name']};"
    return f"""// Auto-generated formal wrapper for {top}
// Minimal stub. Real properties should come from Assertions (SVA) Agent output.

module {top}_formal;
  input logic {clk_name};
  {rst_decl}

  // TODO: instantiate DUT with proper ports or build a dedicated harness.

  always @(posedge {clk_name}) begin
    // TODO: add assertions/properties here.
  end
endmodule
"""


def _is_memory_macro_or_mbist_run(state: Dict[str, Any], rtl_files: List[str]) -> bool:
    handoff = state.get("verification_source_handoff")
    if isinstance(handoff, dict):
        if handoff.get("mbist_integrated_rtl") is True:
            return True
        if handoff.get("mbist_integrated_rtl") is False:
            return False
    names = " ".join(os.path.basename(str(path)).lower() for path in rtl_files)
    if any(token in names for token in ("autombist", "mbist_top", "mbist_fsm", "mbist_algo")):
        return True
    for path in rtl_files[:80]:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read(12000).lower()
            if any(token in text for token in ("autombist", "mbist", "sram", "openram")):
                return True
        except Exception:
            continue
    return False


def _is_integrated_mbist_handoff(state: Dict[str, Any]) -> bool:
    handoff = state.get("verification_source_handoff")
    return isinstance(handoff, dict) and handoff.get("mbist_integrated_rtl") is True


def _formal_depth(state: Dict[str, Any], memory_macro_formal_limited: bool) -> int:
    raw = state.get("formal_depth") or state.get("bmc_depth")
    try:
        depth = int(raw)
    except Exception:
        depth = 12 if memory_macro_formal_limited else 40
    if memory_macro_formal_limited:
        return max(4, min(depth, 16))
    return max(4, min(depth, 80))


def _formal_timeout_sec(state: Dict[str, Any], memory_macro_formal_limited: bool) -> int:
    raw = state.get("formal_timeout_sec")
    try:
        timeout = int(raw)
    except Exception:
        timeout = 180 if memory_macro_formal_limited else 600
    if memory_macro_formal_limited:
        return max(30, min(timeout, 240))
    return max(60, min(timeout, 900))


def run_agent(state: dict) -> dict:
    agent_name = "Formal Verification Agent"
    print("\n🧠 Running Formal Verification Agent (SymbiYosys)...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    _ensure_dirs(workflow_id, workflow_dir)

    log_path = os.path.join("artifact", "formal_verification_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Formal Verification Agent Log\n")

    spec_path = state.get("spec_json") or os.path.join(workflow_dir, "digital", "spec.json")
    spec = _safe_read_json(spec_path)
    rtl_files = state.get("rtl_files") or _collect_rtl_files(workflow_dir)
    top = _pick_top_module(spec, rtl_files, state.get("top_module"))

    ports = _ports_from_spec(spec)
    clocks, resets = _infer_clocks_resets(spec, ports)
    clk = clocks[0] if clocks else None
    rst = resets[0] if resets else None
    toolchain = state.get("toolchain") if isinstance(state.get("toolchain"), dict) else {}
    formal_tool = str(toolchain.get("formal") or state.get("formal_tool") or "symbiyosys").strip().lower()
    formal_solver = _formal_solver_from_state(state)
    memory_macro_formal_limited = _is_memory_macro_or_mbist_run(state, rtl_files)
    integrated_mbist_formal_default = _is_integrated_mbist_handoff(state)
    formal_depth = _formal_depth(state, memory_macro_formal_limited)
    formal_timeout = _formal_timeout_sec(state, memory_macro_formal_limited)

    formal_root = os.path.join(workflow_dir, "vv", "formal")
    os.makedirs(formal_root, exist_ok=True)

    sby_txt = _gen_sby(top, rtl_files, clk, rst, formal_solver, formal_root, depth=formal_depth)
    props_sv = _gen_formal_props(top, clk, rst)

    _write_file(os.path.join(formal_root, f"{top}.sby"), sby_txt)
    _write_file(os.path.join(formal_root, f"{top}_formal.sv"), props_sv)

    sby_bin = _which("sby")
    solver_bin = _which(formal_solver)
    run_result: Dict[str, Any] = {
        "tool": formal_tool,
        "solver": formal_solver,
        "available": bool(sby_bin and solver_bin and formal_tool == "symbiyosys"),
        "attempted": False,
    }
    if formal_tool == "none":
        run_result["disabled"] = True
    elif memory_macro_formal_limited and not integrated_mbist_formal_default and state.get("force_deep_formal") is not True:
        run_result.update(
            {
                "attempted": False,
                "status": "inconclusive",
                "reason": "memory_macro_or_mbist_design",
                "detail": (
                    "Full-top BMC is not run by default for SRAM/MBIST macro designs. "
                    "Use simulation, lint, and dedicated MBIST collateral checks, or enable deep formal explicitly."
                ),
            }
        )
    elif sby_bin and solver_bin:
        run_result["attempted"] = True
        try:
            cmd = ["sby", "-f", f"{top}.sby"]
            p = run_command(state, "formal_verification", cmd, cwd=formal_root, timeout_sec=formal_timeout)
            run_result.update(
                {
                    "returncode": p.returncode,
                    "stdout_tail": (p.stdout or "").splitlines()[-120:],
                    "stderr_tail": (p.stderr or "").splitlines()[-120:],
                    "tool_execution": p.to_dict(),
                }
            )
        except Exception as e:
            run_result.update({"returncode": -1, "error": str(e)})

    artifacts: Dict[str, Any] = {}
    artifacts["sby"] = _record_text(workflow_id, agent_name, "vv/formal", f"{top}.sby", sby_txt)
    artifacts["formal_sv"] = _record_text(workflow_id, agent_name, "vv/formal", f"{top}_formal.sv", props_sv)
    try:
        artifacts["log"] = _record_text(
            workflow_id,
            agent_name,
            "vv",
            "formal_verification_agent.log",
            open(log_path, "r", encoding="utf-8").read(),
        )
    except Exception:
        artifacts["log"] = None

    report = {
        "type": "vv_formal_verification",
        "version": "1.0",
        "top_module": top,
        "spec_path": spec_path,
        "rtl_file_count": len(rtl_files),
        "clock": clk,
        "reset": rst,
        "toolchain": {
            "formal": formal_tool,
            "formal_solver": formal_solver,
        },
        "deep_formal_default": integrated_mbist_formal_default,
        "formal_depth": formal_depth,
        "formal_timeout_sec": formal_timeout,
        "status": run_result.get("status") or (
            "pass" if run_result.get("attempted") and run_result.get("returncode") == 0
            else "fail" if run_result.get("attempted")
            else "disabled" if run_result.get("disabled")
            else "inconclusive" if run_result.get("reason")
            else "generated"
        ),
        "reason": run_result.get("reason"),
        "tools_detected": {
            "sby": bool(sby_bin),
            "yosys": bool(_which("yosys")),
            "z3": bool(_which("z3")),
            "boolector": bool(_which("boolector")),
            formal_solver: bool(solver_bin),
        },
        "run": run_result,
        "artifacts": artifacts,
    }

    rep_txt = json.dumps(report, indent=2)
    _write_file(os.path.join(formal_root, "formal_report.json"), rep_txt)
    artifacts["report"] = _record_text(workflow_id, agent_name, "vv/formal", "formal_report.json", rep_txt)

    state.setdefault("vv", {})
    state["vv"]["formal"] = report
    return state
