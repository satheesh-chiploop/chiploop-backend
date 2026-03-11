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

def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    workflow_dir = state.get("workflow_dir") or ""

    soc_top_text = _safe_read(os.path.join(workflow_dir, "system/integration/soc_top_sim.sv"))
    regmap_text = _safe_read(os.path.join(workflow_dir, "firmware/register_map.json"))
    driver_text = _safe_read(os.path.join(workflow_dir, "firmware/drivers/driver_scaffold.rs"))

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

    # Synthesize missing runtime artifacts if the model omitted them
    if "firmware/validate/Makefile" not in files:
        files["firmware/validate/Makefile"] = """TOPLEVEL_LANG = verilog
SIM ?= verilator
TOPLEVEL = soc_top_sim
MODULE = test_firmware_smoke

include $(shell cocotb-config --makefiles)/Makefile.sim
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

    for p, content in files.items():
        write_artifact(state, p, content, key=p.split("/")[-1])

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = "firmware/validate/cocotb_harness.py"
    state["embedded_cocotb_makefile_path"] = "firmware/validate/Makefile"
    state["embedded_cocotb_test_paths"] = ["firmware/validate/test_firmware_smoke.py"]

    return state

    out = llm_chat(prompt, system="You are a senior verification engineer. Produce runnable Python only. Never use markdown code fences.")
    if not out:
        out = "ERROR: LLM returned empty output."
    out = strip_markdown_fences_for_code(out)

        # Safety: block hierarchical dut access (dut.A.B) which breaks on most DUTs
    sanitized = []
    for line in out.splitlines():
        if "dut." in line and ".value" in line:
            # If line contains dut.<x>.<y>, comment it out
            # (Very lightweight: detect two dots after dut.)
            after = line.split("dut.", 1)[-1]
            if after.count(".") >= 2:
                sanitized.append("# NOTE: removed hierarchical DUT access (requires explicit signal mapping):")
                sanitized.append("# " + line)
                continue
        sanitized.append(line)
    out = "\n".join(sanitized) + "\n"

    # Safety: prevent NameError from removed signal reads
    safe_lines = []
    defined_vars = set()

    for line in out.splitlines():
        stripped = line.strip()

        # Track simple assignments like var =
        if "=" in stripped and not stripped.startswith("#"):
            lhs = stripped.split("=", 1)[0].strip()
            if lhs.isidentifier():
                defined_vars.add(lhs)

        # If printing undefined variable, guard it
        if stripped.startswith("print("):
            for token in stripped.replace("(", " ").replace(")", " ").split():
                if token.isidentifier() and token not in defined_vars and token not in ("dut",):
                    safe_lines.append("# NOTE: removed unsafe print referencing undefined var")
                    safe_lines.append("# " + line)
                    break
            else:
                safe_lines.append(line)
        else:
            safe_lines.append(line)

    out = "\n".join(safe_lines) + "\n"

    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state

    