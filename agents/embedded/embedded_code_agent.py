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


# -----------------------------
# Helpers
# -----------------------------
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


def _find_reg(regs: list, name: str) -> Dict[str, Any]:
    for r in regs:
        if str(r.get("name", "")).upper() == name.upper():
            return r
    return {}


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _field_enums(fields: list, enum_name: str) -> str:
    """
    Creates a minimal enum with <FIELD>_LSB / <FIELD>_MSB entries.
    """
    lines = [f"enum class {enum_name} : uint32_t {{"]

    if not fields:
        lines.append("  // (none)")
        lines.append("};")
        return "\n".join(lines)

    for f in fields:
        fname = str(f.get("name", "sig")).upper()
        lsb = _safe_int(f.get("lsb", 0))
        msb = _safe_int(f.get("msb", lsb))
        lines.append(f"  {fname}_LSB = {lsb},")
        lines.append(f"  {fname}_MSB = {msb},")
    lines.append("};")
    return "\n".join(lines)


def _emit_control_field_sets(control_fields: list) -> str:
    """
    Generic, non-semantic: set each CONTROL0 field to a small non-zero value.
    """
    if not control_fields:
        return "  // No CONTROL0 fields found in spec.\n"

    out = []
    for f in control_fields:
        fname = str(f.get("name", "sig")).upper()
        lsb = _safe_int(f.get("lsb", 0))
        msb = _safe_int(f.get("msb", lsb))
        out.append(f"  // CONTROL0.{fname} ({msb}:{lsb})")
        out.append(f"  ctrl = mmio::set_field(ctrl, {lsb}u, {msb}u, 1u);")
    return "\n".join(out) + "\n"


def _emit_status_field_reads(status_fields: list) -> str:
    """
    Generic: read STATUS0 and extract each field into a volatile.
    """
    lines = []
    lines.append("    const uint32_t st = mmio::read32(mmio::STATUS0_OFF);")

    if not status_fields:
        lines.append("    (void)st; // No STATUS0 fields in spec.")
        return "\n".join(lines)

    lines.append("    // Extract fields (generic). Hook into logging/UART as needed.")
    for f in status_fields:
        fname = str(f.get("name", "sig")).upper()
        lsb = _safe_int(f.get("lsb", 0))
        msb = _safe_int(f.get("msb", lsb))
        var = fname.lower()
        lines.append(f"    volatile uint32_t {var} = mmio::get_field(st, {lsb}u, {msb}u);")
        lines.append(f"    (void){var};")
    return "\n".join(lines)


def _render_cpp(spec: Dict[str, Any]) -> str:
    mmio = spec.get("mmio") or {}
    base_address = mmio.get("base_address", "0x40000000")

    regs = mmio.get("registers", []) or []
    control0 = _find_reg(regs, "CONTROL0")
    status0 = _find_reg(regs, "STATUS0")

    control_fields = control0.get("fields", []) or []
    status_fields = status0.get("fields", []) or []

    control_enum = _field_enums(control_fields, "Control0Bits")
    status_enum = _field_enums(status_fields, "Status0Bits")

    # Keep it “industry-realistic bare-metal style” but generic.
    # No custom semantic signals.
    cpp_lines = [
        "/*",
        " * Auto-generated generic firmware skeleton (C++).",
        " * Integration model: MMIO register block (e.g., AXI4-Lite) exported by RTL.",
        " *",
        " * Notes:",
        " * - This is scaffolding (no startup, no UART impl, no RTOS).",
        " * - Intended for co-sim bringup: firmware reads/writes MMIO registers",
        " *   while RTL provides the register block behavior.",
        " */",
        "",
        "#include <cstdint>",
        "#include <cstddef>",
        "",
        "namespace mmio {",
        f"  static constexpr uintptr_t BASE = {base_address};",
        "",
        "  // Register offsets (bytes) — default layout used across ChipLoop examples",
        "  static constexpr uintptr_t CONTROL0_OFF    = 0x00;",
        "  static constexpr uintptr_t STATUS0_OFF     = 0x04;",
        "  static constexpr uintptr_t IRQ_STATUS_OFF  = 0x08;",
        "  static constexpr uintptr_t IRQ_MASK_OFF    = 0x0C;",
        "",
        "  inline void write32(uintptr_t off, uint32_t v) {",
        "    *reinterpret_cast<volatile uint32_t*>(BASE + off) = v;",
        "  }",
        "",
        "  inline uint32_t read32(uintptr_t off) {",
        "    return *reinterpret_cast<volatile uint32_t*>(BASE + off);",
        "  }",
        "",
        "  inline uint32_t set_field(uint32_t reg, uint32_t lsb, uint32_t msb, uint32_t value) {",
        "    const uint32_t width = (msb - lsb + 1u);",
        "    const uint32_t mask  = (width >= 32u) ? 0xFFFFFFFFu : ((1u << width) - 1u);",
        "    reg &= ~(mask << lsb);",
        "    reg |= ((value & mask) << lsb);",
        "    return reg;",
        "  }",
        "",
        "  inline uint32_t get_field(uint32_t reg, uint32_t lsb, uint32_t msb) {",
        "    const uint32_t width = (msb - lsb + 1u);",
        "    const uint32_t mask  = (width >= 32u) ? 0xFFFFFFFFu : ((1u << width) - 1u);",
        "    return (reg >> lsb) & mask;",
        "  }",
        "}",
        "",
        control_enum,
        "",
        status_enum,
        "",
        "static void delay_cycles(volatile uint32_t cycles) {",
        "  while (cycles--) {",
        '    __asm__ volatile("nop");',
        "  }",
        "}",
        "",
        "int main() {",
        "  // CONTROL0 programming example (generic). Replace with real device driver behavior.",
        "  uint32_t ctrl = 0u;",
        "",
        _emit_control_field_sets(control_fields).rstrip(),
        "",
        "  mmio::write32(mmio::CONTROL0_OFF, ctrl);",
        "",
        "  // Poll loop: reads STATUS0 and extracts fields (generic).",
        "  while (true) {",
        _emit_status_field_reads(status_fields),
        "    delay_cycles(50000u);",
        "  }",
        "",
        "  return 0;",
        "}",
        "",
    ]

    # Ensure we don't accidentally inject "None"
    return "\n".join([line for line in cpp_lines if line is not None])


