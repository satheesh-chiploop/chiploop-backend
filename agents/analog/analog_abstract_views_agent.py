import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load


def _fallback_lef(spec: dict) -> str:
    blk = (spec.get("block") or {}).get("name") or "ANALOG_MACRO"
    return f"""\
VERSION 5.8 ;
BUSBITCHARS "[]" ;
DIVIDERCHAR "/" ;

MACRO {blk}
  CLASS BLOCK ;
  ORIGIN 0 0 ;
  SIZE 100 BY 100 ;
  SYMMETRY X Y ;
  SITE CoreSite ;

  # Pins are placeholders; update based on spec.interfaces
  PIN VDD
    DIRECTION INOUT ;
    USE POWER ;
    PORT
      LAYER M1 ;
      RECT 0 0 1 1 ;
    END
  END VDD

  PIN VSS
    DIRECTION INOUT ;
    USE GROUND ;
    PORT
      LAYER M1 ;
      RECT 0 2 1 3 ;
    END
  END VSS

END {blk}
END LIBRARY
"""


def run_agent(state: dict) -> dict:
    agent_name = "Analog Abstract Views Agent"
    workflow_id = state.get("workflow_id")
    preview_only = bool(state.get("preview_only"))

    spec = state.get("analog_spec") or {}
    if not workflow_id or not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Missing workflow_id or analog_spec"
        return state

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
- LEF should be minimal: MACRO, SIZE placeholder, PINs for supplies/digital/analog pins.
- LIB stub only if digital pins exist; else keep it as an empty string.
- Keep placeholders; no tech-specific layers beyond generic M1.
"""

    out = llm_text(prompt)
    obj = safe_json_load(out)

    lef = (obj.get("lef") or "").strip() if isinstance(obj, dict) else ""
    lib_stub = (obj.get("lib_stub") or "").strip() if isinstance(obj, dict) else ""
    notes = (obj.get("integration_notes_md") or "").strip() if isinstance(obj, dict) else ""

    if not lef:
        lef = _fallback_lef(spec)
    if not notes:
        notes = "# Integration Notes\n\n(TBD)\n"

    if not preview_only:
        # Canonical scaffold path
        save_text_artifact_and_record(workflow_id, agent_name, "analog/abstract", "macro.lef", lef)
        save_text_artifact_and_record(workflow_id, agent_name, "analog/abstract", "macro_stub.lib", lib_stub or "")
        save_text_artifact_and_record(workflow_id, agent_name, "analog/abstract", "integration_notes.md", notes)

        # Legacy mirror for compatibility
        save_text_artifact_and_record(workflow_id, agent_name, "analog/abstracts", "macro.lef", lef)
        save_text_artifact_and_record(workflow_id, agent_name, "analog/abstracts", "macro_stub.lib", lib_stub or "")
        save_text_artifact_and_record(workflow_id, agent_name, "analog/abstracts", "integration_notes.md", notes)

    return state