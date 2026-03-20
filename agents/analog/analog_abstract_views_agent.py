import json
import os
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load
import logging
logger = logging.getLogger("chiploop")

def _get_module_name(spec: dict) -> str:
    return (
        spec.get("module_name")
        or spec.get("block_name")
        or (spec.get("block") or {}).get("name")
        or "analog_macro"
    )


def _get_ports(spec: dict) -> list:
    ports = spec.get("ports")
    if isinstance(ports, list) and ports:
        return ports

    interfaces = spec.get("interfaces")
    if isinstance(interfaces, list) and interfaces:
        norm = []
        for p in interfaces:
            if isinstance(p, dict):
                norm.append(
                    {
                        "name": p.get("name", "sig"),
                        "direction": p.get("direction", "input"),
                        "width": int(p.get("width", 1) or 1),
                    }
                )
        return norm

    return []


def _get_clock_period_ns(spec: dict) -> float:
    behavioral = spec.get("behavioral_contract") or {}
    for key in ("clock_period_ns", "clk_period_ns", "period_ns"):
        v = behavioral.get(key)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass

    sample_rate = (
        spec.get("sampling_rate_hz")
        or spec.get("sampling_rate")
        or behavioral.get("sampling_rate_hz")
        or behavioral.get("sampling_rate")
    )
    if sample_rate is not None:
        try:
            sr = float(sample_rate)
            if sr > 0:
                return 1.0e9 / sr
        except Exception:
            pass

    return 1000.0  # default 1 MHz => 1000 ns


def _lef_direction(direction: str) -> str:
    d = (direction or "INPUT").strip().upper()
    if d == "INPUT":
        return "INPUT"
    if d == "OUTPUT":
        return "OUTPUT"
    if d == "INOUT":
        return "INOUT"
    return "INPUT"


def _lib_direction(direction: str) -> str:
    d = (direction or "input").strip().lower()
    if d == "input":
        return "input"
    if d == "output":
        return "output"
    if d == "inout":
        return "inout"
    return "input"


def _is_clock_port(name: str) -> bool:
    n = (name or "").lower()
    return "clk" in n or n.endswith("_clock") or n == "clock"


def _is_reset_port(name: str) -> bool:
    n = (name or "").lower()
    return "rst" in n or "reset" in n


def _bus_pin_names(name: str, width: int) -> list:
    if width <= 1:
        return [name]
    return [f"{name}[{i}]" for i in range(width)]


def _fallback_lef(spec: dict) -> str:

  
    module_name = _get_module_name(spec)
    ports = _get_ports(spec)

    lines = [
        'VERSION 5.8 ;',
        'BUSBITCHARS "[]" ;',
        'DIVIDERCHAR "/" ;',
        "",
        f"MACRO {module_name}",
        "  CLASS BLOCK ;",
        "  ORIGIN 0 0 ;",
        "  SIZE 100 BY 100 ;",
        "  SYMMETRY X Y ;",
        "  SITE CoreSite ;",
        "",
        "  PIN VDD",
        "    DIRECTION INOUT ;",
        "    USE POWER ;",
        "    PORT",
        "      LAYER M1 ;",
        "      RECT 0 0 1 1 ;",
        "    END",
        "  END VDD",
        "",
        "  PIN VSS",
        "    DIRECTION INOUT ;",
        "    USE GROUND ;",
        "    PORT",
        "      LAYER M1 ;",
        "      RECT 0 2 1 3 ;",
        "    END",
        "  END VSS",
        "",
    ]

    rect_y = 10
    for p in ports:
        pname = p.get("name", "sig")
        pdir = _lef_direction(p.get("direction", "INPUT"))
        width = int(p.get("width", 1) or 1)

        for bit_name in _bus_pin_names(pname, width):
            lines.extend(
                [
                    f"  PIN {bit_name}",
                    f"    DIRECTION {pdir} ;",
                    "    USE SIGNAL ;",
                    "    PORT",
                    "      LAYER M1 ;",
                    f"      RECT 0 {rect_y} 1 {rect_y + 1} ;",
                    "    END",
                    f"  END {bit_name}",
                    "",
                ]
            )
            rect_y += 2

    lines.extend([f"END {module_name}", "END LIBRARY", ""])
    return "\n".join(lines)


