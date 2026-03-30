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
        f"  FOREIGN {module_name} ;",
        "  CLASS BLOCK ;",
        "  ORIGIN 0 0 ;",
        "  SIZE 100.00 BY 100.00 ;",
        "  SYMMETRY X Y R90 ;",
        "  SITE unithd ;",
        "",
        "  PIN VPWR",
        "    DIRECTION INOUT ;",
        "    USE POWER ;",
        "    SHAPE ABUTMENT ;",
        "    PORT",
        "      LAYER met4 ;",
        "      RECT 0.00 0.00 2.00 100.00 ;",
        "      RECT 98.00 0.00 100.00 100.00 ;",
        "    END",
        "  END VPWR",
        "",
        "  PIN VGND",
        "    DIRECTION INOUT ;",
        "    USE GROUND ;",
        "    SHAPE ABUTMENT ;",
        "    PORT",
        "      LAYER met4 ;",
        "      RECT 4.00 0.00 6.00 100.00 ;",
        "      RECT 94.00 0.00 96.00 100.00 ;",
        "    END",
        "  END VGND",
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
                    "      LAYER met2 ;",
                    f"      RECT 0.00 {rect_y:.2f} 2.00 {rect_y + 1.5:.2f} ;",
                    "    END",
                    f"  END {bit_name}",
                    "",
                ]
            )
            rect_y += 4

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
        "    pg_pin (VPWR) {",
        "      pg_type : primary_power ;",
        "      voltage_name : VPWR ;",
        "    }",
        "",
        "    pg_pin (VGND) {",
        "      pg_type : primary_ground ;",
        "      voltage_name : VGND ;",
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

Think like a compiler and formatter, not like a creative writer.

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
Inside LEF and LIB:
- do NOT include comments
- do NOT include C-style comments like /* ... */
- do NOT include // comments
- do NOT include # comments
- do NOT include explanatory text, notes, section labels, or annotations
- output only legal LEF / Liberty syntax
Do NOT split content across continuation fields.
Entire LEF must be inside "lef".
Entire LIB must be inside "lib_stub".
Entire notes content must be inside "integration_notes_md".

==================================================
GLOBAL NAMING RULES — MANDATORY
==================================================
- Macro/module name must be exactly: {module_name}
- LEF MACRO name must be exactly: {module_name}
- LIB cell name must be exactly: {module_name}
- Pin names and bus names must exactly match the spec port names
- Do NOT invent, rename, alias, group, shorten, normalize, or simplify any ports
- Do NOT add helper pins, debug pins, scan pins, test pins, tieoff pins, or internal-only pins
- Do NOT omit any spec port
- Use only names derived directly from the input spec

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
  SYMMETRY X Y R90 ;
  SITE unithd ;

  Important:
- there must be a space before every semicolon
- for example, write `R90 ;` and never `R90;`

Use sky130-style routing layer names:
- met4 for macro power pins
- met2 for signal pins

Never use:
- M1, M2, Metal1, Metal2
- SITE CoreSite

Power pins must:
- use SHAPE ABUTMENT
- expose physically usable PG access for macro-level PDN hookup
- prefer met4 for macro PG pins
- use a valid LEF PORT block
- avoid tiny point-contact rectangles and avoid met1-only local rails

Use this style:

PIN VPWR
  DIRECTION INOUT ;
  USE POWER ;
  SHAPE ABUTMENT ;
  PORT
    LAYER met4 ;
    RECT 0.00 0.00 2.00 100.00 ;
    RECT 98.00 0.00 100.00 100.00 ;
  END
END VPWR

PIN VGND
  DIRECTION INOUT ;
  USE GROUND ;
  SHAPE ABUTMENT ;
  PORT
    LAYER met4 ;
    RECT 4.00 0.00 6.00 100.00 ;
    RECT 94.00 0.00 96.00 100.00 ;
  END
END VGND


Signal pins:
- must use met2
- must be simple rectangular shapes
- must use a valid LEF PORT block
- never place LAYER/RECT directly under PIN without PORT
  
