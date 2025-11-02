# backend/analyze/digital/spec_extractor_digital.py
from typing import Dict, Any
import json, re
from utils.llm_utils import run_llm_fallback

_EXTRACT_PROMPT = """
You are a DIGITAL hardware spec interpreter. Extract a structured spec in JSON.
MUST support multi-clock, multi-reset, multi-power domains and voltage (VD1: voltage implied by PD).
Detect CDC (clock→clock) and PDC (power→power) crossings. Prefer "unspecified" over guessing.

Return ONLY valid JSON with this shape:

{
  "module": { "name": null, "purpose": null },

  "clock_domains": [
    { "name": "core_clk", "edge": "rising|falling|unspecified", "frequency_mhz": null }
  ],

  "reset_domains": [
    { "name": "core_rst_n", "type": "sync|async|unspecified",
      "polarity": "active_high|active_low|unspecified",
      "targets": ["core_clk"] }
  ],

  "power_domains": [
    { "name": "PD_CORE", "rail_voltage": 0.85, "default_state": "ON",
      "states": ["ON","RETENTION","OFF"],
      "dvfs": [ { "voltage": 0.75, "frequency_mhz": 300 } ],
      "retention": { "enabled": false, "elements": [] },
      "isolation": { "enabled": false, "clamp_value": 0 }
    }
  ],

  "signals": [
    {
      "name": "clk_core", "dir": "input", "width": 1,
      "role": "clock|reset|enable|handshake|data|status|irq|other|unspecified",
      "clock_domain": "core_clk",
      "power_domain": "PD_CORE",
      "confidence": 0.0
    }
  ],

  "behavior": [
    {
      "clock_domain": "core_clk",
      "transitions": [
        { "when": "enable==1", "effect": "count := count + 1",
          "wrap_mode": "wrap|saturate|error|unspecified" }
      ]
    }
  ],

  "cdc_crossings": [
    { "source_clock": "core_clk", "dest_clock": "bus_clk",
      "signal": "count_sync", "method": "two_flop_sync|handshake|fifo|unspecified",
      "confidence": 0.0 }
  ],

  "pdc_crossings": [
    { "from_pd": "PD_CORE", "to_pd": "PD_AON", "signal": "count_sync",
      "level_shifter": "required|not_required|unspecified",
      "isolation": "required|not_required|unspecified",
      "retention_needed": "true|false|unspecified",
      "confidence": 0.0 }
  ],

  "verification": {
    "goals": [],
    "assertions": [],
    "coverage_intent": []
  },

  "notes": ""
}

Rules:
- DO NOT fabricate extra signals or domains.
- If a datum is not present in text, set null or "unspecified".
- Never output any commentary. JSON only.
"""

def _safe_json(resp: str) -> Dict[str, Any]:
    m = re.search(r"\{[\s\S]*\}", resp)
    if not m: return {}
    try: return json.loads(m.group(0))
    except: return {}

async def extract_structured_spec_digital(spec_text: str) -> Dict[str, Any]:
    prompt = f"{_EXTRACT_PROMPT}\n\nSPEC:\n{spec_text.strip()}"
    resp = await run_llm_fallback(prompt)
    data = _safe_json(resp)
    if not data:
        # minimal skeleton to keep pipeline alive
        data = {
            "module": {"name": None, "purpose": None},
            "clock_domains": [], "reset_domains": [], "power_domains": [],
            "signals": [], "behavior": [], "cdc_crossings": [],
            "pdc_crossings": [], "verification": {"goals": [], "assertions": [], "coverage_intent": []},
            "notes": ""
        }
    return data
