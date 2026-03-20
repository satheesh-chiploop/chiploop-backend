import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load


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
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    if not workflow_id or not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing workflow_id or analog_spec"
        return state


        module_name = _get_module_name(spec)

    prompt = f"""
You are creating integration abstracts for an analog macro.

Using this spec:
{json.dumps(spec, indent=2)}

Return ONLY JSON:
{{
  "lef": "VERSION 5.8 ;\\nMACRO ...",
  "lib_stub": "library(...) {{ ... }}",
  "integration_notes_md": "# Integration Notes\\n..."
}}

Rules:
- Macro name in LEF and LIB cell name must be exactly: {module_name}
- LEF should be minimal: MACRO, SIZE placeholder, PINs for supplies/digital/analog pins
- Use generic M1 only; no tech-specific layers
- Generate a Liberty stub only if there is at least one clock-like port
- For Liberty timing:
  - setup = 20% of clock period
  - hold = 20% of clock period
  - clk->q delay LUT value = 40% of clock period
- Use a simple 1x1 LUT style
- Return ONLY valid JSON
"""

    out = llm_text(prompt)
    obj = safe_json_load(out)

    module_name = _get_module_name(spec)
    lef_filename = f"{module_name}.lef"
    lib_filename = f"{module_name}.lib"
    notes_filename = f"{module_name}_notes.md"

    lef = (obj.get("lef") or "").strip() if isinstance(obj, dict) else ""
    lib_stub = (obj.get("lib_stub") or "").strip() if isinstance(obj, dict) else ""
    notes = (obj.get("integration_notes_md") or "").strip() if isinstance(obj, dict) else ""

    if not lef:
        lef = _fallback_lef(spec)

    if "MACRO" not in lef or f"MACRO {module_name}" not in lef or "END LIBRARY" not in lef:
        lef = _fallback_lef(spec)

    if not lib_stub:
        lib_stub = _build_lib_stub(spec)

    if not notes:
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

    if not preview_only:
        save_text_artifact_and_record(workflow_id, agent_name, "analog/abstract", lef_filename, lef)
        save_text_artifact_and_record(workflow_id, agent_name, "analog/abstract", lib_filename, lib_stub or "")
        save_text_artifact_and_record(workflow_id, agent_name, "analog/abstract", notes_filename, notes)

    state["analog_abstract_dir"] = "analog/abstract"
    state["analog_macro_module"] = module_name
    state["analog_macro_lef"] = f"analog/abstract/{lef_filename}"
    state["analog_macro_lib"] = f"analog/abstract/{lib_filename}"
    state["analog_integration_notes"] = f"analog/abstract/{notes_filename}"

    


    return state