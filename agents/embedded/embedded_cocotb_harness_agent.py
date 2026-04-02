import json
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

from ._embedded_common import ensure_workflow_dir, write_artifact

logger = logging.getLogger(__name__)

AGENT_NAME = "Embedded Cocotb Harness Agent"
PHASE = "cocotb_harness"
HARNESS_PATH = "firmware/validate/cocotb_harness.py"
MAKEFILE_PATH = "firmware/validate/Makefile"
SMOKE_TEST_PATH = "firmware/validate/test_firmware_smoke.py"
DEBUG_PATH = "firmware/debug/cocotb_harness_debug.json"
RTL_RESOLUTION_DEBUG_PATH = "firmware/debug/cocotb_rtl_resolution.json"


SV_KEYWORDS = {
    "module", "endmodule", "if", "else", "for", "while", "case", "endcase", "always",
    "always_ff", "always_comb", "always_latch", "assign", "generate", "endgenerate",
    "begin", "end", "wire", "logic", "reg", "input", "output", "inout", "parameter",
    "localparam", "typedef", "function", "endfunction", "task", "endtask", "initial",
    "assert", "assume", "cover", "property", "endproperty", "sequence", "endsequence",
}


CLOCK_PATTERNS = [
    r"(?:^|_)(clk|clock)(?:$|_)",
    r"^(pclk|aclk|hclk|fclk|core_clk|sys_clk)$",
]
RESET_PATTERNS = [
    r"^(rst|reset)$",
    r"^(rst_n|reset_n|aresetn|resetn|por_n|resetb|rstb)$",
    r"(?:^|_)(rst|reset)(?:$|_)",
]


def _safe_read(path: str) -> str:
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception as exc:
        logger.warning("%s failed reading %s: %s", AGENT_NAME, path, exc)
    return ""


def _strip_sv_comments(text: str) -> str:
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


