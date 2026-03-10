import os
import re
import json
import datetime
from typing import Dict, Any, List, Optional

from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text


def _now() -> str:
    return datetime.datetime.now().isoformat()


def _count_assertions_in_text(text: str) -> int:
    if not text:
        return 0
    count = 0
    count += len(re.findall(r"\bassert\s+property\b", text))
    count += len(re.findall(r"\bassert\s*\(", text))
    return count


def _clean_sv_output(raw: str) -> str:
    if not raw:
        return ""

    raw = raw.replace("```systemverilog", "")
    raw = raw.replace("```sv", "")
    raw = raw.replace("```verilog", "")
    raw = raw.replace("```", "")
    return raw.strip()


def _find_rtl_files(workflow_dir: str) -> List[str]:
    exts = (".v", ".sv")
    out: List[str] = []
    for root, _, files in os.walk(workflow_dir):
        for fn in files:
            if fn.lower().endswith(exts):
                out.append(os.path.join(root, fn))
    out.sort()
    return out

def _pick_rtl_file(state: dict, workflow_dir: str) -> Optional[str]:
    # 1) Prefer assembled SoC sim top for System_SIM
    soc_top_sim_path = state.get("soc_top_sim_path")
    if isinstance(soc_top_sim_path, str):
        cand = soc_top_sim_path
        if not os.path.isabs(cand):
            cand = os.path.join(workflow_dir, cand)
        if os.path.exists(cand):
            return cand

    # 2) Explicit path if present
    explicit = state.get("rtl_file")
    if explicit and os.path.exists(explicit):
        return explicit

    # 3) rtl_files list from state
    rtl_files = state.get("rtl_files")
    if isinstance(rtl_files, list):
        for p in rtl_files:
            if isinstance(p, str) and os.path.exists(p):
                return p

    # 4) Prefer system/integration tops if discovered

        # 4) Prefer assembled system tops if discovered
    discovered = _find_rtl_files(workflow_dir)
    preferred = []
    fallback = []

    for p in discovered:
        norm = p.replace("\\", "/").lower()
        if (
            ("/system/integration/" in norm or "/system/integrate/" in norm)
            and norm.endswith(".sv")
        ):
            preferred.append(p)
        else:
            fallback.append(p)
    

    if preferred:
        return preferred[0]
    if fallback:
        return fallback[0]

    return None




def _pick_top_module_name(state: dict, rtl_text: str) -> str:
    if state.get("soc_top_sim_module"):
        return str(state["soc_top_sim_module"]).strip()

    if state.get("top_module"):
        return str(state["top_module"]).strip()

    m = re.search(r"^\s*module\s+([a-zA-Z_][a-zA-Z0-9_$]*)\b", rtl_text, flags=re.MULTILINE)
    if m:
        return m.group(1)

    return "top"


def _infer_clock_reset_names(state: dict, rtl_text: str) -> Dict[str, Optional[str]]:
    # Prefer existing state hints
    clk = state.get("clock_name")
    rst = state.get("reset_name")

    if isinstance(clk, str) and clk.strip():
        clk = clk.strip()
    else:
        clk = None

    if isinstance(rst, str) and rst.strip():
        rst = rst.strip()
    else:
        rst = None

    # Best-effort scan from ports in RTL text
    if not clk:
        for cand in ["clk", "clock", "core_clk", "i_clk"]:
            if re.search(rf"\b{re.escape(cand)}\b", rtl_text):
                clk = cand
                break

    if not rst:
        for cand in ["rst_n", "reset_n", "rst", "reset", "i_rst_n", "i_reset_n"]:
            if re.search(rf"\b{re.escape(cand)}\b", rtl_text):
                rst = cand
                break

    return {"clock": clk, "reset": rst}


def _default_assertions(top_module: str, clock_name: Optional[str], reset_name: Optional[str]) -> str:
    clk = clock_name or "clk"
    rst = reset_name or "rst_n"
    active_low = rst.lower().endswith("_n")

    disable_expr = f"!{rst}" if active_low else rst
    reset_idle_expr = f"!{rst}" if active_low else rst

    return f"""default clocking cb @(posedge {clk}); endclocking

property p_clock_known;
  !$isunknown({clk});
endproperty
ap_clock_known: assert property (p_clock_known);

property p_reset_known;
  !$isunknown({rst});
endproperty
ap_reset_known: assert property (p_reset_known);

property p_no_x_after_reset;
  disable iff ({disable_expr})
  1'b1 |-> !$isunknown({clk});
endproperty
ap_no_x_after_reset: assert property (p_no_x_after_reset);

cp_reset_observed: cover property (@(posedge {clk}) {reset_idle_expr});
"""


