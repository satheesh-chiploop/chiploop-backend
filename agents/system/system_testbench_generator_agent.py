"""
ChipLoop Verification & Validation - System Testbench Generator Agent

Design goals:
- system integration intent is the primary source of truth for system assembly intent
- register/control metadata is consumed from upstream digital handoff artifacts when available
- no mode switching; this agent is dedicated to system-level simulation collateral
- avoid hardcoded DUT signal assumptions in test intent
- keep the implementation as close as possible to the stabilized digital testbench generator
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


# -----------------------------------------------------------------------------
# Small utilities
# -----------------------------------------------------------------------------

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


def _safe_read_text(path: Optional[str]) -> str:
    try:
        if path and isinstance(path, str) and os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception:
        pass
    return ""


def _safe_read_lines(path: Optional[str]) -> List[str]:
    try:
        if path and isinstance(path, str) and os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return [ln.strip() for ln in f if ln.strip()]
    except Exception:
        pass
    return []


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

def _which(binname: str) -> Optional[str]:
    return shutil.which(binname)


def _rel_to_workflow(workflow_dir: str, path: Optional[str]) -> Optional[str]:
    if not path or not isinstance(path, str):
        return path
    try:
        return os.path.relpath(path, workflow_dir).replace("\\", "/")
    except Exception:
        return path.replace("\\", "/")


def _rel_list_to_workflow(workflow_dir: str, paths: List[str]) -> List[str]:
    out: List[str] = []
    for p in paths or []:
        if isinstance(p, str) and p.strip():
            out.append(_rel_to_workflow(workflow_dir, p))
    return list(dict.fromkeys(out))


# -----------------------------------------------------------------------------
# Artifact discovery
# -----------------------------------------------------------------------------

def _find_system_integration_json(workflow_dir: str) -> Optional[str]:
    candidates = [
        os.path.join(workflow_dir, "system", "integration", "system_integration_intent.json"),
        os.path.join(workflow_dir, "system_integration_intent.json"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _find_soc_top_sim_path(workflow_dir: str) -> Optional[str]:
    candidates = [
        os.path.join(workflow_dir, "system", "integration", "soc_top_sim.sv"),
        os.path.join(workflow_dir, "soc_top_sim.sv"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _find_system_rtl_filelist(workflow_dir: str) -> Optional[str]:
    candidates = [
        os.path.join(workflow_dir, "system", "integration", "system_rtl_filelist_sim.txt"),
        os.path.join(workflow_dir, "system_rtl_filelist_sim.txt"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _find_regmap_json(workflow_dir: str) -> Optional[str]:
    candidates = [
        os.path.join(workflow_dir, "digital", "digital_regmap.json"),
        os.path.join(workflow_dir, "handoff", "docs", "regmap", "digital_regmap.json"),
        os.path.join(workflow_dir, "handoff", "digital_subsystem_ip_package", "docs", "regmap", "digital_regmap.json"),
        os.path.join(workflow_dir, "digital_regmap.json"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _collect_rtl_files_from_filelist(path: Optional[str]) -> List[str]:
    files: List[str] = []
    for ln in _safe_read_lines(path):
        if not isinstance(ln, str):
            continue
        p = ln.strip()
        if not p:
            continue
        files.append(os.path.abspath(p))
    return list(dict.fromkeys(files))


def _collect_system_rtl_files(workflow_dir: str) -> List[str]:
    filelist = _find_system_rtl_filelist(workflow_dir)
    if filelist:
        listed = _collect_rtl_files_from_filelist(filelist)
        if listed:
            return listed

    exts = (".v", ".sv", ".vh", ".svh")
    scan_dirs = [
        os.path.join(workflow_dir, "system", "integration"),
        os.path.join(workflow_dir, "digital", "rtl_refactored"),
        os.path.join(workflow_dir, "analog"),
    ]
    rtl: List[str] = []
    for d in scan_dirs:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for fn in files:
                if fn.lower().endswith(exts):
                    rtl.append(os.path.abspath(os.path.join(root, fn)))
    return sorted(set(rtl))


# -----------------------------------------------------------------------------
# Structural extraction
# -----------------------------------------------------------------------------

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


def _ports_from_top_sv(top_sv_path: Optional[str], top_module: Optional[str]) -> List[Dict[str, Any]]:
    text = _safe_read_text(top_sv_path)
    if not text:
        return []

    if top_module:
        pat = re.compile(
            rf"module\s+{re.escape(top_module)}\s*\((.*?)\);",
            re.DOTALL | re.IGNORECASE,
        )
    else:
        pat = re.compile(r"module\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\((.*?)\);", re.DOTALL | re.IGNORECASE)

    m = pat.search(text)
    block = ""
    if m:
        block = m.group(1 if top_module else 2)
    if not block:
        return []

    ports: List[Dict[str, Any]] = []
    for raw in block.split(","):
        line = " ".join(raw.replace("\n", " ").split())
        if not line:
            continue
        mm = re.match(
            r"(?P<dir>input|output|inout)\s+(?:wire|logic|reg\s+)?(?:signed\s+)?(?P<range>\[[^\]]+\]\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_$]*)$",
            line,
            re.IGNORECASE,
        )
        if not mm:
            continue
        rng = (mm.group("range") or "").strip()
        p: Dict[str, Any] = {
            "name": mm.group("name"),
            "direction": mm.group("dir").lower(),
        }
        if rng:
            p["range"] = rng
            rm = re.match(r"\[\s*(.+?)\s*:\s*(.+?)\s*\]", rng)
            if rm:
                p["msb"] = rm.group(1)
                p["lsb"] = rm.group(2)
                p["width"] = f"(({rm.group(1)}) - ({rm.group(2)}) + 1)"
        ports.append(p)
    return ports


def _ports_from_integration_json(intent: Dict[str, Any]) -> List[Dict[str, Any]]:
    ports: Dict[str, Dict[str, Any]] = {}
    for c in intent.get("connections", []):
        if not isinstance(c, dict):
            continue
        src = str(c.get("from", "")).strip()
        dst = str(c.get("to", "")).strip()

        if src.startswith("top."):
            name = src.split(".", 1)[1].split("[", 1)[0]
            ports.setdefault(name, {"name": name, "direction": "input", "width": "1"})
        if dst.startswith("top."):
            name = dst.split(".", 1)[1].split("[", 1)[0]
            ports.setdefault(name, {"name": name, "direction": "output", "width": "1"})

    return [ports[k] for k in sorted(ports.keys())]


def _merge_port_shapes(preferred: List[Dict[str, Any]], fallback: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for p in fallback:
        if isinstance(p, dict) and p.get("name"):
            merged[str(p["name"])] = dict(p)
    for p in preferred:
        if isinstance(p, dict) and p.get("name"):
            nm = str(p["name"])
            base = merged.get(nm, {})
            base.update({k: v for k, v in p.items() if v not in (None, "")})
            merged[nm] = base
    return [merged[k] for k in sorted(merged.keys())]


def _infer_clocks_resets(ports: List[Dict[str, Any]]) -> Tuple[List[str], List[Dict[str, Any]]]:
    clocks: List[str] = []
    resets: List[Dict[str, Any]] = []

    for p in ports:
        nm = str(p.get("name", ""))
        if re.search(r"(?:^|_)(clk|clock)(?:$|_)", nm, re.IGNORECASE):
            clocks.append(nm)
        if re.search(r"(?:^|_)(rst|reset)(?:$|_)", nm, re.IGNORECASE):
            active_low = bool(re.search(r"(rst_n|reset_n|por_n)", nm, re.IGNORECASE))
            resets.append({"name": nm, "active_low": active_low, "async": False})

    clocks = list(dict.fromkeys([c for c in clocks if c.strip()]))
    uniq_resets: List[Dict[str, Any]] = []
    seen = set()
    for rr in resets:
        if rr["name"] not in seen:
            seen.add(rr["name"])
            uniq_resets.append(rr)
    return clocks, uniq_resets


def _pick_top_module(intent: Dict[str, Any], state_top: Optional[str], top_sv_path: Optional[str]) -> str:
    top = (((intent.get("top") or {}).get("sim_module")) or state_top or "").strip()
    if top:
        return top

    text = _safe_read_text(top_sv_path)
    m = re.search(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b", text)
    if m:
        return m.group(1)
    return "soc_top_sim"


# -----------------------------------------------------------------------------
# Control metadata (generic, metadata-driven)
# -----------------------------------------------------------------------------

def _parse_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            s = value.strip().lower()
            if s.startswith("0x"):
                return int(s, 16)
            return int(s, 10)
    except Exception:
        pass
    return default


def _bit_mask(lsb: Any, msb: Any) -> int:
    l = _parse_int(lsb, 0)
    m = _parse_int(msb, l)
    if m < l:
        l, m = m, l
    width = (m - l) + 1
    return ((1 << width) - 1) << l


def _build_control_model(regmap: Dict[str, Any], ports: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Keep this metadata-only and protocol-agnostic.
    Do NOT infer APB/I2C/SPI/custom bus behavior in code.
    Downstream LLM prompt can consume this as optional context.
    """
    rm = regmap.get("regmap") if isinstance(regmap, dict) else {}
    regs = (rm or {}).get("registers") or []

    by_name: Dict[str, Dict[str, Any]] = {}
    for r in regs:
        if isinstance(r, dict) and r.get("name"):
            by_name[str(r["name"])] = r

    return {
        "has_regmap": bool(by_name),
        "register_count": len(by_name),
        "register_names": sorted(list(by_name.keys())),
        "top_input_ports": sorted([str(p.get("name")) for p in ports if _normalize_direction(p.get("direction")) in ("input", "inout")]),
        "top_output_ports": sorted([str(p.get("name")) for p in ports if _normalize_direction(p.get("direction")) in ("output", "out", "inout")]),
    }