- end with:
  END {module_name}
  END LIBRARY

LEF bus rule:
- LEF should still scalarize buses into physical pins:
  adc_data[0], adc_data[1], ..., adc_data[N]
- Do NOT collapse a whole bus into one LEF PIN like adc_data[11:0]

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
- define required templates before the cell block
- include pg pins:
  pg_pin (VPWR)
  pg_pin (VGND)

  The library must include these library-level threshold attributes exactly once:

input_threshold_pct_rise : 50.0 ;
input_threshold_pct_fall : 50.0 ;
output_threshold_pct_rise : 50.0 ;
output_threshold_pct_fall : 50.0 ;
slew_lower_threshold_pct_rise : 20.0 ;
slew_upper_threshold_pct_rise : 80.0 ;
slew_lower_threshold_pct_fall : 20.0 ;
slew_upper_threshold_pct_fall : 80.0 ;

For every output timing arc, include all four:
- cell_rise(delay_template_1x1)
- cell_fall(delay_template_1x1)
- rise_transition(delay_template_1x1)
- fall_transition(delay_template_1x1)

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
PIN AND BUS STRUCTURE RULES — CRITICAL
==================================================
1. SINGLE-BIT SIGNALS
- Every width=1 signal must appear exactly once as:
  pin (<name>) {{ ... }}
- Never define the same scalar pin twice
- Never create a timing-only duplicate pin block

2. MULTI-BIT SIGNALS
- Every width>1 signal must appear exactly once as:
  bus (<name>) {{ ... }}
- Every bus must reference a previously defined bus type:
  bus_type : <type_name> ;
- Every bus must include exactly one valid direction
- Do NOT represent a whole multi-bit port as:
  pin ( foo[11:0] ) {{ ... }}

3. OPTIONAL BUS MEMBERS
- If you include member pins for a bus, include them consistently and legally
- Never collapse all members into one fake pin like:
  pin ( foo[11:0] ) {{ ... }}
- If member pins are emitted, they must be the individual bits only:
  pin ( foo[0] ) {{ ... }}
  pin ( foo[1] ) {{ ... }}
  ...
- Never duplicate a bus bit
- Never omit an intermediate bit

4. EVERY scalar pin() MUST INCLUDE direction
- Every scalar pin() must include exactly one valid direction:
  direction : input ;
  direction : output ;
  direction : inout ;
- Never emit a scalar pin() block with timing only and no direction

5. ALL TIMING FOR A SIGNAL MUST STAY INSIDE THE SAME OWNING BLOCK
- Scalar signal timing stays inside its pin() block
- Bus timing stays inside its bus() block unless the format explicitly requires per-bit timing
- Never split one signal's semantics across duplicate blocks

==================================================
BUS TYPE RULES — CRITICAL
==================================================
For every width > 1 port, define a matching Liberty type.

Example form:
type (adc_data_bus_t) {{
  base_type : array ;
  data_type : bit ;
  bit_width : 12 ;
  bit_from : 11 ;
  bit_to : 0 ;
  downto : true ;
}}

Then define the bus:
bus (adc_data) {{
  bus_type : adc_data_bus_t ;
  direction : output ;
  ...
}}

Rules:
- Use one type per distinct bus shape when needed
- bit_width must exactly match the spec width
- bit_from / bit_to must match the intended numbering
- Use downto : true ; for descending buses like [11:0]
- Do not invent widths
- Do not use bus(...) for width=1 signals
- Do not use collapsed single-pin bus syntax

==================================================
CLOCK RULES — CRITICAL
==================================================
- Identify the first clock-like input pin:
  - prefer a name containing "clk"
  - otherwise a name ending with "_clock"
  - otherwise exactly "clock"
- That one signal is the ONLY clock reference
- If it is width=1, mark only that pin with:
  clock : true ;
- No other pin or bus may have clock : true ;
- All timing arcs must reference this clock signal using:
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
- do not add setup/hold timing blocks to the clock pin
- do not add output timing arcs to the clock pin