def run_agent(state: dict) -> dict:
    agent_name = "Digital Assertions (SVA) Agent"
    print("\n🧠 Running Digital Assertions (SVA) Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    rtl_file = _pick_rtl_file(state, workflow_dir)
    if not rtl_file:
        state["status"] = "❌ RTL file not found for assertion generation."
        return state

    try:
        with open(rtl_file, "r", encoding="utf-8", errors="ignore") as f:
            rtl_text = f.read()
    except Exception as e:
        state["status"] = f"❌ Failed to read RTL file: {e}"
        return state

    top_module = _pick_top_module_name(state, rtl_text)
    sigs = _infer_clock_reset_names(state, rtl_text)
    clock_name = sigs["clock"]
    reset_name = sigs["reset"]

    prompt = f"""
You are an expert SystemVerilog verification engineer.

TASK:
Generate a valid SystemVerilog Assertions file for the RTL module below.

GOALS:
- Reset behavior checks
- Enable/disable behavior checks if signals exist
- Counter/FSM transition sanity if such logic is inferable
- Clock stability / no unknown clock
- Overflow/underflow protection if inferable
- Include at least one cover property

STRICT RULES:
- Output ONLY raw SystemVerilog code
- No markdown fences
- No English explanation before or after the code
- No backticks or macros
- No module wrapper
- Use labeled properties/assertions
- Start with: default clocking @(posedge <clock_port>); endclocking
- Prefer the real clock/reset names from the RTL
- Keep syntax broadly simulator-friendly
- If a signal does not clearly exist, do not invent complex checks for it
- If reset appears active-low, use disable iff (!reset_name)
- If reset appears active-high, use disable iff (reset_name)

TOP MODULE:
{top_module}

PREFERRED CLOCK:
{clock_name or "(infer from RTL)"}

PREFERRED RESET:
{reset_name or "(infer from RTL if present)"}

RTL SNIPPET:
{rtl_text[:6000]}

Now output only valid SystemVerilog assertion code.
""".strip()

    raw = ""
    try:
        raw = llm_text(prompt) or ""
    except Exception as e:
        raw = ""
        print(f"⚠️ llm_text failed in assertion agent: {e}")

    assertions_code = _clean_sv_output(raw)

    if not assertions_code:
        assertions_code = _default_assertions(top_module, clock_name, reset_name)

    # If LLM omitted default clocking, prepend a safe fallback
    if "default clocking" not in assertions_code:
        clk = clock_name or "clk"
        assertions_code = f"default clocking cb @(posedge {clk}); endclocking\n\n" + assertions_code

    assertions_total = _count_assertions_in_text(assertions_code)

    metadata: Dict[str, Any] = {
        "type": "digital_assertions_metadata",
        "version": "1.0",
        "generated_at": _now(),
        "top_module": top_module,
        "rtl_file": rtl_file,
        "clock_name": clock_name,
        "reset_name": reset_name,
        "assertions_total": assertions_total,
        "note": "Demo metadata for System_SIM assertion coverage summary.",
    }

    log_txt = "\n".join([
        f"[{_now()}] Assertion generation completed",
        f"rtl_file={rtl_file}",
        f"top_module={top_module}",
        f"clock_name={clock_name}",
        f"reset_name={reset_name}",
        f"assertions_total={assertions_total}",
    ]) + "\n"

    # Write local workflow artifacts
    assertions_sv_path = os.path.join(workflow_dir, "assertions.sv")
    metadata_path = os.path.join(workflow_dir, "assertions_metadata.json")
    log_path = os.path.join(workflow_dir, "digital_assertion_agent.log")

    with open(assertions_sv_path, "w", encoding="utf-8") as f:
        f.write(assertions_code)

    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(log_txt)

    # Upload canonical artifacts
    try:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="vv/tb",
            filename="assertions.sv",
            content=assertions_code,
        )
    except Exception as e:
        print(f"⚠️ Failed to upload vv/tb/assertions.sv: {e}")

    try:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="vv/tb",
            filename="assertions_metadata.json",
            content=json.dumps(metadata, indent=2),
        )
    except Exception as e:
        print(f"⚠️ Failed to upload vv/tb/assertions_metadata.json: {e}")

    # Legacy compatibility artifact path
    try:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="",
            filename="assertions.sv",
            content=assertions_code,
        )
    except Exception as e:
        print(f"⚠️ Failed to upload legacy assertions.sv: {e}")

    try:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="vv",
            filename="digital_assertion_agent.log",
            content=log_txt,
        )
    except Exception as e:
        print(f"⚠️ Failed to upload digital_assertion_agent.log: {e}")

    state["assertions_sv"] = assertions_code
    state["assertions_metadata"] = metadata
    state["assertions_total"] = assertions_total
    state["artifact"] = assertions_sv_path
    state["artifact_log"] = log_path
    state["status"] = f"✅ Assertions generated ({assertions_total} assertions)"

    print(f"✅ Digital Assertions Agent completed — {assertions_sv_path}")
    return state