def _field_mask(reg: Optional[Dict[str, Any]], field_name: str) -> int:
    if not isinstance(reg, dict):
        return 0
    for f in reg.get("fields", []) or []:
        if str(f.get("name", "")).upper() == field_name.upper():
            return _bit_mask(f.get("lsb", 0), f.get("msb", f.get("lsb", 0)))
    return 0


def _system_scenarios_from_regmap(regmap: Dict[str, Any]) -> Dict[str, Any]:
    """
    Keep only lightweight, design-neutral metadata.
    Scenario meaning should come from LLM/system intent, not fixed register names.
    """
    rm = (regmap or {}).get("regmap") or {}
    regs = rm.get("registers") or []

    return {
        "has_regmap": bool(regs),
        "register_count": len(regs),
        "register_names": [str(r.get("name")) for r in regs if isinstance(r, dict) and r.get("name")],
    }

# -----------------------------------------------------------------------------
# Contract resolution
# -----------------------------------------------------------------------------

def _resolve_tb_contract(state: Dict[str, Any], workflow_dir: str, log_path: str) -> Dict[str, Any]:
    integration_json_path = (
        state.get("system_integration_intent_json")
        or _find_system_integration_json(workflow_dir)
    )
    top_sv_path = state.get("soc_top_sim_path") or _find_soc_top_sim_path(workflow_dir)
    rtl_filelist_path = state.get("system_rtl_filelist_sim") or _find_system_rtl_filelist(workflow_dir)
    regmap_json_path = (
        state.get("digital_regmap_json")
        or state.get("regmap_json")
        or _find_regmap_json(workflow_dir)
    )

    integration = _safe_read_json(integration_json_path)
    top_module = _pick_top_module(integration, state.get("top_module"), top_sv_path)

    filelist_value = state.get("system_rtl_filelist_sim")

    rtl_files: List[str] = []
    rtl_source = ""

    # 1) Always prefer top-assembly-generated sim filelist
    if isinstance(filelist_value, list) and filelist_value:
        rtl_files = [os.path.abspath(p) for p in filelist_value if isinstance(p, str) and p.strip()]
        rtl_source = "state.system_rtl_filelist_sim"
    elif isinstance(filelist_value, str) and filelist_value.strip():
        rtl_files = _collect_rtl_files_from_filelist(filelist_value)
        rtl_source = "state.system_rtl_filelist_sim"

    # 2) Then fallback to on-disk filelist text
    if not rtl_files and rtl_filelist_path:
        rtl_files = _collect_rtl_files_from_filelist(rtl_filelist_path)
        rtl_source = "system_rtl_filelist_sim"

    # 3) Only after that consider generic fallback keys
    if not rtl_files:
        legacy_rtl = state.get("system_rtl_files") or state.get("rtl_inputs") or state.get("rtl_files") or []
        if not isinstance(legacy_rtl, list):
            legacy_rtl = [legacy_rtl] if legacy_rtl else []
        rtl_files = [os.path.abspath(p) for p in legacy_rtl if isinstance(p, str) and p.strip()]
        if rtl_files:
            rtl_source = "legacy_state_rtl"

    # 4) Final fallback: scan
    if not rtl_files:
        rtl_files = _collect_system_rtl_files(workflow_dir)
        rtl_source = "fallback_scan"


    ports_from_sv = _ports_from_top_sv(top_sv_path, top_module)
    ports_from_intent = _ports_from_integration_json(integration)
    ports = _merge_port_shapes(ports_from_sv, ports_from_intent)
    clocks, resets = _infer_clocks_resets(ports)

    regmap = _safe_read_json(regmap_json_path)
    control_model = _build_control_model(regmap, ports)

    contract = {
        "integration_json_path": integration_json_path,
        "top_sv_path": top_sv_path,
        "rtl_filelist_path": rtl_filelist_path,
        "regmap_json_path": regmap_json_path,
        "rtl_files": rtl_files,
        "rtl_source": rtl_source,
        "top_module": top_module,
        "ports": ports,
        "clock_names": clocks,
        "resets": resets,
        "control_model": control_model,
    }

    _log_kv(
        log_path,
        "resolved_contract",
        {
            "integration_json_path": integration_json_path,
            "top_sv_path": top_sv_path,
            "rtl_filelist_path": rtl_filelist_path,
            "regmap_json_path": regmap_json_path,
            "rtl_source": rtl_source,
            "rtl_file_count": len(rtl_files),
            "top_module": top_module,
            "port_names": [p.get("name") for p in ports],
            "clock_names": clocks,
            "reset_names": [r.get("name") for r in resets],
            "has_regmap": control_model.get("has_regmap"),
            "register_count": control_model.get("register_count"),
            "register_names": control_model.get("register_names"),
        },
    )
    return contract