B) RESET PINS
- reset-like signals are names containing "rst" or "reset"
- reset pins must have direction
- do NOT add setup/hold timing to reset pins unless explicitly required by spec

C) NON-CLOCK INPUT SCALARS
For every width=1 input except clock/reset:
- add exactly two timing() blocks:
  1) timing_type : setup_rising ;
  2) timing_type : hold_rising ;
- both must use:
  related_pin : "<clock_name>" ;
- setup/hold blocks must use:
  rise_constraint(constraint_template_1x1)
  fall_constraint(constraint_template_1x1)

D) NON-CLOCK INPUT BUSES
For every width>1 input bus except reset buses:
- put conservative input timing on the bus block
- use:
  related_pin : "<clock_name>" ;
  timing_type : setup_rising ;
  timing_type : hold_rising ;
- do not collapse the bus into one fake scalar pin declaration

E) OUTPUT SCALARS
For every width=1 output:
- add exactly one timing() block
- it must use:
  related_pin : "<clock_name>" ;
  timing_type : rising_edge ;
- it must include:
  cell_rise(delay_template_1x1)
  cell_fall(delay_template_1x1)

F) OUTPUT BUSES
For every width>1 output bus:
- put conservative output timing on the bus block
- use:
  related_pin : "<clock_name>" ;
  timing_type : rising_edge ;
- include:
  cell_rise(delay_template_1x1)
  cell_fall(delay_template_1x1)

G) INOUT SIGNALS
- keep direction : inout ;
- be conservative
- include no complex bidirectional timing unless clearly required by spec

==================================================
POWER PIN RULES — MANDATORY
==================================================
Always include exactly these in LIB:

pg_pin (VPWR) {{
  pg_type : primary_power ;
  voltage_name : VPWR ;
}}

pg_pin (VGND) {{
  pg_type : primary_ground ;
  voltage_name : VGND ;
}}

Do not omit them.
Do not rename them.
Do not replace them with ordinary signal pins.

==================================================
CANONICAL GOOD EXAMPLE — FOLLOW THIS STYLE
==================================================
This example shows the required Liberty bus style.

library (example_macro_lib) {{
  delay_model : table_lookup ;
  time_unit : "1ns" ;
  voltage_unit : "1V" ;
  current_unit : "1mA" ;
  capacitive_load_unit(1,pf) ;
  leakage_power_unit : "1nW" ;

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

  type (dac_code_bus_t) {{
    base_type : array ;
    data_type : bit ;
    bit_width : 12 ;
    bit_from : 11 ;
    bit_to : 0 ;
    downto : true ;
  }}

  type (adc_data_bus_t) {{
    base_type : array ;
    data_type : bit ;
    bit_width : 12 ;
    bit_from : 11 ;
    bit_to : 0 ;
    downto : true ;
  }}

  cell (example_macro) {{
    area : 100.0 ;

    pg_pin (VPWR) {{
      pg_type : primary_power ;
      voltage_name : VPWR ;
    }}

    pg_pin (VGND) {{
      pg_type : primary_ground ;
      voltage_name : VGND ;
    }}

    pin (clk) {{
      direction : input ;
      clock : true ;
    }}

    pin (rst_n) {{
      direction : input ;
    }}

    pin (enable) {{
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

    bus (dac_code) {{
      bus_type : dac_code_bus_t ;
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

    bus (adc_data) {{
      bus_type : adc_data_bus_t ;
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

    pin (adc_done) {{
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
  }}
}}

==================================================
BAD EXAMPLES — NEVER DO THESE
==================================================

1) WRONG: collapsed whole-bus pin
pin ( adc_data[11:0] ) {{
  direction : output ;
}}

Why bad:
- a multi-bit bus must be represented with type(...) + bus(...), not a collapsed scalar pin

2) WRONG: duplicate scalar pin blocks
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

Why bad:
- same pin defined twice

3) WRONG: timing-only pin block
pin ( ready ) {{
  timing () {{
    related_pin : "clk" ;
    timing_type : rising_edge ;
  }}
}}

Why bad:
- every pin block must include direction

4) WRONG: empty related_pin
timing () {{
  related_pin : "" ;
  timing_type : rising_edge ;
}}

Why bad:
- related_pin must reference the actual clock pin

5) WRONG: more than one clock
pin ( clk ) {{
  direction : input ;
  clock : true ;
}}
pin ( sample_clk ) {{
  direction : input ;
  clock : true ;
}}

Why bad:
- only one signal may be the timing reference clock

6) WRONG:
SITE CoreSite ;

Why bad:
- not a valid sky130 site

7) WRONG:
LAYER M1 ;

Why bad:
- sky130 uses met1/met2 naming, not M1

8) WRONG:
PIN VPWR
  DIRECTION INOUT ;
  USE POWER ;
  SHAPE ABUTMENT ;
  LAYER met1 ;
  RECT 0 0 1 1 ;
