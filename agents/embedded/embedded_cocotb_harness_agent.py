import json
import os

from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code

AGENT_NAME = "Embedded Cocotb Harness Agent"
PHASE = "cocotb_harness"
OUTPUT_PATH = "firmware/validate/cocotb_harness.py"

def _safe_read(path):
    try:
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return ""

def _infer_topmodule_from_sv(sv_text: str, fallback: str = "soc_top_sim") -> str:
    if not sv_text:
        return fallback
    for line in sv_text.splitlines():
        s = line.strip()
        if s.startswith("module "):
            rest = s[len("module "):].strip()
            name = rest.split("(")[0].split("#")[0].strip()
            if name:
                return name
    return fallback

def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""

   

    

    soc_top_relpath = (
        state.get("soc_top_sim_path")
        or state.get("system_top_sim_path")
        or "system/integration/soc_top_sim.sv"
    )

    if workflow_dir:
        preferred = os.path.join(workflow_dir, soc_top_relpath)
        if not os.path.isfile(preferred):
            discovered = ""
            integ_dir = os.path.join(workflow_dir, "system", "integration")
            if os.path.isdir(integ_dir):
                for name in sorted(os.listdir(integ_dir)):
                    if name.endswith("_sim.sv"):
                        discovered = f"system/integration/{name}"
                        break
            if discovered:
                soc_top_relpath = discovered

    soc_top_abs = os.path.join(workflow_dir, soc_top_relpath) if workflow_dir else soc_top_relpath
    soc_top_text = _safe_read(soc_top_abs)
    regmap_text = _safe_read(os.path.join(workflow_dir, "firmware/register_map.json"))
    driver_text = _safe_read(os.path.join(workflow_dir, "firmware/drivers/driver_scaffold.rs"))

    soc_top_exists = bool(soc_top_abs and os.path.isfile(soc_top_abs))

    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

SOC TOP RTL (preferred if available):
{soc_top_text if soc_top_text else "(not available)"}

FIRMWARE REGISTER MAP (preferred if available):
{regmap_text if regmap_text else "(not available)"}

DRIVER LAYER (preferred if available):
{driver_text if driver_text else "(not available)"}

TOOLCHAIN:
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Generate Cocotb harness collateral for firmware-aware co-simulation.

RULES:
- Prefer SOC TOP RTL + REGISTER MAP + DRIVER artifacts when available.
- Fall back to USER SPEC if artifacts are unavailable.
- Generate a Makefile and at least one concrete test_*.py.
- The harness should target the actual top module when it can be inferred from SOC TOP RTL.


MANDATORY CONTENT:
- Create a clock using cocotb.clock.Clock
- Apply reset sequencing
- Include at least one example test coroutine
- Include placeholder for ELF preload or firmware stimulus
- Include a watchdog timeout to fail if expected progress never occurs
- Must import: Clock, RisingEdge, Timer

CORRECTNESS REQUIREMENTS:
- Use dut.signal.value = X syntax
- Use cocotb.clock.Clock for clock generation
- Never use <= operator
- Do not call .read() on DUT signals
- Use int(dut.<sig>.value) or dut.<sig>.value.integer when reading numeric values

STRICT RUNTIME RULES:
- Must include @cocotb.test()
- Clock must start using:
  cocotb.start_soon(Clock(...).start())
- Assume ONLY the following signals exist by default:
  dut.clk
  dut.rst_n
- Any access beyond dut.clk and dut.rst_n MUST be guarded with hasattr(dut, "<sig>")
- Do NOT use hierarchical signal access (no dut.A.B)
- If a flattened signal is needed, declare it as:
  # REQUIRED_SIGNAL: <signal_name>
  and only access dut.<signal_name> after hasattr(dut, "<signal_name>") is true

ARTIFACT-AWARE RULES:
- Prefer generated SoC top/module intent if available
- Prefer firmware/register-map/runtime artifacts if available
- If concrete DUT signals are not clearly available, generate a minimal safe smoke harness using only:
  - clock generation
  - reset sequencing
  - fixed-cycle wait
  - clean termination
- Do NOT reference non-existent DUT signals

FIRMWARE-AWARE RULES:
- If USER SPEC or generated artifacts clearly define firmware/MMIO/boot/status signals,
  you may include guarded placeholders for them
- Include placeholder comments for ELF preload / firmware stimulus even if not executable yet
- Any helper coroutine/function MUST accept dut as an argument

