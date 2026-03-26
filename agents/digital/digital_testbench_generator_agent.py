"""
ChipLoop Verification & Validation - Digital Testbench Generator Agent

Design goals:
- digital_spec.json / spec_json is the primary source of truth
- no hardcoded DUT signal names
- support both digital-only and future system/SoC runs
- log decisions to both backend logger and persistent artifact log
- generate deterministic, reusable simulation collateral
"""

import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from utils.artifact_utils import save_text_artifact_and_record

python_exe = sys.executable
logger = logging.getLogger("chiploop")


def _now() -> str:
    return datetime.now().isoformat()


def _log(path: str, msg: str, level: str = "info") -> None:
    if level == "error":
        logger.error(msg)
    elif level == "warning":
        logger.warning(msg)
    else:
        logger.info(msg)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{_now()}] [{level.upper()}] {msg}\n")


def _log_kv(path: str, key: str, value: Any, level: str = "info") -> None:
    try:
        rendered = json.dumps(value, indent=2, default=str)
    except Exception:
        rendered = str(value)
    _log(path, f"{key}={rendered}", level=level)


def _safe_read_json(path: Optional[str]) -> Dict[str, Any]:
    try:
        if path and isinstance(path, str) and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
                if isinstance(obj, dict):
                    return obj
    except Exception:
        pass
    return {}


def _ensure_dirs(workflow_id: str, workflow_dir: str) -> Tuple[str, str]:
    os.makedirs(workflow_dir, exist_ok=True)
    os.makedirs("artifact", exist_ok=True)
    return workflow_id, workflow_dir


def _write_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _record_text(
    workflow_id: str,
    agent_name: str,
    subdir: str,
    filename: str,
    content: str,
) -> Optional[str]:
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


def _collect_rtl_files(workflow_dir: str) -> List[str]:
    exts = (".v", ".sv", ".vh", ".svh")

    handoff_dirs = [
        os.path.join(workflow_dir, "handoff", "digital_subsystem_ip_package", "rtl"),
        os.path.join(workflow_dir, "handoff", "rtl"),
    ]

    for d in handoff_dirs:
        if not os.path.isdir(d):
            continue

        rtl: List[str] = []
        for root, _, files in os.walk(d):
            for fn in files:
                if fn.lower().endswith(exts):
                    rtl.append(os.path.abspath(os.path.join(root, fn)))

        rtl = sorted(set(rtl))
        if rtl:
            return rtl

    return []

def _find_fallback_spec_json(workflow_dir: str) -> Optional[str]:
    preferred: List[str] = []
    fallback: List[str] = []

    for root, _, files in os.walk(workflow_dir):
        for fn in files:
            if not fn.endswith(".json"):
                continue
            if not fn.endswith("_spec.json") and "spec" not in fn.lower():
                continue

            path = os.path.join(root, fn)
            norm = path.replace("\\", "/").lower()

            if "/digital/" in norm:
                preferred.append(path)
            elif "/analog/" in norm:
                continue
            else:
                fallback.append(path)

    if preferred:
        preferred.sort()
        return preferred[0]
    if fallback:
        fallback.sort()
        return fallback[0]
    return None


def _pick_top_module(spec: Dict[str, Any], rtl_files: List[str], state_top: Optional[str]) -> str:
    top = (spec.get("top_module") or {}).get("name")
    if isinstance(top, str) and top.strip():
        return top.strip()

    hierarchy = spec.get("hierarchy") or {}
    top2 = hierarchy.get("top_module")
    if isinstance(top2, dict):
        nm = top2.get("name")
        if isinstance(nm, str) and nm.strip():
            return nm.strip()
    elif isinstance(top2, str) and top2.strip():
        return top2.strip()

    if state_top and isinstance(state_top, str) and state_top.strip():
        return state_top.strip()

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
    out: List[Dict[str, Any]] = []

    tm = spec.get("top_module") or {}
    ports = tm.get("ports")
    if isinstance(ports, list) and ports:
        return [dict(p) for p in ports if isinstance(p, dict) and p.get("name")]

    hierarchy = spec.get("hierarchy") or {}
    htop = hierarchy.get("top_module")
    if isinstance(htop, dict):
        ports = htop.get("ports")
        if isinstance(ports, list) and ports:
            return [dict(p) for p in ports if isinstance(p, dict) and p.get("name")]

    ports = spec.get("ports")
    if isinstance(ports, list) and ports:
        return [dict(p) for p in ports if isinstance(p, dict) and p.get("name")]

    io = spec.get("io")
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


