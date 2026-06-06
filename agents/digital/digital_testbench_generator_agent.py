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
from tooling.runner import tool_path

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
        os.path.join(workflow_dir, "digital", "handoff", "digital_subsystem_ip_package", "rtl"),
        os.path.join(workflow_dir, "digital", "handoff", "rtl"),
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
    return tool_path(binname)


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


def _build_observable_outputs(ports: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    arr: List[Dict[str, Any]] = []
    for p in ports:
        name = p.get("name")
        direction = _normalize_direction(p.get("direction"))
        if not name or direction != "output":
            continue
        arr.append({"name": name, "width_expr": _port_width_expr(p)})
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
    observable_outputs = _build_observable_outputs(ports)

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


def _assert_outputs_known(dut, observable_outputs):
    for p in observable_outputs:
        name = p.get("name")
        if not name or not hasattr(dut, name):
            continue
        sig = getattr(dut, name)
        value = str(sig.value).lower()
        assert "x" not in value and "z" not in value, f"Output {{name}} is unknown/high-Z: {{sig.value}}"


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

    _assert_outputs_known(dut, {observable_outputs_json})

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

    _assert_outputs_known(dut, {observable_outputs_json})

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
        observable_outputs_json=json.dumps(observable_outputs, indent=2),
    )

def _selected_default_tests(selection: Any) -> List[str]:
    mode = str(selection or "both").strip().lower()
    if mode in {"directed", "directed_only", "smoke"}:
        return ["smoke_test"]
    if mode in {"random", "random_only", "constrained_random"}:
        return ["constrained_random_sanity"]
    return ["smoke_test", "constrained_random_sanity"]


def _build_testcases_manifest(
    top: str,
    clocks: List[str],
    resets: List[Dict[str, Any]],
    mode: str,
    test_selection: Any = "both",
) -> Dict[str, Any]:
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
        "test_selection": str(test_selection or "both"),
        "default_tests": _selected_default_tests(test_selection),
        "tests": tests,
    }


def _to_make_relpath(base_dir: str, abs_path: str) -> str:
    try:
        rel = os.path.relpath(os.path.abspath(abs_path), os.path.abspath(base_dir))
        return rel.replace("\\", "/")
    except Exception:
        return os.path.basename(abs_path).replace("\\", "/")

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
EXTRA_ARGS += --coverage
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


def _plan_port_rows(ports: List[Dict[str, Any]], direction: str) -> List[str]:
    rows: List[str] = []
    for p in ports:
        if _normalize_direction(p.get("direction")) != direction:
            continue
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        rows.append(f"- `{name}` width `{_port_width_expr(p)}`")
    return rows


def _generate_verification_plan(
    spec: Dict[str, Any],
    top: str,
    ports: List[Dict[str, Any]],
    clocks: List[str],
    resets: List[Dict[str, Any]],
    test_intent: str,
) -> str:
    inputs = _plan_port_rows(ports, "input")
    outputs = _plan_port_rows(ports, "output")
    inouts = _plan_port_rows(ports, "inout")
    reset_names = [str(r.get("name")) for r in resets if isinstance(r, dict) and r.get("name")]
    features = spec.get("features") or spec.get("requirements") or spec.get("functional_requirements") or []
    feature_rows: List[str] = []
    if isinstance(features, list):
        for item in features[:12]:
            if isinstance(item, dict):
                text = item.get("name") or item.get("description") or item.get("requirement")
            else:
                text = item
            if text:
                feature_rows.append(f"- {str(text).strip()}")

    lines = [
        "# Verification Plan",
        "",
        f"- Source: generated_from_spec",
        f"- Top module: `{top}`",
        f"- Clocks: {', '.join(f'`{c}`' for c in clocks) if clocks else 'not declared'}",
        f"- Resets: {', '.join(f'`{r}`' for r in reset_names) if reset_names else 'not declared'}",
        "",
        "## User/Test Intent",
        test_intent.strip() or "No explicit test intent was provided. The plan is derived from the resolved RTL specification.",
        "",
        "## Interfaces Under Test",
        "### Inputs",
        *(inputs or ["- No spec-declared inputs found"]),
        "",
        "### Outputs",
        *(outputs or ["- No spec-declared outputs found"]),
        "",
    ]
    if inouts:
        lines.extend(["### Inouts", *inouts, ""])
    if feature_rows:
        lines.extend(["## Functional Requirements", *feature_rows, ""])
    lines.extend(
        [
            "## Planned Tests",
            "- Reset/boot smoke test: drive reset, release reset, confirm stable known behavior.",
            "- Register or control path test: exercise configuration/control inputs and observe outputs.",
            "- Directed scenario tests: cover the key user intent and spec-declared behavior.",
            "- Constrained-random sanity: vary input values around zero, one, max, and non-zero buckets.",
            "- Output stability checks: confirm outputs do not become unknown after reset release.",
            "",
            "## Assertions And Checks",
            "- Reset sequencing checks for declared reset ports.",
            "- Clocked output known-value checks after reset release.",
            "- Interface-specific checks generated from port directions and widths.",
            "",
            "## Closure Criteria",
            "- All generated simulation tests pass.",
            "- Functional coverage points in `coverage_point_plan.md` are reviewed and either hit or waived.",
            "- Code coverage and formal results are reviewed when enabled for this run.",
            "",
        ]
    )
    return "\n".join(lines)


