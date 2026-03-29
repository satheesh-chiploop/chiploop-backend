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
You are generating integration abstract views for an analog macro.

This output will be consumed by digital implementation tools.
The LEF and Liberty must be structurally valid, conservative, and synthesis-safe.
If the Liberty syntax or structure is wrong, Yosys/OpenLane synthesis will fail.

==================================================
INPUT SPEC
==================================================
{json.dumps(spec, indent=2)}

==================================================
OUTPUT FORMAT — MANDATORY
==================================================
Return ONLY valid JSON with exactly these 3 keys:

{{
  "lef": "...",
  "lib_stub": "...",
  "integration_notes_md": "..."
}}

Do NOT return any extra keys.
Do NOT wrap the JSON in markdown fences.
Do NOT include any prose outside the JSON object.

==================================================
GLOBAL NAMING RULES — MANDATORY
==================================================
- Macro/module name must be exactly: {module_name}
- LEF MACRO name must be exactly: {module_name}
- LIB cell name must be exactly: {module_name}
- Pin names must exactly match the spec port names after bus expansion
- Do NOT invent, rename, alias, group, shorten, or simplify any ports
- Do NOT add helper pins, test pins, scan pins, or internal-only pins
- Do NOT omit any spec port

==================================================
LEF RULES — MANDATORY
==================================================
Generate a valid LEF macro abstract.

The LEF must:
- start with:
  VERSION 5.8 ;
  BUSBITCHARS "[]" ;
  DIVIDERCHAR "/" ;
- contain:
  MACRO {module_name}
- contain:
  CLASS BLOCK ;
  ORIGIN 0 0 ;
  SIZE 100 BY 100 ;
  SYMMETRY X Y ;
  SITE CoreSite ;
- contain pg/supply pins:
  PIN VDD
    DIRECTION INOUT ;
    USE POWER ;
  PIN VSS
    DIRECTION INOUT ;
    USE GROUND ;
- include exactly one LEF PIN block for every signal pin after bus expansion
- use only simple generic M1 rectangles
- end with:
  END {module_name}
  END LIBRARY

LEF bus rule:
- Do NOT use a single LEF pin for a whole bus like adc_data[11:0]
- Expand every bus into scalar pins:
  adc_data[0], adc_data[1], ..., adc_data[11]

==================================================
LIBERTY RULES — MANDATORY
==================================================
Generate a valid Liberty timing stub for digital synthesis use.

The LIB must:
- contain exactly one:
  library ({module_name}_lib) {{ ... }}
- contain exactly one:
  cell ({module_name}) {{ ... }}
- include:
  delay_model : table_lookup ;
  time_unit : "1ns" ;
  voltage_unit : "1V" ;
  current_unit : "1mA" ;
  capacitive_load_unit(1,pf) ;
  leakage_power_unit : "1nW" ;
- include pg pins:
  pg_pin (VDD)
  pg_pin (VSS)

==================================================
PIN STRUCTURE RULES — CRITICAL
==================================================
1. EACH SIGNAL PIN MAY APPEAR EXACTLY ONCE
- One pin() block per signal pin name
- Never define the same pin twice
- Never create a second timing-only pin block for the same signal

2. EVERY pin() BLOCK MUST INCLUDE direction
- Every pin() must include exactly one valid direction:
  direction : input ;
  direction : output ;
  direction : inout ;
- Never emit a pin() block with timing only and no direction

3. ALL TIMING FOR A PIN MUST STAY INSIDE THAT SAME pin() BLOCK
- Do not split timing across multiple pin() blocks

4. EXACT PORT COVERAGE
- Every spec signal port must appear exactly once after bus expansion
- No missing bits
- No extra bits
- No duplicate bits

==================================================
BUS HANDLING RULES — CRITICAL
==================================================
For any port with width > 1:
- Do NOT emit:
  pin ( dac_code[11:0] ) {{ ... }}
- Instead expand scalar bits:
  pin ( dac_code[0] ) {{ ... }}
  pin ( dac_code[1] ) {{ ... }}
  ...
  pin ( dac_code[11] ) {{ ... }}

Same rule applies to LEF PIN names.

==================================================
CLOCK RULES — CRITICAL
==================================================
- Identify the first clock-like input pin:
  - prefer a name containing "clk"
  - otherwise a name ending with "_clock"
  - otherwise exactly "clock"
- That one pin is the ONLY clock pin
- Mark only that pin with:
  clock : true ;