def _infer_topmodule_from_sv(sv_text: str, fallback: str = "soc_top_sim") -> str:
    if not sv_text:
        return fallback
    m = re.search(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b", _strip_sv_comments(sv_text))
    return m.group(1) if m else fallback


def _extract_top_ports(sv_text: str) -> List[Dict[str, str]]:
    if not sv_text:
        return []
    text = _strip_sv_comments(sv_text)
    m = re.search(r"\bmodule\s+[A-Za-z_][A-Za-z0-9_$]*\s*(?:#\s*\(.*?\))?\s*\((.*?)\)\s*;", text, flags=re.DOTALL)
    if not m:
        return []

    body = m.group(1)
    ports: List[Dict[str, str]] = []
    for decl in re.finditer(
        r"\b(input|output|inout)\b\s*(?:wire|logic|reg\s*)?(?:signed\s*)?(\[[^\]]+\])?\s*([^;\)]+)",
        body,
        flags=re.DOTALL,
    ):
        direction = decl.group(1)
        width = (decl.group(2) or "").strip()
        names_blob = decl.group(3)
        for raw_name in names_blob.split(","):
            name = raw_name.strip()
            name = re.sub(r"\s*=.*$", "", name).strip()
            name = re.sub(r"\[[^\]]+\]$", "", name).strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
                ports.append({"name": name, "direction": direction, "width": width})
    dedup: List[Dict[str, str]] = []
    seen = set()
    for p in ports:
        if p["name"] not in seen:
            seen.add(p["name"])
            dedup.append(p)
    return dedup


def _looks_like_instance_line(line: str) -> Optional[Tuple[str, str]]:
    line = line.strip()
    if not line or line.startswith(("module ", "endmodule", "if ", "for ", "while ", "case ", "assign ", "always", "initial")):
        return None
    m = re.match(
        r"^([A-Za-z_][A-Za-z0-9_$]*)\s*(?:#\s*\([^;]*?\))?\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\($",
        line,
    )
    if not m:
        return None
    module_name, instance_name = m.group(1), m.group(2)
    if module_name in SV_KEYWORDS or instance_name in SV_KEYWORDS:
        return None
    return module_name, instance_name


def _extract_instantiated_modules_from_top(sv_text: str) -> List[str]:
    mods: List[str] = []
    if not sv_text:
        return mods
    for raw in _strip_sv_comments(sv_text).splitlines():
        hit = _looks_like_instance_line(raw)
        if not hit:
            continue
        module_name, _instance_name = hit
        if module_name not in mods:
            mods.append(module_name)
    return mods


def _extract_defined_modules_from_file(path: str) -> List[str]:
    txt = _strip_sv_comments(_safe_read(path))
    out: List[str] = []
    for m in re.finditer(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b", txt):
        mod = m.group(1)
        if mod not in out:
            out.append(mod)
    return out


def _is_allowed_impl_file(rel_path: str) -> bool:
    rel = rel_path.replace("\\", "/")
    allowed_prefixes = ("digital/rtl_refactored/", "digital/rtl/", "analog/", "system/integration/")
    if not rel.startswith(allowed_prefixes):
        return False
    banned_substrings = [
        "/spec/", "hierarchy_compile", "/firmware/", "/handoff/", "/signoff/", "/validate/",
        "/coverage", "/assert", "/assertions", "/sva", "/tb_",
    ]
    if any(x in rel for x in banned_substrings):
        return False
    return rel.endswith((".sv", ".v")) and not rel.endswith(("_tb.sv", "_tb.v"))


def _index_module_definitions(workflow_dir: str) -> Dict[str, List[str]]:
    module_to_files: Dict[str, List[str]] = {}
    if not workflow_dir or not os.path.isdir(workflow_dir):
        return module_to_files
    for root, _, files in os.walk(workflow_dir):
        for name in sorted(files):
            abs_path = os.path.join(root, name)
            rel_path = os.path.relpath(abs_path, workflow_dir).replace("\\", "/")
            if not _is_allowed_impl_file(rel_path):
                continue
            for mod in _extract_defined_modules_from_file(abs_path):
                module_to_files.setdefault(mod, [])
                if rel_path not in module_to_files[mod]:
                    module_to_files[mod].append(rel_path)
    return module_to_files


def _resolve_required_verilog_sources(workflow_dir: str, soc_top_relpath: str, soc_top_text: str, state: dict) -> List[str]:
    ordered: List[str] = []
    debug = {
        "soc_top_relpath": soc_top_relpath,
        "instantiated_modules": [],
        "resolved_modules": {},
        "missing_modules": [],
        "ambiguous_modules": {},
    }
    if soc_top_relpath:
        ordered.append(soc_top_relpath.replace("\\", "/"))

    required_modules = _extract_instantiated_modules_from_top(soc_top_text)
    debug["instantiated_modules"] = list(required_modules)
    module_to_files = _index_module_definitions(workflow_dir)

    for mod in required_modules:
        candidates = module_to_files.get(mod, [])
        if len(candidates) == 1:
            rel = candidates[0]
            debug["resolved_modules"][mod] = rel
            if rel not in ordered:
                ordered.append(rel)
        elif len(candidates) == 0:
            debug["missing_modules"].append(mod)
        else:
            debug["ambiguous_modules"][mod] = candidates

    write_artifact(state, RTL_RESOLUTION_DEBUG_PATH, json.dumps(debug, indent=2), key=os.path.basename(RTL_RESOLUTION_DEBUG_PATH))

    if debug["missing_modules"]:
        raise RuntimeError("❌ Missing RTL definition files for instantiated modules: " + ", ".join(debug["missing_modules"]))
    if debug["ambiguous_modules"]:
        raise RuntimeError("❌ Ambiguous RTL definition files for instantiated modules: " + ", ".join(sorted(debug["ambiguous_modules"].keys())))
    return ordered


def _resolve_soc_top(state: dict, workflow_dir: str) -> Tuple[str, str]:
    rel = state.get("soc_top_sim_path") or state.get("system_top_sim_path") or "system/integration/soc_top_sim.sv"
    if workflow_dir:
        preferred = os.path.join(workflow_dir, rel)
        if not os.path.isfile(preferred):
            integ_dir = os.path.join(workflow_dir, "system", "integration")
            if os.path.isdir(integ_dir):
                for name in sorted(os.listdir(integ_dir)):
                    if name.endswith("_sim.sv"):
                        rel = f"system/integration/{name}"
                        break
    abs_path = os.path.join(workflow_dir, rel) if workflow_dir else rel
    return rel.replace("\\", "/"), abs_path


def _rank_clock_name(name: str) -> int:
    low = name.lower()
    preferred = ["clk", "clock", "pclk", "aclk", "hclk", "fclk", "sys_clk", "core_clk"]
    return preferred.index(low) if low in preferred else len(preferred)


def _rank_reset_name(name: str) -> int:
    low = name.lower()
    preferred = ["rst_n", "reset_n", "aresetn", "resetn", "reset", "rst", "por_n", "resetb", "rstb"]
    return preferred.index(low) if low in preferred else len(preferred)


def _detect_clk_reset_names(ports: List[Dict[str, str]]) -> Tuple[Optional[str], Optional[str], bool]:
    input_ports = [p["name"] for p in ports if p.get("direction") == "input"]
    clocks = [n for n in input_ports if any(re.search(pat, n, flags=re.IGNORECASE) for pat in CLOCK_PATTERNS)]
    resets = [n for n in input_ports if any(re.search(pat, n, flags=re.IGNORECASE) for pat in RESET_PATTERNS)]

    clocks = sorted(dict.fromkeys(clocks), key=_rank_clock_name)
    resets = sorted(dict.fromkeys(resets), key=_rank_reset_name)

    rst_name = resets[0] if resets else None
    active_low = bool(rst_name and re.search(r"(?:_n$|resetn$|aresetn$|por_n$|resetb$|rstb$)", rst_name, flags=re.IGNORECASE))
    return (clocks[0] if clocks else None), rst_name, active_low


def _makefile(topmodule: str, verilog_sources: List[str], test_module: str) -> str:
    verilog_sources_str = " \\\n\t".join(verilog_sources)
    return f"""TOPLEVEL_LANG = verilog
TOPLEVEL = {topmodule}
MODULE = {test_module}
SIM ?= verilator

VERILOG_SOURCES = \\
\t{verilog_sources_str}

include $(shell cocotb-config --makefiles)/Makefile.sim
"""


def _render_python(harness_test_name: str, clk_name: Optional[str], rst_name: Optional[str], reset_active_low: bool) -> Tuple[str, str]:
    if not clk_name:
        minimal = f'''import cocotb
from cocotb.triggers import Timer

# ASSUMPTION: No clock-like input was detected on the generated top.
# ASSUMPTION: Firmware preload / runtime stimulus will be integrated later.

@cocotb.test()
async def {harness_test_name}(dut):
    await Timer(1, units="us")
'''
        return minimal, minimal

    if rst_name:
        inactive = "1" if reset_active_low else "0"
        active = "0" if reset_active_low else "1"
        reset_block = f'''
async def reset_sequence(dut, cycles=5):
    dut.{rst_name}.value = {active}
    for _ in range(cycles):
        await RisingEdge(dut.{clk_name})
    dut.{rst_name}.value = {inactive}
    for _ in range(cycles):
        await RisingEdge(dut.{clk_name})
'''
    else:
        reset_block = f'''
async def reset_sequence(dut, cycles=10):
    # ASSUMPTION: No reset-like input was detected on the generated top.
    for _ in range(cycles):
        await RisingEdge(dut.{clk_name})
'''

    harness = f'''import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

# ASSUMPTION: Only top-level DUT signals are used.
# ASSUMPTION: Firmware ELF preload / MMIO stimulus integration is a later step.
{reset_block}

async def smoke_wait(dut, cycles=20):
    for _ in range(cycles):
        await RisingEdge(dut.{clk_name})


async def maybe_drive_placeholder_stimulus(dut):
    # ASSUMPTION: Add runtime firmware/MMIO interactions here once execution flow is enabled.
    await Timer(1, units="ns")


@cocotb.test()
async def {harness_test_name}(dut):
    cocotb.start_soon(Clock(dut.{clk_name}, 10, units="ns").start())
    await reset_sequence(dut)
    await maybe_drive_placeholder_stimulus(dut)
    await smoke_wait(dut, cycles=20)
    await Timer(1, units="us")
'''

    smoke = f'''import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

# ASSUMPTION: Only top-level DUT signals are used.
# ASSUMPTION: Firmware ELF preload is not yet wired into this smoke test.


async def _watchdog(dut, cycles=200):
    for _ in range(cycles):
        await RisingEdge(dut.{clk_name})
    raise AssertionError("Watchdog timeout: smoke test made no visible progress")
{reset_block}

@cocotb.test()
async def firmware_test(dut):
    cocotb.start_soon(Clock(dut.{clk_name}, 10, units="ns").start())
    cocotb.start_soon(_watchdog(dut, cycles=200))
    await reset_sequence(dut)
    for _ in range(20):
        await RisingEdge(dut.{clk_name})
    await Timer(1, units="us")
'''
    return harness, smoke


def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    workflow_dir = os.path.abspath(state.get("workflow_dir") or "")
    if not workflow_dir:
        state["status"] = f"❌ {AGENT_NAME} missing workflow_dir"
        return state

    soc_top_relpath, soc_top_abs = _resolve_soc_top(state, workflow_dir)
    soc_top_text = _safe_read(soc_top_abs)
    if not soc_top_text:
        state["status"] = f"❌ SoC top RTL not found for cocotb harness generation: {soc_top_relpath}"
        return state

    topmodule = (
        state.get("soc_top_sim_module")
        or state.get("system_top_sim_module")
        or _infer_topmodule_from_sv(soc_top_text, fallback=os.path.splitext(os.path.basename(soc_top_relpath))[0] or "soc_top_sim")
    )

    verilog_sources = _resolve_required_verilog_sources(workflow_dir, soc_top_relpath, soc_top_text, state)
    top_ports = _extract_top_ports(soc_top_text)
    clk_name, rst_name, reset_active_low = _detect_clk_reset_names(top_ports)
    harness_code, smoke_test_code = _render_python("firmware_smoke", clk_name, rst_name, reset_active_low)
    makefile = _makefile(topmodule, verilog_sources, test_module=os.path.splitext(os.path.basename(SMOKE_TEST_PATH))[0])

    write_artifact(state, HARNESS_PATH, harness_code, key=os.path.basename(HARNESS_PATH))
    write_artifact(state, SMOKE_TEST_PATH, smoke_test_code, key=os.path.basename(SMOKE_TEST_PATH))
    write_artifact(state, MAKEFILE_PATH, makefile, key=os.path.basename(MAKEFILE_PATH))

    debug_payload = {
        "agent": AGENT_NAME,
        "soc_top_sim_path": soc_top_relpath,
        "soc_top_sim_module": topmodule,
        "resolved_verilog_sources": verilog_sources,
        "top_ports": top_ports,
        "detected_clock": clk_name,
        "detected_reset": rst_name,
        "reset_active_low": reset_active_low,
        "output_paths": {
            "harness": HARNESS_PATH,
            "smoke_test": SMOKE_TEST_PATH,
            "makefile": MAKEFILE_PATH,
        },
    }
    write_artifact(state, DEBUG_PATH, json.dumps(debug_payload, indent=2), key=os.path.basename(DEBUG_PATH))

    state["embedded_cocotb_makefile_path"] = MAKEFILE_PATH
    state["embedded_cocotb_test_paths"] = [SMOKE_TEST_PATH]
    state["cocotb_makefile_path"] = MAKEFILE_PATH
    state["cocotb_test_paths"] = [SMOKE_TEST_PATH]
    state["makefile_path"] = MAKEFILE_PATH
    state["test_paths"] = [SMOKE_TEST_PATH]
    state["cocotb_harness_path"] = HARNESS_PATH
    state["sim_harness_path"] = HARNESS_PATH
    state["rtl_inputs"] = list(verilog_sources)

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = HARNESS_PATH
    embedded["makefile_path"] = MAKEFILE_PATH
    embedded["test_paths"] = [SMOKE_TEST_PATH]
    embedded["cocotb_harness_path"] = HARNESS_PATH

    state["status"] = f"✅ {AGENT_NAME} done"
    return state