API RULES:
- Use only modern cocotb async API
- Do not use cocotb.coroutine
- Do not use yield-based coroutines
- Do not use cocotb.utils

MANDATORY STRUCTURE:

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

@cocotb.test()
async def firmware_test(dut):

TERMINATION RULES:
- Test must terminate cleanly with normal async return on success
- Fail on watchdog timeout or explicit assertion failure

HARD OUTPUT RULES:
- Output MUST be RAW PYTHON ONLY
- No markdown fences
- No headings
- No prose outside code
- Put assumptions as Python comments at the top:
  # ASSUMPTION: ...
- Keep it implementation-ready and consistent with Rust + Cargo + Verilator + Cocotb assumptions

OUTPUT PATH:
OUTPUTS (generate ALL using the format below):
Return multiple files in this exact format:

FILE: firmware/validate/cocotb_harness.py
<content>

FILE: firmware/validate/Makefile
<content>

FILE: firmware/validate/test_firmware_smoke.py
<content>

"""

    out = llm_chat(
        prompt,
        system="You are a senior verification engineer. Output ONLY the requested files. Never use markdown code fences."
    )
    if not out:
        out = "ERROR: LLM returned empty output."
    out = strip_markdown_fences_for_code(out)

    # Parse FILE: blocks
    files = {}
    current = None
    buf = []
    for line in out.splitlines():
        if line.startswith("FILE: "):
            if current:
                files[current] = "\n".join(buf).strip() + "\n"
            current = line.replace("FILE: ", "").strip()
            buf = []
        else:
            buf.append(line)
    if current:
        files[current] = "\n".join(buf).strip() + "\n"

    # Fallback: if model returned just raw python, keep backward-compatible behavior
    if not files:
        files = {
            "firmware/validate/cocotb_harness.py": out.strip() + "\n"
        }

    # Safety-sanitize only the python files
    for py_path in ("firmware/validate/cocotb_harness.py", "firmware/validate/test_firmware_smoke.py"):
        if py_path in files:
            py = files[py_path]

            sanitized = []
            for line in py.splitlines():
                if "dut." in line and ".value" in line:
                    after = line.split("dut.", 1)[-1]
                    if after.count(".") >= 2:
                        sanitized.append("# NOTE: removed hierarchical DUT access (requires explicit signal mapping):")
                        sanitized.append("# " + line)
                        continue
                sanitized.append(line)
            py = "\n".join(sanitized) + "\n"

            safe_lines = []
            defined_vars = set()
            for line in py.splitlines():
                stripped = line.strip()

                if "=" in stripped and not stripped.startswith("#"):
                    lhs = stripped.split("=", 1)[0].strip()
                    if lhs.isidentifier():
                        defined_vars.add(lhs)

                if stripped.startswith("print("):
                    bad = False
                    for token in stripped.replace("(", " ").replace(")", " ").replace(",", " ").split():
                        if token.isidentifier() and token not in defined_vars and token not in ("dut",):
                            safe_lines.append("# NOTE: removed unsafe print referencing undefined var")
                            safe_lines.append("# " + line)
                            bad = True
                            break
                    if not bad:
                        safe_lines.append(line)
                else:
                    safe_lines.append(line)

            files[py_path] = "\n".join(safe_lines) + "\n"

    inferred_top = _infer_topmodule_from_sv(soc_top_text, fallback="soc_top_sim")



    # Always harden / normalize Makefile because LLM output may exist but be wrong.

        # Resolve Verilog source path relative to Makefile location
    verilog_path = f"../../{soc_top_relpath}".replace("//", "/")

    if not soc_top_exists:
        # Keep the bundle truthful: emit a diagnostic Makefile comment if RTL is missing
        files["firmware/validate/Makefile"] = f"""# WARNING: SoC top RTL not found when harness was generated
# EXPECTED_RTL = {verilog_path}
TOPLEVEL_LANG = verilog
VERILOG_SOURCES = {verilog_path}
TOPLEVEL = {inferred_top}
MODULE = test_firmware_smoke
SIM = verilator

include $(shell cocotb-config --makefiles)/Makefile.sim
"""
    else:
        files["firmware/validate/Makefile"] = f"""TOPLEVEL_LANG = verilog
VERILOG_SOURCES = {verilog_path}
TOPLEVEL = {inferred_top}
MODULE = test_firmware_smoke
SIM = verilator