def _maybe_llm_improve_cpp(spec: Dict[str, Any], cpp: str, log_path: str) -> str:
    """
    Optional: let an LLM improve formatting/structure without inventing semantics.
    """
    if not client_openai:
        return cpp

    try:
        prompt = (
            "You are an expert embedded firmware engineer.\n"
            "Improve this generic C++ bare-metal firmware skeleton while keeping it hardware-agnostic:\n"
            "- MUST keep MMIO register access style\n"
            "- MUST NOT invent semantic meaning for signals (no custom flags like overheat_flag)\n"
            "- MAY add a small driver wrapper class, better comments, safer patterns\n"
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
        improved = (resp.choices[0].message.content or "").strip()
        return improved if improved else cpp
    except Exception as e:
        _log(log_path, f"LLM improve step skipped/failed: {e}")
        return cpp


# -----------------------------
# Agent entrypoint
# -----------------------------
def run_agent(state: dict) -> dict:
    print("\n💻 Running Embedded Code Agent (generic C++ firmware)…")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"

    # Local log file (do not assume this is downloadable; we upload to Supabase via artifact util)
    os.makedirs("artifact", exist_ok=True)
    log_path = os.path.join("artifact", "embedded_code_agent.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Embedded Code Agent Log\n")

    # Locate embedded spec JSON
    embedded_spec_path = _find_existing_json_path(
        state,
        candidates=[
            "embedded_spec_json_path",
            "embedded_spec_path",
            "code_spec_json",
            "spec_json_path",
            "embedded_spec_file",
        ],
    )

    embedded_spec: Optional[Dict[str, Any]] = None
    if embedded_spec_path:
        embedded_spec = _load_json(embedded_spec_path)
        _log(log_path, f"Loaded embedded spec from file: {embedded_spec_path}")
    elif isinstance(state.get("embedded_spec_json"), dict):
        embedded_spec = state["embedded_spec_json"]
        _log(log_path, "Loaded embedded spec from state['embedded_spec_json']")
    else:
        _log(log_path, "ERROR: Could not locate embedded spec JSON (file or state).")

        # Always upload the log so UI shows what happened
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name="Embedded Code Agent",
            subdir="embedded",
            filename="embedded_code_agent.log",
            content=open(log_path, "r", encoding="utf-8").read(),
        )

        state["embedded_code_error"] = "missing_embedded_spec"
        return state

    # Render firmware
    cpp = _render_cpp(embedded_spec)

    # Optional LLM polish (still generic)
    cpp = _maybe_llm_improve_cpp(embedded_spec, cpp, log_path)

    # Upload artifacts
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

    # Record paths for downstream agents / UI
    state["embedded_code_path"] = os.path.join(workflow_dir, "embedded", "main.cpp")
    state["embedded_code_artifact"] = f"{workflow_dir}/embedded/main.cpp"
    return state
