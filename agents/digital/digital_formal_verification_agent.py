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
    tm = spec.get("top_module") or {}
    ports = tm.get("ports")
    if isinstance(ports, list) and ports:
        return ports
    ports = spec.get("ports")
    if isinstance(ports, list):
        return ports
    io = spec.get("io")
    out: List[Dict[str, Any]] = []
    if isinstance(io, dict):
        for dkey, direction in [("inputs", "input"), ("outputs", "output"), ("inouts", "inout")]:
            arr = io.get(dkey)
            if isinstance(arr, list):
                for p in arr:
                    if isinstance(p, dict) and p.get("name"):
                        q = dict(p)
                        q.setdefault("direction", direction)
                        out.append(q)
    return out

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
            if re.search(r"\b(clk|clock)\b", nm, re.IGNORECASE):
                clocks.append(nm)

    if not resets:
        for p in ports:
            nm = str(p.get("name", ""))
            if re.search(r"\b(rst|reset)\b", nm, re.IGNORECASE):
                resets.append({"name": nm})

    clocks = [c for c in clocks if isinstance(c, str) and c.strip()]
    clocks = list(dict.fromkeys(clocks))

    norm_resets: List[Dict[str, Any]] = []
    for r in resets:
        if isinstance(r, dict) and r.get("name"):
            rr = {
                "name": r.get("name"),
                "active_low": bool(r.get("active_low", False) or str(r.get("polarity", "")).lower() in ("active_low", "low", "0")),
                "async": bool(str(r.get("type", "")).lower() in ("async", "asynchronous") or r.get("async", False)),
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
    return shutil.which(binname)

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
def _gen_sby(top: str, rtl_files: List[str], clk: Optional[str], rst: Optional[Dict[str, Any]]) -> str:
        rel_files = [os.path.relpath(f) for f in rtl_files[:200]]
        files_block = "\n".join(rel_files)
        read_cmds = "\n".join([f"read_verilog -sv {rf}" for rf in rel_files])

        clock_comment = f"clock {clk}" if clk else "clock <clk> (no clock detected)"
        reset_comment = ""
        if rst and rst.get("name"):
            reset_comment = f"reset: {rst['name']} active_low={rst.get('active_low', False)} async={rst.get('async', False)}"

        return f"""# Auto-generated SymbiYosys config for {top}

    [options]
    mode bmc
    depth 40
    multiclock on

    [engines]
    smtbmc z3

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

        formal_root = os.path.join(workflow_dir, "vv", "formal")
        os.makedirs(formal_root, exist_ok=True)

        sby_txt = _gen_sby(top, rtl_files, clk, rst)
        props_sv = _gen_formal_props(top, clk, rst)

        _write_file(os.path.join(formal_root, f"{top}.sby"), sby_txt)
        _write_file(os.path.join(formal_root, f"{top}_formal.sv"), props_sv)

        sby_bin = _which("sby")
        run_result: Dict[str, Any] = {"available": bool(sby_bin), "attempted": False}
        if sby_bin:
            run_result["attempted"] = True
            try:
                cmd = ["sby", "-f", f"{top}.sby"]
                p = subprocess.run(cmd, cwd=formal_root, capture_output=True, text=True, timeout=600)
                run_result.update({
                    "returncode": p.returncode,
                    "stdout_tail": (p.stdout or "").splitlines()[-120:],
                    "stderr_tail": (p.stderr or "").splitlines()[-120:],
                })
            except Exception as e:
                run_result.update({"returncode": -1, "error": str(e)})

        artifacts: Dict[str, Any] = {}
        artifacts["sby"] = _record_text(workflow_id, agent_name, "vv/formal", f"{top}.sby", sby_txt)
        artifacts["formal_sv"] = _record_text(workflow_id, agent_name, "vv/formal", f"{top}_formal.sv", props_sv)
        artifacts["log"] = _record_text(workflow_id, agent_name, "vv", "formal_verification_agent.log", open(log_path, "r", encoding="utf-8").read())

        report = {
            "type": "vv_formal_verification",
            "version": "1.0",
            "top_module": top,
            "spec_path": spec_path,
            "rtl_file_count": len(rtl_files),
            "clock": clk,
            "reset": rst,
            "tools_detected": {"sby": bool(sby_bin), "yosys": bool(_which("yosys")), "z3": bool(_which("z3"))},
            "run": run_result,
            "artifacts": artifacts,
        }

        rep_txt = json.dumps(report, indent=2)
        _write_file(os.path.join(formal_root, "formal_report.json"), rep_txt)
        artifacts["report"] = _record_text(workflow_id, agent_name, "vv/formal", "formal_report.json", rep_txt)

        state.setdefault("vv", {})
        state["vv"]["formal"] = report
        return state
