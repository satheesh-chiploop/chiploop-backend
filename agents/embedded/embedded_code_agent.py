import os
import json
from datetime import datetime
from typing import Any, Dict, Optional, List

from utils.artifact_utils import save_text_artifact_and_record

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

client_openai = OpenAI() if OpenAI else None


def _log(log_path: str, msg: str) -> None:
    print(msg)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")


def _find_existing_json_path(state: dict, candidates: List[str]) -> Optional[str]:
    for k in candidates:
        v = state.get(k)
        if isinstance(v, str) and v and os.path.exists(v) and v.lower().endswith(".json"):
            return v
    return None


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _gen_cpp_from_spec(spec: Dict[str, Any]) -> str:
    mmio = (spec.get("mmio") or {})
    base = mmio.get("base_address", "0x40000000")
    regs = mmio.get("registers", [])

    # Extract CONTROL0/STATUS0 bitfields if present
    control = next((r for r in regs if r.get("name") == "CONTROL0"), None) or {}
    status = next((r for r in regs if r.get("name") == "STATUS0"), None) or {}
    control_fields = control.get("fields", []) or []
    status_fields = status.get("fields", []) or []

    def field_enum(fields: list) -> str:
        lines = []
        for f in fields:
            name = str(f.get("name", "sig")).upper()
            lsb = int(f.get("lsb", 0))
            msb = int(f.get("msb", lsb))
            lines.append(f"  {name}_LSB = {lsb},")
            lines.append(f"  {name}_MSB = {msb},")
        return "\n".join(lines) if lines else "  // (none)\n"

    # A generic firmware skeleton that looks like real bare-metal style:
    # - MMIO reads/writes
    # - register bit packing helpers
    # - a simple poll loop (no hard-coded semantic signal names)
    return f"""\
/*
 * Auto-generated generic firmware skeleton (C++).
 * Integration model: AXI4-Lite MMIO register block exported by RTL.
 *
 * NOTE: This is scaffolding. Real firmware will add startup code, clock init,
 * UART, IRQ handlers, and platform headers.
 */

#include <cstdint>

namespace mmio {{
  static constexpr uintptr_t BASE = {base};

  // Register offsets (bytes)
  static constexpr uintptr_t CONTROL0_OFF = 0x00;
  static constexpr uintptr_t STATUS0_OFF  = 0x04;
  static constexpr uintptr_t IRQ_STATUS_OFF = 0x08;
  static constexpr uintptr_t IRQ_MASK_OFF   = 0x0C;

  inline void write32(uintptr_t off, uint32_t v) {{
    *reinterpret_cast<volatile uint32_t*>(BASE + off) = v;
  }}

  inline uint32_t read32(uintptr_t off) {{
    return *reinterpret_cast<volatile uint32_t*>(BASE + off);
  }}

  inline uint32_t set_field(uint32_t reg, uint32_t lsb, uint32_t msb, uint32_t value) {{
    const uint32_t width = (msb - lsb + 1u);
    const uint32_t mask  = (width >= 32u) ? 0xFFFFFFFFu : ((1u << width) - 1u);
    reg &= ~(mask << lsb);
    reg |= ((value & mask) << lsb);
    return reg;
  }}

  inline uint32_t get_field(uint32_t reg, uint32_t lsb, uint32_t msb) {{
    const uint32_t width = (msb - lsb + 1u);
    const uint32_t mask  = (width >= 32u) ? 0xFFFFFFFFu : ((1u << width) - 1u);
    return (reg >> lsb) & mask;
  }}
}}

enum Control0Bits {{
{field_enum(control_fields)}
}};

enum Status0Bits {{
{field_enum(status_fields)}
}};

static void delay_cycles(volatile uint32_t cycles) {{
  while (cycles--) {{
    __asm__ volatile("nop");
  }}
}}

int main() {{
  // Example: write deterministic pattern into CONTROL0 fields (if any).
  uint32_t ctrl = 0;

  // If your spec has CONTROL0 fields, you can set them here.
  // Example pattern: set each 1-bit field to 1.
  // (This is generic and safe; replace with real behavior.)
"""

    # append generic field sets
    # We'll set 1-bit controls to 1, multi-bit to 1.
    # If none, just keep ctrl=0.
    # (Do not hard-code semantic names.)
    # We'll build at generation time.
    # This function returns string; we will inject below in Python.
    """


def _append_field_sets(control_fields: list) -> str:
    lines = []
    for f in control_fields:
        lsb = int(f.get("lsb", 0))
        msb = int(f.get("msb", lsb))
        name = str(f.get("name", "sig")).upper()
        # set a small non-zero value within width
        lines.append(f"  // CONTROL0.{name} ({msb}:{lsb})")
        lines.append(f"  ctrl = mmio::set_field(ctrl, {lsb}, {msb}, 1u);")
    return "\n".join(lines) if lines else "  // No CONTROL0 fields found in spec.\n"


def _append_status_reads(status_fields: list) -> str:
    lines = []
    lines.append("    const uint32_t st = mmio::read32(mmio::STATUS0_OFF);")
    if not status_fields:
        lines.append("    (void)st; // No STATUS0 fields in spec.\n")
        return "\n".join(lines)
    lines.append("    // Extract fields (generic); hook into logging/UART as needed.")
    for f in status_fields:
        lsb = int(f.get("lsb", 0))
        msb = int(f.get("msb", lsb))
        name = str(f.get("name", "sig")).upper()
        lines.append(f"    volatile uint32_t {name.lower()} = mmio::get_field(st, {lsb}, {msb});")
        lines.append(f"    (void){name.lower()};")
    return "\n".join(lines)


def _render_cpp(spec: Dict[str, Any]) -> str:
    mmio = (spec.get("mmio") or {})
    base = mmio.get("base_address", "0x40000000")
    regs = mmio.get("registers", [])
    control = next((r for r in regs if r.get("name") == "CONTROL0"), None) or {}
    status = next((r for r in regs if r.get("name") == "STATUS0"), None) or {}
    control_fields = control.get("fields", []) or []
    status_fields = status.get("fields", []) or []

    # Start with base template from _gen_cpp_from_spec
    base_cpp = _gen_cpp_from_spec(spec)

    # Inject field sets and loop body and close main()
    injected = (
        base_cpp
        + _append_field_sets(control_fields)
        + f"""

  mmio::write32(mmio::CONTROL0_OFF, ctrl);

  while (true) {{
{_append_status_reads(status_fields)}
    // Simple pacing; replace with timer interrupt/RTOS tick in real systems.
    delay_cycles(50000);
  }}

  return 0;
}}
"""
    )
    # Fix base placeholder duplication (because _gen_cpp_from_spec ends mid-string)
    # Ensure it compiles cleanly.
    return injected.replace('"""\n\n\n', "")


