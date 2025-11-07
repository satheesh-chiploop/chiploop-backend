# backend/analyze/digital/missing_slot_detector.py
from typing import Dict, List, Any
import json, re
from utils.llm_utils import run_llm_fallback

_SLOT_PROMPT = """
You receive a structured DIGITAL spec (JSON). Identify the MINIMAL set of missing/ambiguous fields
the user must fill to make it production-usable. SHORT mode (<=5 items when possible).

Return ONLY a JSON array of slots with:
[
  { "path": "module.name", "ask": "What should the module be named?", "type": "string" },
  { "path": "clock_domains[0].frequency_mhz", "ask": "Frequency of core_clk (MHz)?", "type": "number" },
  { "path": "pdc_crossings[0].level_shifter", "ask": "Level shifter needed PD_CORE→PD_AON?", "type":"enum",
    "options":["required","not_required","unspecified"] }
]

Rules:
- Prefer essential semantics (clock freq/edge, reset type/polarity/targets, PD states, PDC mitigations).
- Include CDC/PDC only when relevant in the spec.
- Do not ask for already-specified or confident fields.
- Do not output commentary; JSON only.
"""
def _filter_confirmed_slots(slots, struct_spec):
    filtered = []
    for slot in slots:
        path = slot.get("path", "")
        parts = re.split(r'\.|\[|\]', path)
        parts = [p for p in parts if p not in ("", None)]
        
        cursor = struct_spec
        confirmed_cursor = None
        ok = True
        for p in parts:
            if p.isdigit():
                p = int(p)
                if not isinstance(cursor, list) or p >= len(cursor):
                    ok = False; break
                cursor = cursor[p]
                confirmed_cursor = cursor.get("_confirmed", {}) if isinstance(cursor, dict) else None
            else:
                if not isinstance(cursor, dict) or p not in cursor:
                    ok = False; break
                cursor = cursor[p]
                confirmed_cursor = cursor.get("_confirmed", {}) if isinstance(cursor, dict) else None
        
        # If `_confirmed` marks this field as confirmed → skip it
        if confirmed_cursor and parts[-1] in confirmed_cursor:
            continue
        
        filtered.append(slot)
    return filtered

def _safe_json_array(resp: str) -> List[Dict[str, Any]]:
    m = re.search(r"\[[\s\S]*\]", resp)
    if not m: return []
    try: return json.loads(m.group(0))
    except: return []

async def detect_missing_slots(struct_spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    resp = await run_llm_fallback(f"{_SLOT_PROMPT}\n\nSTRUCT:\n{json.dumps(struct_spec, ensure_ascii=False)}")
    slots = _safe_json_array(resp)
    return _filter_confirmed_slots(slots, struct_spec)