def _generate_coverage_plan(cov_spec: Dict[str, Any]) -> str:
    lines = [
        "# Coverage Point Plan",
        "",
        "- Source: generated_from_spec",
        f"- Top module: `{cov_spec.get('top_module') or 'top'}`",
        "",
        "## Output Coverpoints",
    ]
    outputs = cov_spec.get("output_points") or []
    if outputs:
        for point in outputs:
            lines.append(f"- Cover `{point.get('name')}` zero and non-zero/value-transition bins.")
    else:
        lines.append("- No spec-declared outputs found.")
    lines.extend(["", "## Input Coverpoints"])
    inputs = cov_spec.get("input_points") or []
    if inputs:
        for point in inputs:
            lines.append(f"- Cover `{point.get('name')}` zero and non-zero/input-stimulus bins.")
    else:
        lines.append("- No spec-declared inputs found.")
    user_points = cov_spec.get("user_coverage_plan_points") or []
    if user_points:
        lines.extend(["", "## Uploaded Coverage Plan Points"])
        for point in user_points:
            lines.append(f"- {point.get('name')}")
    lines.extend(
        [
            "",
            "## Cross Coverage Candidates",
            "- Cross reset release with first observed output activity.",
            "- Cross primary control inputs with output response bins when both are present.",
            "",
            "## Closure Guidance",
            "- Review uncovered bins before accepting closure.",
            "- Add directed tests for missed bins, or mark exclusions with reviewer rationale.",
            "",
        ]
    )
    return "\n".join(lines)


def _generate_monitor_checker_plan(
    top: str,
    ports: List[Dict[str, Any]],
    clocks: List[str],
    resets: List[Dict[str, Any]],
    cov_spec: Dict[str, Any],
) -> str:
    reset_names = [str(r.get("name")) for r in resets if isinstance(r, dict) and r.get("name")]
    inputs = [p for p in ports if _normalize_direction(p.get("direction")) in ("input", "inout")]
    outputs = [p for p in ports if _normalize_direction(p.get("direction")) == "output"]
    control_inputs = [
        str(p.get("name"))
        for p in inputs
        if p.get("name") and not str(p.get("name")) in set(clocks + reset_names)
    ][:16]
    observed_outputs = [str(p.get("name")) for p in outputs if p.get("name")][:16]
    output_cov_names = [f"`{p.get('name')}`" for p in cov_spec.get("output_points", []) if p.get("name")]
    input_cov_names = [f"`{p.get('name')}`" for p in cov_spec.get("input_points", []) if p.get("name")]

    lines = [
        "# Monitor And Checker Plan",
        "",
        "- Source: generated_from_spec",
        f"- Top module: `{top}`",
        f"- Clock observations: {', '.join(f'`{c}`' for c in clocks) if clocks else 'not declared'}",
        f"- Reset observations: {', '.join(f'`{r}`' for r in reset_names) if reset_names else 'not declared'}",
        "",
        "## Monitors",
        "- Clock/reset monitor: observes reset assertion/deassertion and first active clock edges.",
        "- Input stimulus monitor: records driven values on declared non-clock/reset inputs.",
        "- Output response monitor: samples declared outputs after reset release and after stimulus changes.",
        "- Coverage monitor: calls `CoverageModel.sample()` at transaction/checkpoint boundaries.",
        "",
        "## Observed Inputs",
        *( [f"- `{name}`" for name in control_inputs] or ["- No spec-declared non-clock/reset inputs found"] ),
        "",
        "## Observed Outputs",
        *( [f"- `{name}`" for name in observed_outputs] or ["- No spec-declared outputs found"] ),
        "",
        "## Checkers",
        "- Reset known-value checker: outputs must not remain unknown after reset release and settle.",
        "- Width/value checker: sampled signals are interpreted using spec-declared widths.",
        "- Scenario checker: directed tests should encode expected responses from the verification plan.",
        "- Scoreboard hook: `Scoreboard` is loaded when `scoreboard.py` is present and can compare expected versus observed transactions.",
        "- SVA hook: generated SVA/bind files are included through `verification_sources.mk` when available.",
        "",
        "## Coverage Coupling",
        f"- Functional output points: {', '.join(output_cov_names) or 'none'}",
        f"- Functional input points: {', '.join(input_cov_names) or 'none'}",
        "",
        "## Review Checklist",
        "- Confirm each important requirement has a monitor point.",
        "- Confirm each monitor feeds a checker, scoreboard, assertion, or coverage point.",
        "- Add directed tests or custom scoreboard logic for behavior that cannot be inferred from ports alone.",
        "",
    ]
    return "\n".join(lines)