def _maybe_llm_improve_cpp(spec: Dict[str, Any], cpp: str, log_path: str) -> str:
    """
    Optional LLM improvement step:
    - keep MMIO approach
    - do not invent semantic signal meaning
    - improve structure/comments slightly
    """
    if not client_openai:
        return cpp

    try:
        prompt = (
            "You are an expert embedded firmware engineer.\n"
            "Improve this generic C++ bare-metal firmware skeleton while keeping it hardware-agnostic:\n"
            "- MUST keep MMIO register access style\n"
            "- MUST NOT invent semantic meaning for signals (no 'overheat_flag' etc.)\n"
            "- MAY add a lightweight driver class + clean structure\n"
            "- MUST return code only (no markdown)\n\n"
            f"SPEC_JSON:\n{json.dumps(spec, indent=2)}\n\n"
            f"CODE:\n{cpp}\n"
        )
        resp = client_openai.chat.completions.create(
            model=os.getenv("EMBEDDED_CODE_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": "Return code only. No markdown."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        _log(log_path, f"LLM improve step skipped/failed: {e}")
        return cpp


def run_agent(state: dict) -> dict:
    print("\n💻 Running Embedded Code Agent (generic C++ firmware)…")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"

    os.makedirs("artifact", exist_ok=True)
    log_path = os.path.join("artifact", "embedded_code_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Embedded Code Agent Log\n")

    # Find embedded spec JSON
    embedded_spec_path = _find_existing_json_path(
        state,
        candidates=[
            "embedded_spec_json_path",
            "embedded_spec_path",
            "code_spec_json",
            "spec_json_path",
        ],
    )

    embedded_spec = None
    if embedded_spec_path:
        embedded_spec = _load_json(embedded_spec_path)
        _log(log_path, f"Loaded embedded spec from: {embedded_spec_path}")
    elif isinstance(state.get("embedded_spec_json"), dict):
        embedded_spec = state["embedded_spec_json"]
        _log(log_path, "Loaded embedded spec from state['embedded_spec_json']")
    else:
        _log(log_path, "ERROR: Could not locate embedded spec JSON.")
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name="Embedded Code Agent",
            subdir="embedded",
            filename="embedded_code_agent.log",
            content=open(log_path, "r", encoding="utf-8").read(),
        )
        state["embedded_code_error"] = "missing_embedded_spec"
        return state

    cpp = _render_cpp(embedded_spec)
    cpp = _maybe_llm_improve_cpp(embedded_spec, cpp, log_path)

    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Embedded Code Agent",
        subdir="embedded",
        filename="main.cpp",
        content=cpp,
    )
    save_text_artifact_and_record(
        workflow_id=workflow_id,
        agent_name="Embedded Code Agent",
        subdir="embedded",
        filename="embedded_code_agent.log",
        content=open(log_path, "r", encoding="utf-8").read(),
    )

    state["embedded_code_path"] = os.path.join(workflow_dir, "embedded", "main.cpp")
    return state