include $(shell cocotb-config --makefiles)/Makefile.sim
"""



    def _is_weak_python(py: str) -> bool:
        py = py or ""
        return (
            ("RisingEdge(" in py and "from cocotb.triggers import RisingEdge" not in py)
            or ("expected_value" in py)
            or ("while True" in py and "await timeout" in py)
            or ("from cocotb.reg import" in py)
            or ("dut." in py and ".value" not in py and "hasattr(" not in py and "dut.clk" not in py and "dut.rst_n" not in py)
        )

    safe_smoke_test = """import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

# ASSUMPTION: Only dut.clk and dut.rst_n are guaranteed to exist.
# ASSUMPTION: Firmware preload / ELF injection is a later integration step.

@cocotb.test()
async def firmware_test(dut):
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())

    dut.rst_n.value = 0
    for _ in range(5):
        await RisingEdge(dut.clk)

    dut.rst_n.value = 1
    for _ in range(10):
        await RisingEdge(dut.clk)

    await Timer(1, units="us")
"""

    safe_harness = """import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

# ASSUMPTION: Only dut.clk and dut.rst_n are guaranteed to exist.
# ASSUMPTION: Firmware preload is not yet wired into this harness.

async def reset_sequence(dut):
    dut.rst_n.value = 0
    for _ in range(5):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    for _ in range(5):
        await RisingEdge(dut.clk)

async def smoke_wait(dut, cycles=20):
    for _ in range(cycles):
        await RisingEdge(dut.clk)

@cocotb.test()
async def firmware_smoke(dut):
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    await reset_sequence(dut)
    await smoke_wait(dut, cycles=20)
    await Timer(1, units="us")
"""



 

    if "firmware/validate/test_firmware_smoke.py" not in files:
        files["firmware/validate/test_firmware_smoke.py"] = """import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

@cocotb.test()
async def firmware_test(dut):
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    dut.rst_n.value = 0
    for _ in range(5):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    for _ in range(10):
        await RisingEdge(dut.clk)
    await Timer(1, units="us")
"""
    if (
        "firmware/validate/test_firmware_smoke.py" not in files
        or _is_weak_python(files.get("firmware/validate/test_firmware_smoke.py", ""))
    ):
        files["firmware/validate/test_firmware_smoke.py"] = safe_smoke_test

    if (
        "firmware/validate/cocotb_harness.py" not in files
        or _is_weak_python(files.get("firmware/validate/cocotb_harness.py", ""))
    ):
        files["firmware/validate/cocotb_harness.py"] = safe_harness

    for p, content in files.items():
        write_artifact(state, p, content, key=p.split("/")[-1])



    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = "firmware/validate/cocotb_harness.py"


    state["embedded_cocotb_makefile_path"] = "firmware/validate/Makefile"
    state["embedded_cocotb_test_paths"] = ["firmware/validate/test_firmware_smoke.py"]

    state["makefile_path"] = "firmware/validate/Makefile"
    state["test_paths"] = ["firmware/validate/test_firmware_smoke.py"]
    state["cocotb_makefile_path"] = "firmware/validate/Makefile"
    state["cocotb_test_paths"] = ["firmware/validate/test_firmware_smoke.py"]

    if soc_top_exists:
        state["soc_top_sim_path"] = soc_top_relpath

        existing_rtl = state.get("rtl_inputs") or []
        if not isinstance(existing_rtl, list):
            existing_rtl = [existing_rtl] if existing_rtl else []

        merged_rtl = []
        for p in existing_rtl + [soc_top_relpath]:
            if isinstance(p, str) and p.strip() and p not in merged_rtl:
                merged_rtl.append(p)
        state["rtl_inputs"] = merged_rtl

    state["cocotb_bundle_ready"] = soc_top_exists
    state["cocotb_soc_top_exists"] = soc_top_exists

    # Production runtime contract for downstream execution agent
    state["cosim_logs_dir"] = "system/firmware/cosim/logs"
    state["cosim_waves_dir"] = "system/firmware/cosim/waves"
    state["cosim_results_dir"] = "system/firmware/cosim/results"

    inferred_test_module = "test_firmware_smoke"
    state["cocotb_test_module"] = inferred_test_module
    state["cosim_make_cmd"] = "make"
    state["cosim_make_args"] = [
        "-f",
        "firmware/validate/Makefile",
        f"MODULE={inferred_test_module}",
    ]

    return state

    
    

   
    