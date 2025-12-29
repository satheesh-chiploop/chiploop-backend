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
def _gen_scoreboard(spec: Dict[str, Any], top: str, clocks: List[str]) -> str:
    ports = _ports_from_spec(spec)
    outputs = [p for p in ports if str(p.get("direction","")).lower() == "output" and p.get("name")]
    inputs  = [p for p in ports if str(p.get("direction","")).lower() in ("input","inout") and p.get("name")]
    clk = clocks[0] if clocks else None

    return f'''"""Auto-generated Scoreboard + Golden Model hooks for {top}"""

import os
import json
from dataclasses import dataclass
from typing import Any, Dict, List
import cocotb
from cocotb.triggers import RisingEdge, Timer

TOP = "{top}"
CLK = {json.dumps(clk)}

@dataclass
class Txn:
    inputs: Dict[str, int]
    outputs: Dict[str, int]
    time_ns: int

def golden_model(txn: Txn) -> Dict[str, int]:
    """Pure-Python reference model placeholder (implement from spec)."""
    return dict(txn.outputs)

class Scoreboard:
    def __init__(self, dut):
        self.dut = dut
        self.mismatches: List[Dict[str, Any]] = []
        self.enabled = False
        self.input_ports = {json.dumps([p["name"] for p in inputs], indent=2)}
        self.output_ports = {json.dumps([p["name"] for p in outputs], indent=2)}

    def start(self):
        self.enabled = True
        cocotb.start_soon(self._run())

    def stop(self):
        self.enabled = False
        self._write_report()

    async def _run(self):
        if CLK:
            while self.enabled:
                await RisingEdge(getattr(self.dut, CLK))
                self.observe()
        else:
            while self.enabled:
                await Timer(10, units="ns")
                self.observe()

    def _snapshot(self) -> Txn:
        inp = {{}}
        out = {{}}
        for n in self.input_ports:
            try: inp[n] = int(getattr(self.dut, n).value)
            except Exception: pass
        for n in self.output_ports:
            try: out[n] = int(getattr(self.dut, n).value)
            except Exception: pass
        return Txn(inputs=inp, outputs=out, time_ns=0)

    def observe(self):
        if not self.enabled:
            return
        txn = self._snapshot()
        exp = golden_model(txn)
        for k, vexp in exp.items():
            vobs = txn.outputs.get(k)
            if vobs is None:
                continue
            if int(vobs) != int(vexp):
                self.mismatches.append({{
                    "signal": k,
                    "expected": int(vexp),
                    "observed": int(vobs),
                    "inputs": txn.inputs,
                }})

    def _write_report(self):
        rep_dir = os.path.join(os.path.dirname(__file__), "reports")
        os.makedirs(rep_dir, exist_ok=True)
        out = {{
            "type": "golden_model_comparison",
            "top_module": TOP,
            "mismatch_count": len(self.mismatches),
            "mismatches": self.mismatches[:200],
        }}
        with open(os.path.join(rep_dir, "scoreboard_report.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
'''

def run_agent(state: dict) -> dict:
    agent_name = "Golden Model Comparison Agent"
    print("\n🧾 Running Golden Model Comparison Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    _ensure_dirs(workflow_id, workflow_dir)

    log_path = os.path.join("artifact", "golden_model_comparison_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Golden Model Comparison Agent Log\n")

    spec_path = state.get("spec_json") or os.path.join(workflow_dir, "digital", "spec.json")
    spec = _safe_read_json(spec_path)
    rtl_files = state.get("rtl_files") or _collect_rtl_files(workflow_dir)
    top = _pick_top_module(spec, rtl_files, state.get("top_module"))

    ports = _ports_from_spec(spec)
    clocks, _ = _infer_clocks_resets(spec, ports)

    tb_root = os.path.join(workflow_dir, "vv", "tb")
    os.makedirs(tb_root, exist_ok=True)

    sb_py = _gen_scoreboard(spec, top, clocks)
    _write_file(os.path.join(tb_root, "scoreboard.py"), sb_py)

    readme = """# Golden Model Comparison

Generated:
- `scoreboard.py` : `golden_model()` stub + `Scoreboard` class

Usage:
- Import `Scoreboard` in cocotb tests.
- Implement `golden_model()` using the functional spec (pure Python).
"""
    _write_file(os.path.join(tb_root, "SCOREBOARD.md"), readme)

    artifacts: Dict[str, Any] = {}
    artifacts["scoreboard_py"] = _record_text(workflow_id, agent_name, "vv/tb", "scoreboard.py", sb_py)
    artifacts["scoreboard_readme"] = _record_text(workflow_id, agent_name, "vv/tb", "SCOREBOARD.md", readme)
    artifacts["log"] = _record_text(workflow_id, agent_name, "vv", "golden_model_comparison_agent.log", open(log_path, "r", encoding="utf-8").read())

    report = {
        "type": "vv_golden_model_generation",
        "version": "1.0",
        "top_module": top,
        "spec_path": spec_path,
        "rtl_file_count": len(rtl_files),
        "clocks": clocks,
        "artifacts": artifacts,
    }

    rep_txt = json.dumps(report, indent=2)
    _write_file(os.path.join(tb_root, "golden_model_generation_report.json"), rep_txt)
    artifacts["report"] = _record_text(workflow_id, agent_name, "vv/tb", "golden_model_generation_report.json", rep_txt)

    state.setdefault("vv", {})
    state["vv"]["golden_model"] = report
    return state