- No other pin may have clock : true ;
- All timing arcs must reference this clock pin using:
  related_pin : "<clock_name>" ;
- Never use:
  related_pin : "" ;

==================================================
TIMING RULES — CRITICAL
==================================================
Use clock period derived from the spec.

Let:
- setup_ns = 10% of clock period
- hold_ns = 10% of clock period
- clk_to_q_ns = 30% of clock period

A) CLOCK PIN
- clock pin must have direction : input ;
- clock pin may have:
  clock : true ;
- Do not add setup/hold timing blocks to the clock pin

B) RESET PINS
- reset-like pins are pins whose name contains "rst" or "reset"
- reset pins must have direction
- do NOT add setup/hold timing to reset pins unless explicitly required by spec

C) NON-CLOCK INPUT PINS
For every input pin except clock/reset:
- add exactly two timing() blocks:
  1) timing_type : setup_rising ;
  2) timing_type : hold_rising ;
- both must use:
  related_pin : "<clock_name>" ;
- setup/hold blocks must use:
  rise_constraint(constraint_template_1x1)
  fall_constraint(constraint_template_1x1)

D) OUTPUT PINS
For every output pin:
- add exactly one timing() block
- it must use:
  related_pin : "<clock_name>" ;
  timing_type : rising_edge ;
- it must include:
  cell_rise(delay_template_1x1)
  cell_fall(delay_template_1x1)
- do not use setup/hold timing on outputs

E) INOUT PINS
- keep direction : inout ;
- be conservative
- include no complex bidirectional timing unless clearly required by spec

==================================================
REQUIRED TEMPLATE DEFINITIONS — MANDATORY
==================================================
The library must define these templates before the cell block:

lu_table_template (delay_template_1x1) {{
  variable_1 : input_net_transition ;
  variable_2 : total_output_net_capacitance ;
  index_1("0.100");
  index_2("0.100");
}}

lu_table_template (constraint_template_1x1) {{
  variable_1 : constrained_pin_transition ;
  variable_2 : related_pin_transition ;
  index_1("0.100");
  index_2("0.100");
}}

==================================================
POWER PIN RULES
==================================================
Always include exactly these in LIB:
pg_pin (VDD) {{
  pg_type : primary_power ;
  voltage_name : VDD ;
}}

pg_pin (VSS) {{
  pg_type : primary_ground ;
  voltage_name : VSS ;
}}

Do not omit them.
Do not rename them.

==================================================
LIBERTY EXAMPLES — FOLLOW THESE EXACTLY
==================================================

1) ONE pin() BLOCK PER PORT
Bad:
pin ( clk ) {{
  direction : input ;
  clock : true ;
}}
pin ( clk ) {{
  timing () {{
    related_pin : "clk" ;
    timing_type : rising_edge ;
  }}
}}

Good:
pin ( clk ) {{
  direction : input ;
  clock : true ;
}}

Why:
- Never define the same pin twice.
- All properties for a pin must live inside one pin() block only.


2) NEVER CREATE timing-only pin() BLOCKS
Bad:
pin ( adc_done ) {{
  direction : output ;
}}
pin ( adc_done ) {{
  timing () {{
    related_pin : "clk" ;
    timing_type : rising_edge ;
    cell_rise(delay_template_1x1) {{
      index_1("0.100");
      index_2("0.100");
      values("3.000");
    }}
    cell_fall(delay_template_1x1) {{
      index_1("0.100");
      index_2("0.100");
      values("3.000");
    }}
  }}
}}

Good:
pin ( adc_done ) {{
  direction : output ;
  timing () {{
    related_pin : "clk" ;
    timing_type : rising_edge ;
    cell_rise(delay_template_1x1) {{
      index_1("0.100");
      index_2("0.100");
      values("3.000");
    }}
    cell_fall(delay_template_1x1) {{
      index_1("0.100");
      index_2("0.100");
      values("3.000");
    }}
  }}
}}

Why:
- Timing must be inside the same pin() block as the direction.


3) EVERY pin() MUST HAVE direction
Bad:
pin ( clk ) {{
  clock : true ;
}}
pin ( ready ) {{
  timing () {{
    related_pin : "clk" ;
    timing_type : rising_edge ;
  }}
}}

Good:
pin ( clk ) {{
  direction : input ;
  clock : true ;
}}
pin ( ready ) {{
  direction : output ;
  timing () {{
    related_pin : "clk" ;
    timing_type : rising_edge ;
    cell_rise(delay_template_1x1) {{
      index_1("0.100");
      index_2("0.100");
      values("3.000");
    }}
    cell_fall(delay_template_1x1) {{
      index_1("0.100");
      index_2("0.100");
      values("3.000");
    }}
  }}
}}

