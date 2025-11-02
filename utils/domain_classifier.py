# backend/utils/domain_classifier.py
from typing import Dict, Any
import json, re

# If your helper name differs, adjust this import:
from utils.llm_utils import run_llm_fallback

_DOMAIN_PROMPT = """
Classify the following hardware design specification into one domain:
- DIGITAL (RTL, synchronous logic, FSM, bit widths, pipelines, testbench)
- ANALOG (voltages, currents, SPICE, gm/ro, bias, settling)
- EMBEDDED (firmware, MMIO registers, ISR, drivers, peripherals)
- MIXED (two or more domains are equally central)

Return ONLY valid JSON:
{
  "domain": "digital | analog | embedded | mixed",
  "confidence": { "digital": 0.xx, "analog": 0.xx, "embedded": 0.xx },
  "reason": "short justification"
}
Do not invent details. Confidence values should sum to ~1.0.
"""

async def detect_domain(spec_text: str) -> Dict[str, Any]:
    prompt = f"{_DOMAIN_PROMPT}\n\nSpec:\n{spec_text.strip()}"
    resp = await run_llm_fallback(prompt)

    # Extract the first JSON block from model output
    match = re.search(r"\{[\s\S]*\}", resp)
    if match:
        try:
            data = json.loads(match.group(0))
            # Basic sanity defaults
            dom = (data.get("domain") or "mixed").lower()
            conf = data.get("confidence") or {}
            if not isinstance(conf, dict):
                conf = {}
            # Normalize keys
            conf = {
                "digital": float(conf.get("digital", 0.0)),
                "analog": float(conf.get("analog", 0.0)),
                "embedded": float(conf.get("embedded", 0.0)),
            }
            return {
                "domain": dom if dom in {"digital","analog","embedded","mixed"} else "mixed",
                "confidence": conf,
                "reason": data.get("reason", ""),
            }
        except Exception:
            pass

    # Fallback
    return {
        "domain": "mixed",
        "confidence": {"digital": 0.34, "analog": 0.33, "embedded": 0.33},
        "reason": "Could not reliably classify.",
    }
