# backend/analyze/digital/analyze_spec_digital.py
from typing import Dict, Any, List
from analyze.digital.spec_extractor_digital import extract_structured_spec_digital
from analyze.digital.missing_slot_detector import detect_missing_slots
from utils.llm_utils import run_llm_fallback
import json, re

def _coverage_from(struct_spec: Dict[str, Any], missing: List[Dict[str, Any]]) -> int:
    # Fixed-point heuristic: start high, penalize per missing slot (15 pts each) but cap at [10..100]
    score = 100 - 15 * len(missing)
    return max(10, min(100, score))

_SYNTH_PROMPT = """
You are producing a FINAL human-readable prompt ("spec freeze") for DIGITAL IP generation
from the following structured spec (JSON). Include:
- Module name/purpose
- Clock domains (name, edge, frequency if present)
- Reset domains (type/polarity, targets)
- Power domains (VD1 voltage, states, DVFS OPPs if any, isolation/retention)
- Behavior by clock domain
- CDC/PDC crossings with methods/mitigations
- Verification intent (brief)

Return plain text ONLY, no code.
"""

async def _synthesize_final_prompt(struct_spec: Dict[str, Any]) -> str:
    payload = json.dumps(struct_spec, ensure_ascii=False)
    resp = await run_llm_fallback(f"{_SYNTH_PROMPT}\n\nSTRUCT:\n{payload}")
    # Trim fencing if any:
    text = resp.strip()
    # remove backticks if present
    text = re.sub(r"^```[a-zA-Z]*|```$", "", text).strip()
    return text

def _apply_user_answers(struct_spec: Dict[str, Any], answers: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge user answers by JSONPath-like 'path' keys used in missing_slots.
    Supports simple paths like a.b[0].c
    """
    import copy
    out = copy.deepcopy(struct_spec)

    def set_by_path(obj, path, value):
        import re
        parts = re.findall(r"[^\.\[\]]+|\[\d+\]", path)
        cur = obj
        for i, p in enumerate(parts):
            if p.startswith('[') and p.endswith(']'):
                idx = int(p[1:-1])
                # ensure list size
                if not isinstance(cur, list):
                    return  # invalid path; ignore silently
                if idx >= len(cur):
                    # extend list with dicts
                    for _ in range(idx - len(cur) + 1):
                        cur.append({})
                if i == len(parts)-1:
                    cur[idx] = value
                else:
                    cur = cur[idx]
            else:
                # dict key
                if i == len(parts)-1:
                    cur[p] = value
                else:
                    if p not in cur or not isinstance(cur[p], (dict, list)):
                        cur[p] = {}
                    cur = cur[p]

    for path, val in answers.items():
        set_by_path(out, path, val)
    return out

async def analyze_spec_digital(goal: str, voice_summary: str, user_id: str) -> Dict[str, Any]:
    combined = (voice_summary or "") + "\n" + (goal or "")
    struct_draft = await extract_structured_spec_digital(combined)
    missing = await detect_missing_slots(struct_draft)
    coverage = _coverage_from(struct_draft, missing)

    return {
        "status": "ok",
        "engine": "digital_structured_v1",
        "structured_spec_draft": struct_draft,
        "missing": missing,                 # SHORT form inputs (<=5 ideally)
        "coverage": coverage,
        "ready_for_planning": len(missing) == 0
    }

# Finalization (called after user fills missing slots)
async def finalize_spec_digital(structured_spec_draft: Dict[str, Any],
                                answers: Dict[str, Any],
                                user_id: str) -> Dict[str, Any]:
    struct_final = _apply_user_answers(structured_spec_draft, answers)
    # recompute missing to ensure closure
    rem_missing = await detect_missing_slots(struct_final)
    coverage = _coverage_from(struct_final, rem_missing)
    ready = (len(rem_missing) == 0)

    final_prompt = await _synthesize_final_prompt(struct_final)

    # S1 policy: store ONLY final spec (if you add DB later, do it here)
    # save_final_structured_spec(user_id, struct_final, final_prompt)

    return {
        "status": "ok",
        "engine": "digital_structured_v1",
        "structured_spec_final": struct_final,
        "final_prompt": final_prompt,
        "coverage": coverage,
        "ready_for_planning": ready,
        "remaining_missing": rem_missing
    }