Why:
- A pin without direction is invalid for synthesis use.


4) DO NOT USE pin(bus[msb:lsb]) FORM
Bad:
pin ( dac_code[11:0] ) {{
  direction : input ;
}}
pin ( adc_data[11:0] ) {{
  direction : output ;
}}

Good:
pin ( dac_code[0] ) {{ direction : input ; }}
pin ( dac_code[1] ) {{ direction : input ; }}
pin ( dac_code[2] ) {{ direction : input ; }}
...
pin ( dac_code[11] ) {{ direction : input ; }}

pin ( adc_data[0] ) {{
  direction : output ;
  timing () {{
    related_pin : "clk" ;
    timing_type : rising_edge ;
    cell_rise(delay_template_1x1) {{
      index_1("0.100");
      index_2("0.100");
      values("3.000");
    }}
    cell_fall(delay_template_1x1) {{
      index_1("0.100");
      index_2("0.100");
      values("3.000");
    }}
  }}
}}

Why:
- Expand buses into scalar bit pins.
- Do not use [11:0] inside a single pin() name.


5) NEVER USE EMPTY related_pin
Bad:
timing () {{
  related_pin : "" ;
  timing_type : rising_edge ;
}}

Good:
timing () {{
  related_pin : "clk" ;
  timing_type : rising_edge ;
}}

Why:
- related_pin must reference the actual clock pin name.


6) ONLY ONE CLOCK PIN
Bad:
pin ( clk ) {{
  direction : input ;
  clock : true ;
}}
pin ( sample_clk ) {{
  direction : input ;
  clock : true ;
}}

Good:
pin ( clk ) {{
  direction : input ;
  clock : true ;
}}
pin ( sample_clk ) {{
  direction : input ;
}}

Why:
- Mark only one clock-like pin as clock:true.
- Use the first clock-like pin as the sole timing reference.


7) INPUT TIMING MUST USE setup_rising / hold_rising
Bad:
pin ( enable ) {{
  direction : input ;
  timing () {{
    related_pin : "clk" ;
    timing_type : rising_edge ;
  }}
}}

Good:
pin ( enable ) {{
  direction : input ;
  timing () {{
    related_pin : "clk" ;
    timing_type : setup_rising ;
    rise_constraint(constraint_template_1x1) {{
      index_1("0.100");
      index_2("0.100");
      values("1.000");
    }}
    fall_constraint(constraint_template_1x1) {{
      index_1("0.100");
      index_2("0.100");
      values("1.000");
    }}
  }}
  timing () {{
    related_pin : "clk" ;
    timing_type : hold_rising ;
    rise_constraint(constraint_template_1x1) {{
      index_1("0.100");
      index_2("0.100");
      values("1.000");
    }}
    fall_constraint(constraint_template_1x1) {{
      index_1("0.100");
      index_2("0.100");
      values("1.000");
    }}
  }}
}}

Why:
- Non-clock inputs need setup and hold constraints, not output timing style.


8) OUTPUT TIMING MUST USE rising_edge + cell_rise/cell_fall
Bad:
pin ( adc_done ) {{
  direction : output ;
  timing () {{
    related_pin : "clk" ;
    timing_type : setup_rising ;
    rise_constraint(constraint_template_1x1) {{
      index_1("0.100");
      index_2("0.100");
      values("1.000");
    }}
  }}
}}

Good:
pin ( adc_done ) {{
  direction : output ;
  timing () {{
    related_pin : "clk" ;
    timing_type : rising_edge ;
    cell_rise(delay_template_1x1) {{
      index_1("0.100");
      index_2("0.100");
      values("3.000");
    }}
    cell_fall(delay_template_1x1) {{
      index_1("0.100");
      index_2("0.100");
      values("3.000");
    }}
  }}
}}

Why:
- Outputs need clk-to-q style delay arcs, not setup/hold constraints.


9) DO NOT OMIT REQUIRED POWER PINS
Bad:
cell ( analog_subsystem ) {{
  area : 100.0 ;
  pin ( clk ) {{ direction : input ; clock : true ; }}
}}

Good:
cell ( analog_subsystem ) {{
  area : 100.0 ;

  pg_pin (VDD) {{
    pg_type : primary_power ;
    voltage_name : VDD ;
  }}

  pg_pin (VSS) {{
    pg_type : primary_ground ;
    voltage_name : VSS ;
  }}

  pin ( clk ) {{
    direction : input ;
    clock : true ;
  }}
}}