# -----------------------------------------------------------------------------
# Codegen helpers
# -----------------------------------------------------------------------------

def _render_clock_start(clocks: List[str]) -> str:
    lines: List[str] = []
    for clk in clocks:
        lines.append(
            f"    if hasattr(dut, {json.dumps(clk)}):\n"
            f"        cocotb.start_soon(Clock(getattr(dut, {json.dumps(clk)}), 10, units='ns').start())"
        )
    if not lines:
        return "    # No clock port identified from integrated top."
    return "\n".join(lines)


def _render_reset_sequence(resets: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    if not resets:
        return "    # No reset port identified from integrated top."

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
        return "    # No non-clock/reset input ports discovered from integrated top."
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
        arr.append({"name": name, "width_expr": _port_width_expr(p)})
    return arr


def _gen_cocotb_test(
    top: str,
    ports: List[Dict[str, Any]],
    clocks: List[str],
    resets: List[Dict[str, Any]],
    control_model: Dict[str, Any],
    scenario_model: Dict[str, Any],
) -> str:
    input_init = _render_input_init(ports, clocks, resets)
    clock_start = _render_clock_start(clocks)
    reset_seq = _render_reset_sequence(resets)
    randomizable_inputs = _build_randomizable_inputs(ports, clocks, resets)

    template = '''"""Auto-generated system-level Cocotb testbench skeleton for: {top}

Generated by ChipLoop System Testbench Generator Agent.
- Uses system integration intent as the source of truth for system structure
- Uses upstream control/register metadata when available
- Keeps test intent generic; no protocol-specific transport is hardcoded in the agent

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


CONTROL_MODEL = {control_model_json}
SCENARIO_MODEL = {scenario_model_json}


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




async def apply_control_sequence(dut):
    """
    Placeholder for system-level stimulus.
    The concrete behavior should be generated from integration intent / spec,
    not hardcoded in the agent.
    """
    await _advance_time(dut)


async def observe_system_response(dut):
    """
    Placeholder for system-level observation.
    The concrete checks should be derived from system intent / assertions / coverage.
    """
    await _advance_time(dut)

@cocotb.test()
async def system_smoke_test(dut):
    """Basic smoke test: integrated top bring-up with optional metadata-driven control sequencing."""
    seed = _seed()
    random.seed(seed)

{clock_start}

{reset_seq}

    # Initialize discoverable DUT inputs from integrated top contract
{input_init}

    await Timer(20, units="ns")

    sb = Scoreboard(dut) if Scoreboard else None
    cov = CoverageModel() if CoverageModel else None
    if sb:
        sb.start()
    if cov:
        cov.start(dut)

    # System-level smoke test should remain generic here.
    # Concrete behavior should come from LLM-derived test intent, not hardcoded register names or transport assumptions.
    await apply_control_sequence(dut)

    for _ in range(20):
        await _advance_time(dut)
        if cov:
            try:
                cov.sample()
            except Exception:
                pass

    await observe_system_response(dut)

    if cov:
        try:
            cov.stop()
            cov.write_reports()
        except Exception:
            pass

    if sb:
        try:
            sb.stop()
        except Exception:
            pass

   

@cocotb.test()
async def integrated_input_sanity(dut):
    """Fallback sanity test derived from integrated top inputs only."""
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
    def _to_python_literal(obj):
        return repr(obj)
    return (
        template
        .replace("{top}", top)
        .replace("{control_model_json}", _to_python_literal(control_model))
        .replace("{scenario_model_json}", _to_python_literal(scenario_model))
        .replace("{clocks_json}", json.dumps(clocks, indent=2))
        .replace("{clock_start}", clock_start.rstrip())
        .replace("{reset_seq}", reset_seq.rstrip())
        .replace("{input_init}", input_init)
        .replace("{randomizable_inputs_json}", json.dumps(randomizable_inputs, indent=2))
    )


def _build_testcases_manifest(top: str, clocks: List[str], resets: List[Dict[str, Any]]) -> Dict[str, Any]:
    clock_names = list(clocks)
    reset_names = [r["name"] for r in resets]

    tests = [
        {
            "name": "system_smoke_test",
            "description": "Integrated system smoke test with metadata-driven control sequencing when available.",
            "kind": "smoke",
            "top_module": top,
            "clock_names": clock_names,
            "reset_names": reset_names,
            "tags": ["system", "smoke", "sanity"],
            "timeout_ns": 2000,
        },
        {
            "name": "integrated_input_sanity",
            "description": "Fallback integrated-top sanity test using only discoverable input ports.",
            "kind": "input_sanity",
            "top_module": top,
            "clock_names": clock_names,
            "reset_names": reset_names,
            "tags": ["system", "sanity", "inputs"],
            "timeout_ns": 1000,
        },
    ]

    return {
        "type": "vv_testcases",
        "version": "1.0",
        "top_module": top,
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
        "# Auto-generated by ChipLoop System Testbench Generator Agent",
        "# Explicit RTL sources used by integrated system simulation",
    ]
    for p in rtl_files:
        lines.append(f"VERILOG_SOURCES += {_to_make_relpath(tb_root, p)}")
    lines.append("")
    return "\n".join(lines)


def _gen_verification_sources_mk(tb_root: str, verification_files: List[str]) -> str:
    lines = [
        "# Auto-generated by ChipLoop System Testbench Generator Agent",
        "# Verification collateral used by integrated system simulation",
    ]
    for p in verification_files:
        if p:
            lines.append(f"VERILOG_SOURCES += {_to_make_relpath(tb_root, p)}")
    lines.append("")
    return "\n".join(lines)


def _gen_makefile(top: str) -> str:
    return f'''# Auto-generated by ChipLoop System Testbench Generator Agent
override TOPLEVEL_LANG := verilog
override TOPLEVEL      := {top}
override MODULE        := test_{top}

unexport PYTHON_BIN
unexport PYTHON
override export PYTHON_BIN := {python_exe}
override export PYTHON     := {python_exe}
override SIM := verilator
EXTRA_ARGS += --trace --trace-structs --coverage --assert
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
'''


# -----------------------------------------------------------------------------
# Agent entrypoint
# -----------------------------------------------------------------------------

def run_agent(state: dict) -> dict:
    agent_name = "System Testbench Generator Agent"

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    _ensure_dirs(workflow_id, workflow_dir)

    log_path = os.path.join("artifact", "system_testbench_generator_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("System Testbench Generator Agent Log\n")

    _log(log_path, f"Starting {agent_name}...")

    contract = _resolve_tb_contract(state, workflow_dir, log_path)
    integration_json_path = contract["integration_json_path"]
    top_sv_path = contract["top_sv_path"]
    regmap_json_path = contract["regmap_json_path"]
    rtl_files = contract["rtl_files"]
    if not rtl_files:
        raise FileNotFoundError("Resolved System_Sim RTL filelist is empty")
    top = contract["top_module"]
    ports = contract["ports"]
    clocks = contract["clock_names"]
    resets = contract["resets"]
    control_model = contract["control_model"]

    regmap = _safe_read_json(regmap_json_path)
    scenario_model = _system_scenarios_from_regmap(regmap)

    _log(log_path, f"integration_json_path={integration_json_path}")
    _log(log_path, f"top_sv_path={top_sv_path}")
    _log(log_path, f"regmap_json_path={regmap_json_path}")
    _log(log_path, f"top_module={top}")
    _log(log_path, f"rtl_file_count={len(rtl_files)}")
    _log_kv(log_path, "ports", ports)
    _log_kv(log_path, "clock_candidates", clocks)
    _log_kv(log_path, "reset_candidates", resets)
    _log_kv(log_path, "control_model", control_model)
    _log_kv(log_path, "scenario_model", scenario_model)

    tb_root = os.path.join(workflow_dir, "vv", "tb")
    os.makedirs(tb_root, exist_ok=True)

    test_py = _gen_cocotb_test(top, ports, clocks, resets, control_model, scenario_model)
    testcase_manifest = _build_testcases_manifest(top, clocks, resets)

    verification_files = [
        p for p in [
            state.get("system_sva_assertions_path"),
            state.get("system_sva_bind_path"),
            state.get("sva_assertions_path"),
            state.get("sva_bind_path"),
        ]
        if p and os.path.exists(p)
    ]


    tb_contract = {
        "type": "vv_tb_contract",
        "version": "1.1",
        "top_module": top,
        "integration_json_path": _rel_to_workflow(workflow_dir, integration_json_path),
        "top_sv_path": _rel_to_workflow(workflow_dir, top_sv_path),
        "regmap_json_path": _rel_to_workflow(workflow_dir, regmap_json_path),
        "rtl_files": _rel_list_to_workflow(workflow_dir, rtl_files),
        "rtl_source": contract["rtl_source"],
        "verification_files": _rel_list_to_workflow(workflow_dir, verification_files),
        "ports": ports,
        "clock_names": clocks,
        "reset_names": [r["name"] for r in resets],
        "control_model": control_model,
        "scenario_model": scenario_model,
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

    readme = f'''# ChipLoop V&V: System Cocotb + Verilator

Generated:
- `test_{top}.py`     : integrated cocotb tests
- `Makefile`          : cocotb/verilator make entry
- `rtl_sources.mk`    : explicit RTL file list
- `testcases.json`    : testcase manifest
- `tb_contract.json`  : resolved contract used for testbench generation

Run (from this folder):
```bash
make
RANDOM_SEED=123 make
NUM_ITERS=200 RANDOM_SEED=7 make TESTCASE=integrated_input_sanity
```
'''

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
    _log_kv(log_path, "resolved_rtl_files", rtl_files)

    artifacts["tb_test_py"] = _record_text(workflow_id, agent_name, "vv/tb", f"test_{top}.py", test_py)
    artifacts["tb_makefile"] = _record_text(workflow_id, agent_name, "vv/tb", "Makefile", makefile)
    artifacts["tb_rtl_sources_mk"] = _record_text(workflow_id, agent_name, "vv/tb", "rtl_sources.mk", rtl_sources_mk)
    artifacts["tb_readme"] = _record_text(workflow_id, agent_name, "vv/tb", "README.md", readme)
    artifacts["testcases_manifest"] = _record_text(workflow_id, agent_name, "vv/tb", "testcases.json", testcase_manifest_txt)
    artifacts["tb_contract"] = _record_text(workflow_id, agent_name, "vv/tb", "tb_contract.json", tb_contract_txt)

    

    report = {
        "type": "vv_system_testbench_generation",
        "version": "1.1",
        "top_module": top,
        "integration_json_path": _rel_to_workflow(workflow_dir, integration_json_path),
        "top_sv_path": _rel_to_workflow(workflow_dir, top_sv_path),
        "regmap_json_path": _rel_to_workflow(workflow_dir, regmap_json_path),
        "rtl_file_count": len(rtl_files),
        "rtl_files": _rel_list_to_workflow(workflow_dir, rtl_files),
        "verification_files": _rel_list_to_workflow(workflow_dir, verification_files),
        "clocks": clocks,
        "resets": resets,
        "control_model": control_model,
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
    artifacts["log"] = _record_text(workflow_id, agent_name, "vv", "system_testbench_generator_agent.log", log_text)

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
