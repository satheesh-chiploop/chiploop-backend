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
def _gen_cocotb_test(spec: Dict[str, Any], top: str, clocks: List[str], resets: List[Dict[str, Any]]) -> str:
    ports = _ports_from_spec(spec)
    clk = clocks[0] if clocks else None
    rst = resets[0]["name"] if resets else None

    init_lines: List[str] = []
    for p in ports:
        name = p.get("name")
        direction = str(p.get("direction", "")).lower()
        if not name or direction not in ("input", "inout"):
            continue
        if name in (clk, rst):
            continue
        init_lines.append(f"    dut.{name}.value = 0")

    rst_seq = ""
    if rst:
        active_low = resets[0].get("active_low", False)
        assert_val = "0" if active_low else "1"
        deassert_val = "1" if active_low else "0"
        rst_seq = (
            f"    # Reset sequence (derived from digital_spec_json)\n"
            f"    dut.{rst}.value = {assert_val}\n"
            f"    await Timer(5, units=\"ns\")\n"
            f"    dut.{rst}.value = {deassert_val}\n"
            f"    await Timer(5, units=\"ns\")\n"
        )

    if clk:
        clk_seq = f"    cocotb.start_soon(Clock(dut.{clk}, 10, units=\"ns\").start())\n"
    else:
        clk_seq = "    # No clock detected in spec; add one if DUT requires it.\n"

    init_block = "\n".join(init_lines) if init_lines else "    # No extra input ports found to init."

    port_names = [p.get("name") for p in ports if p.get("name")]

    return f'''"""Auto-generated Cocotb testbench skeleton for: {top}

Generated by ChipLoop Testbench Generator Agent.
- Uses Digital Spec JSON to detect ports/clocks/resets (no hardcoding)
- Provides a directed smoke test + a constrained-random test stub
- Includes hooks for scoreboard + coverage (generated by other agents)
"""

import os
import random
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

# Optional integration hooks (created by other agents)
try:
    from tb.scoreboard import Scoreboard
except Exception:
    Scoreboard = None

try:
    from tb.coverage_model import CoverageModel
except Exception:
    CoverageModel = None


def _seed() -> int:
    env = os.getenv("RANDOM_SEED")
    if env and env.isdigit():
        return int(env)
    return 1


@cocotb.test()
async def smoke_test(dut):
    """Basic sanity: clock/reset + one simple stimulus cycle."""
    seed = _seed()
    random.seed(seed)

{clk_seq.rstrip()}
{rst_seq.rstrip()}

    # Initialize DUT inputs (except clk/rst)
{init_block}

    await Timer(20, units="ns")

    sb = Scoreboard(dut) if Scoreboard else None
    cov = CoverageModel() if CoverageModel else None
    if sb:
        sb.start()
    if cov:
        cov.start(dut)

    if {bool(clk)}:
        for _ in range(5):
            await RisingEdge(dut.{clk})
    else:
        await Timer(50, units="ns")

    if cov:
        cov.stop()
        cov.write_reports()
    if sb:
        sb.stop()


@cocotb.test()
async def constrained_random_sanity(dut):
    """Constrained-random stub (extend constraints using spec fields)."""
    seed = _seed()
    random.seed(seed)

{clk_seq.rstrip()}
{rst_seq.rstrip()}

    ports = {json.dumps(port_names, indent=2)}
    ignore = set([{json.dumps(clk) if clk else "None"}, {json.dumps(rst) if rst else "None"}])

    for _ in range(int(os.getenv("NUM_ITERS", "25"))):
        for p in ports:
            if p in ignore:
                continue
            try:
                sig = getattr(dut, p)
                width = len(sig.value.binstr)
                sig.value = random.getrandbits(max(width, 1))
            except Exception:
                continue
        if {bool(clk)}:
            await RisingEdge(dut.{clk})
        else:
            await Timer(10, units="ns")
'''

def run_agent(state: dict) -> dict:
    agent_name = "Testbench Generator Agent"
    print("\n🧪 Running Testbench Generator Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    _ensure_dirs(workflow_id, workflow_dir)

    log_path = os.path.join("artifact", "testbench_generator_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Testbench Generator Agent Log\n")

    spec_path = state.get("spec_json") or os.path.join(workflow_dir, "digital", "spec.json")
    spec = _safe_read_json(spec_path)
    rtl_files = state.get("rtl_files") or _collect_rtl_files(workflow_dir)
    top = _pick_top_module(spec, rtl_files, state.get("top_module"))

    ports = _ports_from_spec(spec)
    clocks, resets = _infer_clocks_resets(spec, ports)

    _log(log_path, f"spec_path={spec_path} ports={len(ports)} clocks={clocks} resets={[r['name'] for r in resets]}")
    _log(log_path, f"top_module={top} rtl_files={len(rtl_files)}")

    tb_root = os.path.join(workflow_dir, "vv", "tb")
    os.makedirs(tb_root, exist_ok=True)

    test_py = _gen_cocotb_test(spec, top, clocks, resets)
    _write_file(os.path.join(tb_root, f"test_{top}.py"), test_py)

    makefile = f"""# Auto-generated by ChipLoop Testbench Generator Agent
TOPLEVEL_LANG ?= verilog
TOPLEVEL      ?= {top}
MODULE        ?= test_{top}

VERILOG_SOURCES += $(shell find ../.. -name '*.v' -o -name '*.sv')

SIM ?= verilator
EXTRA_ARGS += --trace --trace-structs

include $(shell cocotb-config --makefiles)/Makefile.sim
"""
    _write_file(os.path.join(tb_root, "Makefile"), makefile)

    readme = f"""# ChipLoop V&V: Cocotb + Verilator

Generated:
- `test_{top}.py` : cocotb tests (smoke + constrained-random stub)
- `Makefile`      : cocotb Makefile targeting Verilator

Run (from this folder):
```bash
make
RANDOM_SEED=123 make
NUM_ITERS=200 RANDOM_SEED=7 make TESTCASE=constrained_random_sanity
```
"""
    _write_file(os.path.join(tb_root, "README.md"), readme)

    artifacts: Dict[str, Any] = {}
    artifacts["tb_test_py"] = _record_text(workflow_id, agent_name, "vv/tb", f"test_{top}.py", test_py)
    artifacts["tb_makefile"] = _record_text(workflow_id, agent_name, "vv/tb", "Makefile", makefile)
    artifacts["tb_readme"] = _record_text(workflow_id, agent_name, "vv/tb", "README.md", readme)
    artifacts["log"] = _record_text(workflow_id, agent_name, "vv", "testbench_generator_agent.log", open(log_path, "r", encoding="utf-8").read())

    report = {
        "type": "vv_testbench_generation",
        "version": "1.0",
        "top_module": top,
        "spec_path": spec_path,
        "rtl_file_count": len(rtl_files),
        "clocks": clocks,
        "resets": resets,
        "generated_dir": "vv/tb",
        "tools_detected": {"verilator": bool(_which("verilator"))},
        "artifacts": artifacts,
    }

    rep_txt = json.dumps(report, indent=2)
    _write_file(os.path.join(tb_root, "tb_generation_report.json"), rep_txt)
    artifacts["report"] = _record_text(workflow_id, agent_name, "vv/tb", "tb_generation_report.json", rep_txt)

    state.setdefault("vv", {})
    state["vv"]["testbench"] = report
    return state