def _normalize_direction(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s in ("input", "in", "i"):
        return "input"
    if s in ("output", "out", "o"):
        return "output"
    if s in ("inout", "io"):
        return "inout"
    return s


def _port_width_expr(port: Dict[str, Any]) -> str:
    width = port.get("width")
    msb = port.get("msb")
    lsb = port.get("lsb")
    rng = port.get("range")

    if isinstance(width, int) and width >= 1:
        return str(width)
    if isinstance(width, str) and width.strip():
        return width.strip()
    if msb is not None and lsb is not None:
        return f"(({msb}) - ({lsb}) + 1)"
    if isinstance(rng, str):
        m = re.match(r"\[\s*(.+?)\s*:\s*(.+?)\s*\]", rng.strip())
        if m:
            return f"(({m.group(1)}) - ({m.group(2)}) + 1)"
    return "1"


def _infer_clocks_resets(spec: Dict[str, Any], ports: List[Dict[str, Any]]) -> Tuple[List[str], List[Dict[str, Any]]]:
    clocks: List[str] = []
    resets: List[Dict[str, Any]] = []

    clk_spec = spec.get("clocks") or (spec.get("clocking") or {}).get("clocks")
    if isinstance(clk_spec, list):
        for c in clk_spec:
            if isinstance(c, dict) and c.get("name"):
                clocks.append(str(c["name"]))
            elif isinstance(c, str):
                clocks.append(c)

    rst_spec = spec.get("resets") or (spec.get("reset") or {})
    if isinstance(rst_spec, list):
        for r in rst_spec:
            if isinstance(r, dict) and r.get("name"):
                resets.append(dict(r))
            elif isinstance(r, str):
                resets.append({"name": r})
    elif isinstance(rst_spec, dict) and rst_spec.get("name"):
        resets.append(dict(rst_spec))

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
            nm = str(r.get("name"))
            is_name_active_low = bool(re.search(r"(rst_n|reset_n|por_n)", nm, re.IGNORECASE))
            rr = {
                "name": nm,
                "active_low": bool(
                    r.get("active_low", False)
                    or is_name_active_low
                    or str(r.get("polarity", "")).lower() in ("active_low", "low", "0")
                ),
                "async": bool(
                    str(r.get("type", "")).lower() in ("async", "asynchronous")
                    or r.get("async", False)
                ),
            }
            norm_resets.append(rr)

    

    uniq_resets: List[Dict[str, Any]] = []
    seen = set()
    for rr in norm_resets:
        if rr["name"] not in seen:
            seen.add(rr["name"])
            uniq_resets.append(rr)

    return clocks, uniq_resets


def _which(binname: str) -> Optional[str]:
    return shutil.which(binname)


def _resolve_tb_mode(state: Dict[str, Any]) -> str:
    if (
        state.get("soc_top_sim_module")
        or state.get("soc_top_name")
        or state.get("system_integration_intent_json")
        or state.get("soc_top_sim_path")
    ):
        return "system"
    return "digital"


def _resolve_tb_contract(state: Dict[str, Any], workflow_dir: str, log_path: str) -> Dict[str, Any]:
    mode = _resolve_tb_mode(state)

    spec_path = state.get("spec_json") or state.get("digital_spec_json")
    spec_source = "state"
    if not spec_path:
        spec_path = _find_fallback_spec_json(workflow_dir)
        spec_source = "fallback_scan"

    spec = _safe_read_json(spec_path)

    rtl_files = state.get("rtl_files")
    rtl_source = "state.rtl_files"
    if not isinstance(rtl_files, list) or not rtl_files:
        rtl_files = _collect_rtl_files(workflow_dir)
        rtl_source = "fallback_scan"

    rtl_files = [os.path.abspath(p) for p in rtl_files if isinstance(p, str)]

    if mode == "system":
        top = (
            state.get("soc_top_sim_module")
            or state.get("soc_top_name")
            or _pick_top_module(spec, rtl_files, state.get("top_module"))
        )
    else:
        top = state.get("top_module") or _pick_top_module(spec, rtl_files, state.get("top_module"))

    contract = {
        "mode": mode,
        "spec_path": spec_path,
        "spec_source": spec_source,
        "rtl_files": rtl_files,
        "rtl_source": rtl_source,
        "top_module": top,
        "soc_mode": bool(mode == "system"),
    }

    _log_kv(
        log_path,
        "resolved_contract",
        {
            "mode": contract["mode"],
            "spec_path": contract["spec_path"],
            "spec_source": contract["spec_source"],
            "rtl_source": contract["rtl_source"],
            "rtl_file_count": len(contract["rtl_files"]),
            "top_module": contract["top_module"],
            "soc_mode": contract["soc_mode"],
        },
    )
    return contract


def _render_clock_start(clocks: List[str]) -> str:
    lines: List[str] = []
    for clk in clocks:
        lines.append(
            f"    if hasattr(dut, {json.dumps(clk)}):\n"
            f"        cocotb.start_soon(Clock(getattr(dut, {json.dumps(clk)}), 10, units='ns').start())"
        )
    if not lines:
        return "    # No clock port identified from spec."
    return "\n".join(lines)


def _render_reset_sequence(resets: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    if not resets:
        return "    # No reset port identified from spec."

    for r in resets:
        rst = r["name"]
        active_low = bool(r.get("active_low", False))
        assert_val = "0" if active_low else "1"
        lines.append(
            f"    if hasattr(dut, {json.dumps(rst)}):\n"
            f"        getattr(dut, {json.dumps(rst)}).value = {assert_val}"
        )
    lines.append("    await Timer(5, units='ns')")
    for r in resets:
        rst = r["name"]
        active_low = bool(r.get("active_low", False))
        deassert_val = "1" if active_low else "0"
        lines.append(
            f"    if hasattr(dut, {json.dumps(rst)}):\n"
            f"        getattr(dut, {json.dumps(rst)}).value = {deassert_val}"
        )
    lines.append("    await Timer(5, units='ns')")
    return "\n".join(lines)


def _render_input_init(ports: List[Dict[str, Any]], clocks: List[str], resets: List[Dict[str, Any]]) -> str:
    clk_set = set(clocks)
    rst_set = {r["name"] for r in resets}
    lines: List[str] = []

    for p in ports:
        name = p.get("name")
        direction = _normalize_direction(p.get("direction"))
        if not name or direction not in ("input", "inout"):
            continue
        if name in clk_set or name in rst_set:
            continue

        lines.append(
            f"    if hasattr(dut, {json.dumps(name)}):\n"
            f"        getattr(dut, {json.dumps(name)}).value = 0"
        )

    if not lines:
        return "    # No non-clock/reset input ports discovered from spec."
    return "\n".join(lines)


def _build_randomizable_inputs(
    ports: List[Dict[str, Any]],
    clocks: List[str],
    resets: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    clk_set = set(clocks)
    rst_set = {r["name"] for r in resets}
    arr: List[Dict[str, Any]] = []

    for p in ports:
        name = p.get("name")
        direction = _normalize_direction(p.get("direction"))
        if not name or direction not in ("input", "inout"):
            continue
        if name in clk_set or name in rst_set:
            continue

        arr.append(
            {
                "name": name,
                "width_expr": _port_width_expr(p),
            }
        )
    return arr


def _gen_cocotb_test(
    spec: Dict[str, Any],
    top: str,
    clocks: List[str],
    resets: List[Dict[str, Any]],
    soc_mode: bool = False,
) -> str:
    ports = [] if soc_mode else _ports_from_spec(spec)
    input_init = _render_input_init(ports, clocks, resets)
    clock_start = _render_clock_start(clocks)
    reset_seq = _render_reset_sequence(resets)
    randomizable_inputs = _build_randomizable_inputs(ports, clocks, resets)

    template = '''"""Auto-generated Cocotb testbench skeleton for: {top}

Generated by ChipLoop Testbench Generator Agent.
- Uses spec JSON as the source of truth for ports/clocks/resets
- Supports digital mode and future system/SoC mode
- Avoids hardcoded DUT signal assumptions
"""

import os
import random
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

try:
    from scoreboard import Scoreboard
except Exception:
    Scoreboard = None

try:
    from coverage_model import CoverageModel
except Exception:
    CoverageModel = None


def _seed() -> int:
    env = os.getenv("RANDOM_SEED")
    if env and env.isdigit():
        return int(env)
    return 1


async def _advance_time(dut):
    clocks = {clocks_json}
    for clk_name in clocks:
        if hasattr(dut, clk_name):
            await RisingEdge(getattr(dut, clk_name))
            return
    await Timer(10, units="ns")


def _safe_drive_random(sig, width_expr: str):
    try:
        width = int(width_expr)
    except Exception:
        try:
            width = len(sig.value.binstr)
        except Exception:
            width = 1
    width = max(int(width), 1)
    sig.value = random.getrandbits(width)


@cocotb.test()
async def smoke_test(dut):
    """Basic smoke test: clock/reset bring-up and brief settle window."""
    seed = _seed()
    random.seed(seed)

{clock_start}

{reset_seq}

    # Initialize discoverable DUT inputs from spec
{input_init}

    await Timer(20, units="ns")

    sb = Scoreboard(dut) if Scoreboard else None
    cov = CoverageModel() if CoverageModel else None

    if sb:
        sb.start()
    if cov:
        cov.start(dut)

    for _ in range(5):
        await _advance_time(dut)
        if cov:
            try:
                cov.sample()
            except Exception:
                pass

    if cov:
        try:
            cov.stop()
            cov.write_reports()
        except Exception:
            pass

    

    if sb:
        sb.stop()


@cocotb.test()
async def constrained_random_sanity(dut):
    """Constrained-random sanity test derived only from spec-declared inputs."""
    seed = _seed()
    random.seed(seed)

{clock_start}

{reset_seq}

    randomizable_inputs = {randomizable_inputs_json}

    cov = CoverageModel() if CoverageModel else None
    if cov:
        cov.start(dut)

    for _ in range(int(os.getenv("NUM_ITERS", "25"))):
        for p in randomizable_inputs:
            name = p.get("name")
            width_expr = str(p.get("width_expr", "1"))
            if not name or not hasattr(dut, name):
                continue
            try:
                sig = getattr(dut, name)
                _safe_drive_random(sig, width_expr)
            except Exception:
                continue

        await _advance_time(dut)

        if cov:
            try:
                cov.sample()
            except Exception:
                pass

    if cov:
        try:
            cov.stop()
            cov.write_reports()
        except Exception:
            pass

    
'''
    return template.format(
        top=top,
        clocks_json=json.dumps(clocks, indent=2),
        clock_start=clock_start.rstrip(),
        reset_seq=reset_seq.rstrip(),
        input_init=input_init,
        randomizable_inputs_json=json.dumps(randomizable_inputs, indent=2),
    )

def _build_testcases_manifest(top: str, clocks: List[str], resets: List[Dict[str, Any]], mode: str) -> Dict[str, Any]:
    clock_names = list(clocks)
    reset_names = [r["name"] for r in resets]

    tests = [
        {
            "name": "smoke_test",
            "description": "Basic smoke test with clock/reset bring-up and settle window.",
            "kind": "smoke",
            "top_module": top,
            "mode": mode,
            "clock_names": clock_names,
            "reset_names": reset_names,
            "tags": ["smoke", "sanity"],
            "timeout_ns": 200,
        },
        {
            "name": "constrained_random_sanity",
            "description": "Simple constrained-random sanity test using only spec-declared inputs.",
            "kind": "cr_sanity",
            "top_module": top,
            "mode": mode,
            "clock_names": clock_names,
            "reset_names": reset_names,
            "tags": ["sanity", "random"],
            "timeout_ns": 1000,
        },
    ]

    return {
        "type": "vv_testcases",
        "version": "1.0",
        "top_module": top,
        "mode": mode,
        "default_tests": [t["name"] for t in tests],
        "tests": tests,
    }


def _to_make_relpath(base_dir: str, abs_path: str) -> str:
    try:
        rel = os.path.relpath(abs_path, base_dir)
        return rel.replace("\\", "/")
    except Exception:
        return abs_path.replace("\\", "/")

def _gen_rtl_sources_mk(tb_root: str, rtl_files: List[str]) -> str:
    lines = [
        "# Auto-generated by ChipLoop Testbench Generator Agent",
        "# Explicit RTL sources used by simulation",
    ]
    for p in rtl_files:
        lines.append(f"VERILOG_SOURCES += {_to_make_relpath(tb_root, p)}")
    lines.append("")
    return "\n".join(lines)


def _gen_verification_sources_mk(tb_root: str, verification_files: List[str]) -> str:
    lines = [
        "# Auto-generated by ChipLoop Testbench Generator Agent",
        "# Verification collateral used by simulation",
    ]
    for p in verification_files:
        if p:
            lines.append(f"VERILOG_SOURCES += {_to_make_relpath(tb_root, p)}")
    lines.append("")
    return "\n".join(lines)

def _gen_makefile(top: str) -> str:
    return f"""# Auto-generated by ChipLoop Testbench Generator Agent
override TOPLEVEL_LANG := verilog
override TOPLEVEL      := {top}
override MODULE        := test_{top}

unexport PYTHON_BIN
unexport PYTHON
override export PYTHON_BIN := {python_exe}
override export PYTHON     := {python_exe}
override SIM := verilator
EXTRA_ARGS += --trace --trace-structs
EXTRA_ARGS += -Wno-fatal
EXTRA_ARGS += -Wno-CASEINCOMPLETE
EXTRA_ARGS += -Wno-WIDTH
EXTRA_ARGS += -Wno-UNOPTFLAT

include rtl_sources.mk
-include verification_sources.mk

override COCOTB_MAKEFILES := $(shell $(PYTHON_BIN) -m cocotb_tools.config --makefiles 2>/dev/null)

ifeq ($(strip $(COCOTB_MAKEFILES)),)
$(error $(PYTHON_BIN) -m cocotb_tools.config --makefiles failed. Ensure cocotb is installed in this exact Python environment)
endif

include $(COCOTB_MAKEFILES)/Makefile.sim
"""




def run_agent(state: dict) -> dict:
    agent_name = "Digital Testbench Generator Agent"

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    _ensure_dirs(workflow_id, workflow_dir)

    log_path = os.path.join("artifact", "testbench_generator_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Digital Testbench Generator Agent Log\n")

    _log(log_path, f"Starting {agent_name}...")

    contract = _resolve_tb_contract(state, workflow_dir, log_path)
    mode = contract["mode"]
    spec_path = contract["spec_path"]
    rtl_files = contract["rtl_files"]
    top = contract["top_module"]
    soc_mode = contract["soc_mode"]

    spec = _safe_read_json(spec_path)

    ports = [] if soc_mode else _ports_from_spec(spec)
    clocks, resets = _infer_clocks_resets(spec, ports)

    _log(log_path, f"resolved_mode={mode}")
    _log(log_path, f"spec_path={spec_path}")
    _log(log_path, f"top_module={top}")
    _log(log_path, f"rtl_file_count={len(rtl_files)}")
    _log_kv(log_path, "clock_candidates", clocks)
    _log_kv(log_path, "reset_candidates", resets)

    tb_root = os.path.join(workflow_dir, "vv", "tb")
    os.makedirs(tb_root, exist_ok=True)

    test_py = _gen_cocotb_test(spec, top, clocks, resets, soc_mode=soc_mode)
    testcase_manifest = _build_testcases_manifest(top, clocks, resets, mode)

    verification_files = [
        p for p in [
            state.get("sva_assertions_path"),
            state.get("sva_bind_path"),
        ]
        if p and os.path.exists(p)
    ]

    tb_contract = {
        "type": "vv_tb_contract",
        "version": "1.1",
        "mode": mode,
        "top_module": top,
        "spec_path": spec_path,
        "spec_source": contract["spec_source"],
        "rtl_files": rtl_files,
        "rtl_source": contract["rtl_source"],
        "verification_files": verification_files,
        "clock_names": clocks,
        "reset_names": [r["name"] for r in resets],
        "soc_mode": soc_mode,
        "generated_testbench": f"vv/tb/test_{top}.py",
        "generated_makefile": "vv/tb/Makefile",
        "generated_rtl_sources_mk": "vv/tb/rtl_sources.mk",
        "generated_verification_sources_mk": "vv/tb/verification_sources.mk",
        "generated_testcases_manifest": "vv/tb/testcases.json",
    }

    artifacts: Dict[str, Any] = {}

    makefile = _gen_makefile(top)
    rtl_sources_mk = _gen_rtl_sources_mk(tb_root, rtl_files)
    verification_sources_mk = _gen_verification_sources_mk(tb_root, verification_files)
    _write_file(os.path.join(tb_root, "verification_sources.mk"), verification_sources_mk)
    artifacts["tb_verification_sources_mk"] = _record_text(
        workflow_id, agent_name, "vv/tb", "verification_sources.mk", verification_sources_mk
    )
    _log_kv(log_path, "verification_files", verification_files)
    state["tb_verification_sources_mk"] = os.path.join(tb_root, "verification_sources.mk")
    state["verification_files"] = verification_files

    readme = f"""# ChipLoop V&V: Cocotb + Verilator

Generated:
- `test_{top}.py`     : cocotb tests
- `Makefile`          : cocotb/verilator make entry
- `rtl_sources.mk`    : explicit RTL file list
- `testcases.json`    : testcase manifest
- `tb_contract.json`  : resolved contract used for testbench generation

Run (from this folder):
```bash
make
RANDOM_SEED=123 make
NUM_ITERS=200 RANDOM_SEED=7 make TESTCASE=constrained_random_sanity
```
"""

    testcase_manifest_txt = json.dumps(testcase_manifest, indent=2)
    tb_contract_txt = json.dumps(tb_contract, indent=2)

    _write_file(os.path.join(tb_root, f"test_{top}.py"), test_py)
    _write_file(os.path.join(tb_root, "Makefile"), makefile)
    _write_file(os.path.join(tb_root, "rtl_sources.mk"), rtl_sources_mk)
    _write_file(os.path.join(tb_root, "README.md"), readme)
    _write_file(os.path.join(tb_root, "testcases.json"), testcase_manifest_txt)
    _write_file(os.path.join(tb_root, "tb_contract.json"), tb_contract_txt)

    _log(log_path, f"Generated testbench: vv/tb/test_{top}.py")
    _log(log_path, "Generated Makefile")
    _log(log_path, "Generated rtl_sources.mk")
    _log(log_path, "Generated testcases.json")
    _log(log_path, "Generated tb_contract.json")
    _log_kv(log_path, "generated_testcases", testcase_manifest["default_tests"])


    artifacts["tb_test_py"] = _record_text(workflow_id, agent_name, "vv/tb", f"test_{top}.py", test_py)
    artifacts["tb_makefile"] = _record_text(workflow_id, agent_name, "vv/tb", "Makefile", makefile)
    artifacts["tb_rtl_sources_mk"] = _record_text(workflow_id, agent_name, "vv/tb", "rtl_sources.mk", rtl_sources_mk)
    artifacts["tb_readme"] = _record_text(workflow_id, agent_name, "vv/tb", "README.md", readme)
    artifacts["testcases_manifest"] = _record_text(workflow_id, agent_name, "vv/tb", "testcases.json", testcase_manifest_txt)
    artifacts["tb_contract"] = _record_text(workflow_id, agent_name, "vv/tb", "tb_contract.json", tb_contract_txt)

    report = {
        "type": "vv_testbench_generation",
        "version": "1.1",
        "mode": mode,
        "top_module": top,
        "spec_path": spec_path,
        "rtl_file_count": len(rtl_files),
        "clocks": clocks,
        "resets": resets,
        "generated_dir": "vv/tb",
        "default_tests": testcase_manifest["default_tests"],
        "tools_detected": {
            "verilator": bool(_which("verilator")),
            "make": bool(_which("make")),
            "cocotb_config": bool(_which("cocotb-config")),
        },
        "artifacts": artifacts,
    }

    rep_txt = json.dumps(report, indent=2)
    _write_file(os.path.join(tb_root, "tb_generation_report.json"), rep_txt)
    artifacts["report"] = _record_text(workflow_id, agent_name, "vv/tb", "tb_generation_report.json", rep_txt)

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            log_text = f.read()
    except Exception:
        log_text = ""
    artifacts["log"] = _record_text(workflow_id, agent_name, "vv", "testbench_generator_agent.log", log_text)

    state.setdefault("vv", {})
    state["vv"]["testbench"] = report
    state["vv"]["tb_contract"] = tb_contract
    state["vv"]["testcases_manifest"] = os.path.join(tb_root, "testcases.json")

    state["vv_testcases"] = testcase_manifest["default_tests"]
    state["testcases"] = testcase_manifest["default_tests"]

    state["tb_contract_json"] = os.path.join(tb_root, "tb_contract.json")
    state["tb_testcases_json"] = os.path.join(tb_root, "testcases.json")
    state["tb_makefile"] = os.path.join(tb_root, "Makefile")
    state["tb_test_py"] = os.path.join(tb_root, f"test_{top}.py")
    state["top_module"] = top

    _log(log_path, f"{agent_name} completed successfully.")
    return state