Why:
- Always include pg_pin(VDD) and pg_pin(VSS).


10) DO NOT INVENT OR RENAME PORTS
Bad:
pin ( analog_enable ) {{ direction : input ; }}
pin ( data_out ) {{ direction : output ; }}

Good:
If spec says:
- enable
- adc_data[0]

Then use exactly:
pin ( enable ) {{ direction : input ; }}
pin ( adc_data[0] ) {{ direction : output ; ... }}

Why:
- Pin names must exactly match the spec.
- Do not rename, group, alias, or simplify.


11) DO NOT SKIP BITS OF A BUS
Bad:
pin ( dac_code[0] ) {{ direction : input ; }}
pin ( dac_code[1] ) {{ direction : input ; }}
pin ( dac_code[11] ) {{ direction : input ; }}

Good:
pin ( dac_code[0] ) {{ direction : input ; }}
pin ( dac_code[1] ) {{ direction : input ; }}
pin ( dac_code[2] ) {{ direction : input ; }}
...
pin ( dac_code[11] ) {{ direction : input ; }}

Why:
- Every bus bit must be present exactly once.


12) DO NOT OUTPUT PROSE OR COMMENTS INSIDE LIBERTY
Bad:
library ( analog_subsystem_lib ) {{
  // This is a simple timing model for synthesis
  cell ( analog_subsystem ) {{
    ...
  }}
}}

Good:
library ( analog_subsystem_lib ) {{
  time_unit : "1ns" ;
  voltage_unit : "1V" ;
  ...
}}

Why:
- Output only valid Liberty syntax.
- No prose, no markdown, no explanatory comments.

==================================================
FINAL VALIDITY CHECKLIST — MANDATORY
==================================================
Before returning, verify all of the following are true:

- JSON has exactly 3 keys: lef, lib_stub, integration_notes_md
- LEF macro name == {module_name}
- LIB cell name == {module_name}
- pg_pin(VDD) present
- pg_pin(VSS) present
- every spec port is present exactly once after bus expansion
- no duplicate pin names
- no pin() block is missing direction
- no timing-only duplicate pin block exists
- exactly one clock pin has clock : true ;
- no empty related_pin
- no pin(name[msb:lsb]) syntax
- non-clock inputs use setup_rising and hold_rising
- outputs use rising_edge with cell_rise and cell_fall
- no extra prose outside JSON
- no comments inside LEF or LIB
- syntax must be conservative and tool-safe

If any checklist item fails, regenerate internally and return only a corrected final JSON.
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
    
    abstract_dir = os.path.join(workflow_dir, "analog", "abstract")
    os.makedirs(abstract_dir, exist_ok=True)

    lef_abs_path = os.path.join(abstract_dir, lef_filename)
    lib_abs_path = os.path.join(abstract_dir, lib_filename)
    notes_abs_path = os.path.join(abstract_dir, notes_filename)

    with open(lef_abs_path, "w", encoding="utf-8") as f:
        f.write(lef)

    with open(lib_abs_path, "w", encoding="utf-8") as f:
        f.write(lib_stub or "")

    with open(notes_abs_path, "w", encoding="utf-8") as f:
        f.write(notes)

    logger.info(f"[{agent_name}] canonical LEF written locally: {lef_abs_path}")
    logger.info(f"[{agent_name}] canonical LIB written locally: {lib_abs_path}")
    logger.info(f"[{agent_name}] canonical notes written locally: {notes_abs_path}")

    if not preview_only:
        logger.info(f"[{agent_name}] saving artifacts.")
        save_text_artifact_and_record(workflow_id, agent_name, "analog/abstract", lef_filename, lef)
        save_text_artifact_and_record(workflow_id, agent_name, "analog/abstract", lib_filename, lib_stub or "")
        save_text_artifact_and_record(workflow_id, agent_name, "analog/abstract", notes_filename, notes)
        logger.info(f"[{agent_name}] saved:")
        logger.info(f"  - {lef_filename}")
        logger.info(f"  - {lib_filename}")
        logger.info(f"  - {notes_filename}")

    state["analog_abstract_dir"] = "analog/abstract"
    state["analog_macro_module"] = module_name
    state["analog_macro_lef"] = lef_abs_path
    state["analog_macro_lib"] = lib_abs_path
    state["analog_integration_notes"] = notes_abs_path

    


    return state