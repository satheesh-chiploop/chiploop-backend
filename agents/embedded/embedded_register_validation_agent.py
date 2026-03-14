import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact

AGENT_NAME = "Embedded Register Validation Agent"
PHASE = "reg_validate"
OUTPUT_PATH = "firmware/hal/register_validation.md"

def run_agent(state: dict) -> dict:
    print(f"\n🚀 Running {AGENT_NAME}...")
    ensure_workflow_dir(state)

    spec_text = (state.get("spec_text") or state.get("spec") or "").strip()
    goal = (state.get("goal") or "").strip()
    toolchain = state.get("toolchain") or {}
    toggles = state.get("toggles") or {}

    regmap_obj = (
        state.get("firmware_register_map")
        or (state.get("firmware") or {}).get("register_map")
    )

    hal_code = (
        state.get("firmware_hal_code")
        or (state.get("firmware") or {}).get("hal_code")
    )

    driver_code = (
        state.get("firmware_driver_code")
        or (state.get("firmware") or {}).get("driver_code")
    )

    if not regmap_obj:
        state["status"] = "❌ firmware register map missing in state for register validation"
        return state

    if not hal_code:
        state["status"] = "❌ firmware HAL missing in state for register validation"
        return state

    driver_present = bool(driver_code)

    regmap_json = json.dumps(regmap_obj, indent=2)

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

REGISTER MAP:
{regmap_json}

HAL REGISTER LAYER:
{hal_code}


DRIVER SCAFFOLD:
{driver_code if driver_code else "(not available yet)"}

TOOLCHAIN (for future extensibility):
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

TASK:

TASK:
Validate register map consistency and HAL correctness.
If DRIVER SCAFFOLD is present, also validate driver consistency against HAL/register map.
If DRIVER SCAFFOLD is missing, explicitly note that driver validation was skipped.


OUTPUT REQUIREMENTS:
- Write the primary output to match this path: firmware/hal/register_validation.md
- Check that HAL register names match REGISTER MAP.
- Check that DRIVER uses actual HAL/register names.
- Report mismatches explicitly.
- Keep it implementation-ready and consistent with Rust + Cargo + Verilator + Cocotb assumptions.
- If information is missing, make reasonable assumptions and clearly list them inside the artifact.
"""

    out = llm_chat(prompt, system="You are a senior embedded firmware engineer for silicon bring-up and RTL co-simulation. Produce concise, production-quality outputs. Avoid markdown code fences unless explicitly asked.")
    if not out:
        out = "ERROR: LLM returned empty output."

    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