def _build_lib_stub(spec: dict) -> str:
    module_name = _get_module_name(spec)
    ports = _get_ports(spec)
    if not ports:
        return ""

    clk_ports = [p for p in ports if _is_clock_port(p.get("name", ""))]
    if not clk_ports:
        return ""

    clk_name = clk_ports[0].get("name", "clk")
    period_ns = _get_clock_period_ns(spec)
    setup_hold_ns = round(period_ns * 0.20, 3)
    clk2q_ns = round(period_ns * 0.40, 3)

    lines = [
        f"library ({module_name}_lib) {{",
        "  delay_model : table_lookup ;",
        "  time_unit : \"1ns\" ;",
        "  voltage_unit : \"1V\" ;",
        "  current_unit : \"1mA\" ;",
        "  capacitive_load_unit(1,pf) ;",
        "  leakage_power_unit : \"1nW\" ;",
        "",
        f"  cell ({module_name}) {{",
        "    area : 100.0 ;",
        "",
    ]

    lines.extend([
        "    pg_pin (VDD) {",
        "      pg_type : primary_power ;",
        "      voltage_name : VDD ;",
        "    }",
        "",
        "    pg_pin (VSS) {",
        "      pg_type : primary_ground ;",
        "      voltage_name : VSS ;",
        "    }",
        "",
    ])

    for p in ports:
        pname = p.get("name", "sig")
        pdir = _lib_direction(p.get("direction", "input"))
        lines.extend(
            [
                f"    pin ({pname}) {{",
                f"      direction : {pdir} ;",
            ]
        )

        if _is_clock_port(pname):
            lines.append("      clock : true ;")

        if pdir == "output":
            lines.extend(
                [
                    "      function : \"1\" ;",
                    "      timing () {",
                    f"        related_pin : \"{clk_name}\" ;",
                    "        timing_type : rising_edge ;",
                    "        cell_rise(delay_template_1x1) {",
                    '          index_1("0.100");',
                    '          index_2("0.100");',
                    f'          values("{clk2q_ns}");',
                    "        }",
                    "        cell_fall(delay_template_1x1) {",
                    '          index_1("0.100");',
                    '          index_2("0.100");',
                    f'          values("{clk2q_ns}");',
                    "        }",
                    "        rise_transition(delay_template_1x1) {",
                    '          index_1("0.100");',
                    '          index_2("0.100");',
                    f'          values("{clk2q_ns}");',
                    "        }",
                    "        fall_transition(delay_template_1x1) {",
                    '          index_1("0.100");',
                    '          index_2("0.100");',
                    f'          values("{clk2q_ns}");',
                    "        }",
                    "      }",
                ]
            )

        if pdir == "input" and not _is_clock_port(pname) and not _is_reset_port(pname):
            lines.extend(
                [
                    "      timing () {",
                    f"        related_pin : \"{clk_name}\" ;",
                    "        timing_type : setup_rising ;",
                    "        rise_constraint(constraint_template_1x1) {",
                    '          index_1("0.100");',
                    '          index_2("0.100");',
                    f'          values("{setup_hold_ns}");',
                    "        }",
                    "        fall_constraint(constraint_template_1x1) {",
                    '          index_1("0.100");',
                    '          index_2("0.100");',
                    f'          values("{setup_hold_ns}");',
                    "        }",
                    "      }",
                    "      timing () {",
                    f"        related_pin : \"{clk_name}\" ;",
                    "        timing_type : hold_rising ;",
                    "        rise_constraint(constraint_template_1x1) {",
                    '          index_1("0.100");',
                    '          index_2("0.100");',
                    f'          values("{setup_hold_ns}");',
                    "        }",
                    "        fall_constraint(constraint_template_1x1) {",
                    '          index_1("0.100");',
                    '          index_2("0.100");',
                    f'          values("{setup_hold_ns}");',
                    "        }",
                    "      }",
                ]
            )

        lines.extend(["    }", ""])

    lines.extend(["  }", "}", ""])
    return "\n".join(lines)

