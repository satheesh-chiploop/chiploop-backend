import json
from ._embedded_common import ensure_workflow_dir, llm_chat, write_artifact, strip_markdown_fences_for_code

AGENT_NAME = "Embedded Interrupt Mapping Agent"
PHASE = "irq_map"
OUTPUT_PATH = "firmware/isr/interrupts.rs"

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

    if not regmap_obj:
        state["status"] = "❌ firmware register map missing in state for interrupt generation"
        return state

    regmap_json = json.dumps(regmap_obj, indent=2)

    prompt = f"""USER SPEC:
{spec_text}

GOAL:
{goal}

TOOLCHAIN (for future extensibility):
{json.dumps(toolchain, indent=2)}

TOGGLES:
{json.dumps(toggles, indent=2)}

REGISTER MAP:
{regmap_json}

TASK:
Create interrupt vector mapping and ISR stubs.
MANDATORY:
- Include VECTOR_TABLE definition
 
  VECTOR_TABLE entries must have consistent type signature: unsafe extern "C" fn()
  VECTOR_TABLE must include:
  #[link_section = ".vector_table"]
  #[no_mangle]
  Vector table MUST follow embedded production layout:

pub static VECTOR_TABLE: [unsafe extern "C" fn(); 32]
Use vector table size 32 unless spec explicitly defines interrupt count.

Rules:
- Entry 0 must be Reset_Handler
- Remaining entries must be ISR handler function pointers
- Do NOT cast handlers to usize
- Provide exactly one DefaultHandler function
- Do NOT use C attributes such as __attribute__((weak))
- Do NOT use external stack pointer symbols
- Always generate a Reset_Handler stub
- Must compile in no_std firmware environment
- Other ISR stubs must call DefaultHandler by default
- VECTOR_TABLE must be declared exactly as:
  #[link_section = ".vector_table"]
  #[no_mangle]
  pub static VECTOR_TABLE: [unsafe extern "C" fn(); 32]
- VECTOR_TABLE entries must have consistent type signature: unsafe extern "C" fn()
- Do not reference undefined symbols
- Do NOT reference external stack pointer symbols such as STACK_POINTER or _stack_start
- The vector table must use Reset_Handler as entry 0

This file is a Rust MODULE.

DO NOT generate:
#![no_std]
#![allow(...)]
#![feature(...)]


OUTPUT REQUIREMENTS:
- Write the primary output to match this path: firmware/isr/interrupts.rs
- Keep it implementation-ready and consistent with Rust + Cargo + Verilator + Cocotb assumptions.
- If information is missing, Add assumptions only as Rust comments:// ASSUMPTION: ...
"""

    out = """// ASSUMPTION: Generic interrupt vector table for demo/co-sim runtime.
// ASSUMPTION: Real IRQ-specific handlers can replace DefaultHandler later.

pub type Isr = unsafe extern "C" fn();

pub unsafe extern "C" fn DefaultHandler() {
    loop {}
}

pub unsafe extern "C" fn Reset_Handler() {
    loop {}
}

#[link_section = ".vector_table"]
#[no_mangle]
pub static VECTOR_TABLE: [Isr; 32] = [
    Reset_Handler,
    DefaultHandler, DefaultHandler, DefaultHandler, DefaultHandler,
    DefaultHandler, DefaultHandler, DefaultHandler, DefaultHandler,
    DefaultHandler, DefaultHandler, DefaultHandler, DefaultHandler,
    DefaultHandler, DefaultHandler, DefaultHandler, DefaultHandler,
    DefaultHandler, DefaultHandler, DefaultHandler, DefaultHandler,
    DefaultHandler, DefaultHandler, DefaultHandler, DefaultHandler,
    DefaultHandler, DefaultHandler, DefaultHandler, DefaultHandler,
    DefaultHandler, DefaultHandler, DefaultHandler,
];
"""
    write_artifact(state, OUTPUT_PATH, out, key=OUTPUT_PATH.split("/")[-1])

    # lightweight state update for downstream agents
    embedded = state.setdefault("embedded", {})
    embedded[PHASE] = OUTPUT_PATH

    return state
