import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code

AGENT_NAME = "Embedded Boot Dependency Planner Agent"
PHASE = "boot_plan"
OUTPUT_PATH = "firmware/boot/boot_sequence.md"

def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

TOOLCHAIN (for future extensibility):
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:
Plan boot sequencing dependencies (clocks, resets, power domains).
OUTPUT REQUIREMENTS:
- Write the primary output to match this path: firmware/boot/boot_sequence.md
- Keep it implementation-ready and consistent with Rust + Cargo + Verilator + Cocotb assumptions.
- If information is missing, assumptions only as Python comments at top (# ASSUMPTION: ...). No prose.
"""

    out = llm_chat(prompt, system="You are a senior embedded firmware engineer for silicon bring-up and RTL co-simulation. Produce concise, production-quality outputs. NEVER include markdown fences.")
    if not out:
        out = "ERROR: LLM returned empty output."

    out = strip_markdown_fences_for_code(out)
    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    # Canonical firmware toolchain defaults for downstream build/sim agents.
    toolchain = state.setdefault("toolchain", {})

    # Only set defaults if upstream did not already provide them.
    target_triple = (
        toolchain.get("target_triple")
        or state.get("target_triple")
        or "x86_64-unknown-linux-gnu"
    )
    bin_name = (
        toolchain.get("bin_name")
        or state.get("firmware_bin_name")
        or "firmware_app"
    )

    toolchain["target_triple"] = target_triple
    toolchain["bin_name"] = bin_name

    state["target_triple"] = target_triple
    state["firmware_bin_name"] = bin_name

    # Helpful boot/runtime hints for downstream agents
    boot_block = state.setdefault("firmware_boot", {})
    boot_block["boot_sequence_path"] = OUTPUT_PATH
    boot_block["target_triple"] = target_triple
    boot_block["bin_name"] = bin_name

    write_artifact(
        state,
        "firmware/debug/boot_toolchain_debug.json",
        json.dumps({
           "agent": AGENT_NAME,
           "target_triple": state.get("target_triple"),
           "firmware_bin_name": state.get("firmware_bin_name"),
           "toolchain": state.get("toolchain"),
        }, indent=2),
        key="boot_toolchain_debug.json",
    )

    return state
    