def _coverage_points_from_ports(top: str, ports: List[Dict[str, Any]]) -> Dict[str, Any]:
    output_points: List[Dict[str, Any]] = []
    input_points: List[Dict[str, Any]] = []
    for p in ports:
        name = p.get("name")
        if not name:
            continue
        point = {
            "name": str(name),
            "direction": _normalize_direction(p.get("direction")),
            "width_expr": _port_width_expr(p),
        }
        if point["direction"] == "output":
            output_points.append(point)
        elif point["direction"] in ("input", "inout"):
            input_points.append(point)
    return {"top_module": top, "output_points": output_points[:16], "input_points": input_points[:16]}




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
    test_selection = state.get("random_vs_directed") or "both"
    testcase_manifest = _build_testcases_manifest(top, clocks, resets, mode, test_selection)
    closure_testcase_intents = [
        item for item in (state.get("closure_testcase_intents") or [])
        if isinstance(item, dict)
    ]
    if closure_testcase_intents:
        testcase_manifest["closure_testcase_intents"] = closure_testcase_intents
        testcase_manifest["tests"].extend(closure_testcase_intents)
        executable = [
            str(item.get("mapped_executable_test"))
            for item in closure_testcase_intents
            if item.get("mapped_executable_test")
        ]
        if executable:
            testcase_manifest["default_tests"] = list(dict.fromkeys([
                *testcase_manifest["default_tests"],
                *executable,
            ]))
    uploaded_verification_plan = state.get("verification_plan") if isinstance(state.get("verification_plan"), str) else ""
    uploaded_monitor_checker_plan = state.get("monitor_checker_plan") if isinstance(state.get("monitor_checker_plan"), str) else ""
    uploaded_coverage_plan = state.get("coverage_plan") if isinstance(state.get("coverage_plan"), str) else ""
    test_intent = state.get("test_intent") if isinstance(state.get("test_intent"), str) else ""
    verification_plan_source = "uploaded" if uploaded_verification_plan.strip() else "generated_from_spec"
    monitor_checker_plan_source = "uploaded" if uploaded_monitor_checker_plan.strip() else "generated_from_spec"
    coverage_plan_source = "uploaded" if uploaded_coverage_plan.strip() else "generated_from_spec"
    cov_plan_spec = _coverage_points_from_ports(top, ports)
    verification_plan = uploaded_verification_plan.strip() or _generate_verification_plan(
        spec, top, ports, clocks, resets, test_intent
    )
    monitor_checker_plan = uploaded_monitor_checker_plan.strip() or _generate_monitor_checker_plan(
        top, ports, clocks, resets, cov_plan_spec
    )
    coverage_plan = uploaded_coverage_plan.strip() or _generate_coverage_plan(cov_plan_spec)

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
        "verification_plan_path": "vv/tb/verification_plan.md",
        "verification_plan_source": verification_plan_source,
        "monitor_checker_plan_path": "vv/tb/monitor_checker_plan.md",
        "monitor_checker_plan_source": monitor_checker_plan_source,
        "coverage_point_plan_path": "vv/tb/coverage_point_plan.md",
        "coverage_point_plan_source": coverage_plan_source,
        "test_selection": testcase_manifest["test_selection"],
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
- `verification_plan.md`, `monitor_checker_plan.md`, `coverage_point_plan.md` : generated or uploaded review plans

Run (from this folder):
```bash
make
RANDOM_SEED=123 make
NUM_ITERS=200 RANDOM_SEED=7 make TESTCASE=constrained_random_sanity
python run_regression.py --tests smoke_test constrained_random_sanity --seeds 1 2 3 4
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
    _write_file(os.path.join(tb_root, "verification_plan.md"), verification_plan.strip() + "\n")
    _write_file(os.path.join(tb_root, "monitor_checker_plan.md"), monitor_checker_plan.strip() + "\n")
    _write_file(os.path.join(tb_root, "coverage_point_plan.md"), coverage_plan.strip() + "\n")

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
    artifacts["verification_plan"] = _record_text(workflow_id, agent_name, "vv/tb", "verification_plan.md", verification_plan.strip() + "\n")
    artifacts["monitor_checker_plan"] = _record_text(workflow_id, agent_name, "vv/tb", "monitor_checker_plan.md", monitor_checker_plan.strip() + "\n")
    artifacts["coverage_point_plan"] = _record_text(workflow_id, agent_name, "vv/tb", "coverage_point_plan.md", coverage_plan.strip() + "\n")

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
        "closure_testcase_intents": closure_testcase_intents,
        "test_selection": testcase_manifest["test_selection"],
        "uploaded_plans": {
            "verification_plan_present": bool(uploaded_verification_plan.strip()),
            "monitor_checker_plan_present": bool(uploaded_monitor_checker_plan.strip()),
            "coverage_plan_present": bool(uploaded_coverage_plan.strip()),
        },
        "plan_sources": {
            "verification_plan": verification_plan_source,
            "monitor_checker_plan": monitor_checker_plan_source,
            "coverage_point_plan": coverage_plan_source,
        },
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