END VPWR

Why bad:
- LEF geometry must be inside a PORT ... END block
- LAYER/RECT cannot appear directly under PIN

9) WRONG:
/* power pins */
PIN VPWR
  DIRECTION INOUT ;
  USE POWER ;
  SHAPE ABUTMENT ;
  PORT
    LAYER met1 ;
    RECT 0 0 1 1 ;
  END
END VPWR

Why bad:
- LEF output must not contain comments
- tokens like /* and */ can break OpenDB LEF parsing



==================================================
INTEGRATION NOTES RULES
==================================================
The "integration_notes_md" field must be short and factual.
It should include:
- module_name
- lef_file
- lib_file
- timing_basis_clock_period_ns
- setup_constraint_ns
- hold_constraint_ns
- clk_to_q_ns
- a brief note that this is a conservative abstract view artifact set

Do not include long prose.
Do not include speculative statements.

==================================================
FINAL VALIDITY CHECKLIST — MANDATORY
==================================================
Before returning, verify all of the following are true:

- JSON has exactly 3 keys: lef, lib_stub, integration_notes_md
- LEF macro name == {module_name}
- LIB cell name == {module_name}
- library ({module_name}_lib) exists exactly once
- cell ({module_name}) exists exactly once
- pg_pin(VPWR) present
- pg_pin(VGND) present
- required templates are defined before the cell block
- every width=1 spec port is represented exactly once as pin(...)
- every width>1 spec port is represented exactly once as bus(...)
- every width>1 spec port has a matching type(...)
- no duplicate scalar pin names
- no duplicate bus names
- no scalar pin() block is missing direction
- no timing-only duplicate pin block exists
- exactly one clock pin has clock : true ;
- no empty related_pin
- no collapsed pin(name[msb:lsb]) whole-bus syntax
- non-clock scalar inputs use setup_rising and hold_rising
- output scalars use rising_edge with cell_rise and cell_fall
- reset pins do not get accidental setup/hold timing
- no extra prose outside JSON
- every LEF PIN uses a PORT ... END block
- VPWR uses USE POWER
- VGND uses USE GROUND
- LEF contains no comment tokens such as /*, */, //, or #
- LIB contains no comment tokens such as /*, */, //, or #
- no comments inside LEF or LIB
- syntax is conservative and tool-safe


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

    required_tokens = [
        f"MACRO {module_name}",
        f"END {module_name}",
        "END LIBRARY",
        "SITE unithd",
        "PIN VPWR",
        "PIN VGND",
        "USE POWER",
        "USE GROUND",
        "PORT",
        "LAYER met1",
    ]

    for tok in required_tokens:
        if tok not in lef:
            lef_issues.append(f"missing {tok}")

    for bad in ["LAYER M1", "LAYER M2", "SITE CoreSite"]:
        if bad in lef:
            lef_issues.append(f"invalid sky130 token: {bad}")

    if "PIN VGND" in lef and "USE GROUND" not in lef:
        lef_issues.append("VGND must use USE GROUND")

    if "PIN VPWR" in lef and "USE POWER" not in lef:
        lef_issues.append("VPWR must use USE POWER")

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