def run_agent(state: dict) -> dict:
    agent_name = "Analog Abstract Views Agent"
    workflow_id = state.get("workflow_id")
    workflow_dir = state.get("workflow_dir", ".")
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    if not workflow_id or not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing workflow_id or analog_spec"
        return state


    module_name = _get_module_name(spec)
    ports = _get_ports(spec)

    logger.info(f"[{agent_name}] START")
    logger.info(f"[{agent_name}] workflow_id={workflow_id}")
    logger.info(f"[{agent_name}] preview_only={preview_only}")
    logger.info(f"[{agent_name}] spec keys={list(spec.keys())}")
    logger.info(f"[{agent_name}] module_name={module_name}")
    logger.info(f"[{agent_name}] num_ports={len(ports)}")

    module_name = spec.get("module_name") or spec.get("block_name") or "analog_macro"
    ports = spec.get("ports", [])

    prompt = f"""
You are creating integration abstracts for an analog macro.

Using this spec:
{json.dumps(spec, indent=2)}

Return ONLY valid JSON with exactly these keys:
{{
  "lef": "...",
  "lib_stub": "...",
  "integration_notes_md": "..."
}}

Strict rules:
- Do NOT return any extra keys
- Do NOT split content across continuation fields
- Entire LEF must be contained in "lef"
- Entire LIB must be contained in "lib_stub"
- Macro name in LEF must be exactly: {module_name}
- LEF must include:
  - VERSION 5.8 ;
  - BUSBITCHARS "[]" ;
  - DIVIDERCHAR "/" ;
  - MACRO {module_name}
  - one PIN block for every port in spec
  - END {module_name}
  - END LIBRARY
- Use only generic M1 rectangles
- Keep SIZE placeholder legal
- Pin names must exactly match spec ports
- DIRECTION must match spec ports
- Entire LIB must be contained in "lib_stub"
- LIB cell name must be exactly: {module_name}
- LIB must include pg_pin blocks for VDD and VSS
- LIB must include pin entries for every signal port in spec
- Treat the first clock-like pin as the related clock
- For every input pin except clock/reset:
  - setup = 10% of clock period
  - hold = 10% of clock period
  - use timing_type : setup_rising and hold_rising
- For every output pin:
  - related_pin must be the clock pin
  - clk->q delay LUT value = 30% of clock period
  - include cell_rise / cell_fall
- Do not return descriptive placeholder timing only
- Return a Liberty stub that is structurally timing-usable
- No prose outside JSON
"""
    logger.info(f"[{agent_name}] calling LLM for abstract views...")
    out = llm_text(prompt)
    logger.info(f"[{agent_name}] raw LLM output length={len(out) if out else 0}")
    obj = safe_json_load(out)

    if not isinstance(obj, dict):
      logger.warning(f"[{agent_name}] LLM output not valid JSON → using fallbacks")
    else:
      logger.info(f"[{agent_name}] LLM JSON keys={list(obj.keys())}")

    module_name = _get_module_name(spec)
    ports = _get_ports(spec)
    lef_filename = f"{module_name}.lef"
    lib_filename = f"{module_name}.lib"
    notes_filename = f"{module_name}_notes.md"

    lef = (obj.get("lef") or "").strip() if isinstance(obj, dict) else ""
    lib_stub = (obj.get("lib_stub") or "").strip() if isinstance(obj, dict) else ""
    notes = (obj.get("integration_notes_md") or "").strip() if isinstance(obj, dict) else ""

    raw_debug_path = os.path.join(workflow_dir, "analog", "abstract", f"{module_name}_abstract_llm_raw.txt")
    json_debug_path = os.path.join(workflow_dir, "analog", "abstract", f"{module_name}_abstract_llm_json.json")
    lef_debug_path = os.path.join(workflow_dir, "analog", "abstract", f"{module_name}_llm_lef_raw.lef")

    os.makedirs(os.path.dirname(raw_debug_path), exist_ok=True)

    with open(raw_debug_path, "w", encoding="utf-8") as f:
        f.write(out or "")

    with open(json_debug_path, "w", encoding="utf-8") as f:
        json.dump(obj if isinstance(obj, dict) else {"raw_text": out}, f, indent=2)

    with open(lef_debug_path, "w", encoding="utf-8") as f:
        f.write((obj.get("lef") or "") if isinstance(obj, dict) else "")

    logger.info(f"[{agent_name}] raw LLM text saved: {raw_debug_path}")
    logger.info(f"[{agent_name}] parsed JSON saved: {json_debug_path}")
    logger.info(f"[{agent_name}] raw LEF field saved: {lef_debug_path}")

    if not lef:
        logger.warning(f"[{agent_name}] LEF missing from LLM → using fallback")
        lef = _fallback_lef(spec)

    lef_issues = []
    if "MACRO" not in lef:
        lef_issues.append("missing MACRO")
    if f"MACRO {module_name}" not in lef:
        lef_issues.append(f"missing exact macro name MACRO {module_name}")
    if f"END {module_name}" not in lef:
        lef_issues.append(f"missing END {module_name}")
    if "END LIBRARY" not in lef:
        lef_issues.append("missing END LIBRARY")

    if lef_issues:
        logger.warning(f"[{agent_name}] LEF invalid: {lef_issues} → regenerating fallback")
        logger.info(f"[{agent_name}] rejected LLM LEF preview:\n{lef[:1000]}")
        lef = _fallback_lef(spec)
        logger.info(f"[{agent_name}] fallback LEF generated, size={len(lef)}")

    
    if not lib_stub:
        logger.warning(f"[{agent_name}] LIB missing → building stub")
        lib_stub = _build_lib_stub(spec)

    lib_issues = []
    if "cell (" not in lib_stub:
        lib_issues.append("missing cell block")
    if "pg_pin (" not in lib_stub:
        lib_issues.append("missing pg_pin")
    if "related_pin" not in lib_stub:
        lib_issues.append("missing related_pin")
    if "setup_rising" not in lib_stub:
        lib_issues.append("missing setup_rising")
    if "hold_rising" not in lib_stub:
        lib_issues.append("missing hold_rising")
    if "cell_rise" not in lib_stub:
        lib_issues.append("missing cell_rise")
    if "cell_fall" not in lib_stub:
        lib_issues.append("missing cell_fall")

    if lib_issues:
        logger.warning(f"[{agent_name}] LIB invalid: {lib_issues} → regenerating deterministic LIB stub")
        logger.info(f"[{agent_name}] rejected LLM LIB preview:\n{lib_stub[:1200]}")
        lib_stub = _build_lib_stub(spec)    

    if not notes:
        logger.warning(f"[{agent_name}] notes missing → generating default")
        period_ns = _get_clock_period_ns(spec)
        notes = f"""# Integration Notes

- module_name: {module_name}
- lef_file: {lef_filename}
- lib_file: {lib_filename}
- timing_basis_clock_period_ns: {period_ns}
- setup_constraint_ns: {round(period_ns * 0.20, 3)}
- hold_constraint_ns: {round(period_ns * 0.20, 3)}
- clk_to_q_ns: {round(period_ns * 0.40, 3)}
- note: Module-scoped analog abstract view artifact set
"""
    logger.info(f"[{agent_name}] final LEF size={len(lef)}")
    logger.info(f"[{agent_name}] final LIB size={len(lib_stub)}")
    logger.info(f"[{agent_name}] final notes size={len(notes)}")
    if not preview_only:
        logger.info(f"[{agent_name}] saving artifacts...")
        save_text_artifact_and_record(workflow_id, agent_name, "analog/abstract", lef_filename, lef)
        save_text_artifact_and_record(workflow_id, agent_name, "analog/abstract", lib_filename, lib_stub or "")
        save_text_artifact_and_record(workflow_id, agent_name, "analog/abstract", notes_filename, notes)
        logger.info(f"[{agent_name}] saved:")
        logger.info(f"  - {lef_filename}")
        logger.info(f"  - {lib_filename}")
        logger.info(f"  - {notes_filename}")

    state["analog_abstract_dir"] = "analog/abstract"
    state["analog_macro_module"] = module_name
    state["analog_macro_lef"] = f"analog/abstract/{lef_filename}"
    state["analog_macro_lib"] = f"analog/abstract/{lib_filename}"
    state["analog_integration_notes"] = f"analog/abstract/{notes_filename}"

    


    return state