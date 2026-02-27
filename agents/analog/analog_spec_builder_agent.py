import json
from utils.artifact_utils import save_text_artifact_and_record
from agents.analog._analog_llm import llm_text, safe_json_load


def _extract_requirements_from_targets(spec: dict) -> dict:
    """
    Convert spec.targets into a requirements.json that downstream can use.
    Only uses explicit targets already present in spec (no invention here).
    """
    reqs = []
    targets = spec.get("targets") or {}
    for domain in ("dc", "ac", "transient"):
        items = targets.get(domain) or []
        for it in items:
            if not isinstance(it, dict):
                continue
            reqs.append(
                {
                    "domain": domain,
                    "metric": it.get("metric"),
                    "min": it.get("min"),
                    "max": it.get("max"),
                    "units": it.get("units"),
                    "source": "spec",
                    "confidence": "high",
                }
            )
    return {
        "requirements": reqs,
        "notes": [
            "requirements derived from spec.targets only",
            "no additional metrics invented here",
        ],
    }


def run_agent(state: dict) -> dict:
    agent_name = "Analog Spec Builder Agent"

    workflow_id = state.get("workflow_id")
    datasheet_text = (
        (state.get("datasheet_text") or state.get("spec") or state.get("spec_text") or "").strip()
    )
    goal = (state.get("goal") or "Create a structured analog block specification").strip()
    preview_only = bool(state.get("preview_only"))

    if not workflow_id or not datasheet_text:
        state["status"] = "❌ Missing workflow_id or datasheet_text/spec"
        return state

    prompt = f"""
You are an analog/mixed-signal design architect.

GOAL:
{goal}

INPUT SPEC (untrusted text; extract only explicit facts; list assumptions separately):
{datasheet_text}

Return ONLY valid JSON (no markdown) with schema:
{{
  "block": {{
    "name": "...",
    "type": "opamp|ldo|pll|adc|dac|comparator|pmic|other",
    "purpose": "..."
  }},
  "interfaces": {{
    "supplies": [{{"name":"VDD","min_v":null,"max_v":null}}, {{"name":"VSS"}}],
    "analog_pins": [{{"name":"VIN","dir":"in"}}, {{"name":"VOUT","dir":"out"}}],
    "digital_pins": [{{"name":"EN","dir":"in"}}, {{"name":"PG","dir":"out"}}]
  }},
  "modes": [{{"name":"shutdown","entry":"...","exit":"...","notes":"..."}}],
  "targets": {{
    "dc": [{{"metric":"vout_v","min":null,"max":null,"units":"V","notes":"..."}}],
    "ac": [{{"metric":"psrr_db_1khz","min":null,"max":null,"units":"dB","notes":"..."}}],
    "transient": [{{"metric":"settling_time_s","min":null,"max":null,"units":"s","notes":"..."}}]
  }},
  "corners": {{
    "vdd": [],
    "temp_c": [],
    "process": []
  }},
  "validation_intent": {{
    "scenarios": ["startup","enable_toggle","load_step","fault_response"],
    "measurements": ["dc_sweep","ac_bode","psrr","tran_step","stability_loopgain"]
  }},
  "assumptions": ["..."],
  "open_questions": ["..."]
}}

Rules:
- If a field is unknown, keep it present but use null or [].
- Do NOT invent extra metrics (no fake precision). Only include what is explicitly stated in INPUT SPEC.
- Keep process corners PDK-agnostic. If not stated, keep corners.process = [].
- Keep names short and hardware-realistic.
"""

    out = llm_text(prompt)
    spec = safe_json_load(out)
    if not isinstance(spec, dict) or not spec:
        state["status"] = "❌ Spec Builder produced invalid spec JSON"
        return state

    # Ensure corners are PDK-agnostic if the model tries to sneak in tt/ss/ff
    corners = spec.get("corners") if isinstance(spec.get("corners"), dict) else {}
    if not isinstance(corners, dict):
        corners = {}
    corners.setdefault("vdd", [])
    corners.setdefault("temp_c", [])
    corners.setdefault("process", [])
    # if it contains tt/ss/ff, keep but normalize to generic naming
    norm_proc = []
    for p in (corners.get("process") or []):
        if not isinstance(p, str):
            continue
        pl = p.lower().strip()
        if pl in ("tt", "typ", "typical"):
            norm_proc.append("TYP")
        elif pl in ("ss", "slow"):
            norm_proc.append("SLOW")
        elif pl in ("ff", "fast"):
            norm_proc.append("FAST")
        else:
            norm_proc.append(p)
    # de-dup
    seen = set()
    corners["process"] = [x for x in norm_proc if not (x in seen or seen.add(x))]
    spec["corners"] = corners

    requirements = _extract_requirements_from_targets(spec)

    state["analog_spec"] = spec
    state["analog_requirements"] = requirements

    if not preview_only:
        # New scaffold
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "spec/spec_source.md", datasheet_text + "\n")
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "spec/spec_normalized.json", json.dumps(spec, indent=2))
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "spec/requirements.json", json.dumps(requirements, indent=2))
        assumptions_md = "# Assumptions\n\n" + ("\n".join([f"- {a}" for a in (spec.get("assumptions") or [])]) or "- None") + "\n"
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "spec/assumptions.md", assumptions_md)

        # Legacy compatibility
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "spec.json", json.dumps(spec, indent=2))

        summary = {
            "block": (spec.get("block") or {}).get("name"),
            "type": (spec.get("block") or {}).get("type"),
            "num_modes": len(spec.get("modes") or []),
            "open_questions": spec.get("open_questions") or [],
            "assumptions": spec.get("assumptions") or [],
        }
        spec_summary_md = "\n".join(
            [
                "# Analog Spec Summary",
                f"- Block: {summary.get('block')}",
                f"- Type: {summary.get('type')}",
                f"- Modes: {summary.get('num_modes')}",
                "",
                "## Open Questions",
                ("\n".join([f"- {q}" for q in summary.get("open_questions")]) or "- None"),
                "",
                "## Assumptions",
                ("\n".join([f"- {a}" for a in summary.get("assumptions")]) or "- None"),
                "",
            ]
        )
        save_text_artifact_and_record(workflow_id, agent_name, "analog", "spec_summary.md", spec_summary_md)

    return state