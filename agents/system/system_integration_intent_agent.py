import os
import json
import datetime
import requests
from typing import Dict

from portkey_ai import Portkey
from openai import OpenAI
from utils.artifact_utils import save_text_artifact_and_record

# ---------------------------------------------------------------------
# System Loop: Integration Intent Agent
# - Generates a strict JSON integration manifest for SoC top assembly.
# - Designed to avoid touching digital_integration_intent_agent.py.
# ---------------------------------------------------------------------

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY) if PORTKEY_API_KEY else None
client_openai = OpenAI()


def _now() -> str:
    return datetime.datetime.now().isoformat()


def _clean_llm_output_to_json_text(raw: str) -> str:
    if not raw:
        return ""
    raw = (
        raw.replace("```json", "")
        .replace("```JSON", "")
        .replace("```", "")
        .strip()
    )

    # Keep only first JSON object or array.
    first_obj = raw.find("{")
    last_obj = raw.rfind("}")
    if first_obj != -1 and last_obj != -1 and last_obj > first_obj:
        return raw[first_obj:last_obj + 1].strip()

    first_arr = raw.find("[")
    last_arr = raw.rfind("]")
    if first_arr != -1 and last_arr != -1 and last_arr > first_arr:
        return raw[first_arr:last_arr + 1].strip()

    return raw.strip()


def _llm_generate(prompt: str, timeout_s: int = 600) -> str:
    """
    Matches existing repo style:
    - If USE_LOCAL_OLLAMA: call ollama
    - Else: Portkey streaming, fallback to ollama
    """
    out = ""

    try:
        if USE_LOCAL_OLLAMA:
            payload = {"model": "llama3", "prompt": prompt}
            r = requests.post(OLLAMA_URL, json=payload, timeout=timeout_s)
            r.raise_for_status()
            data = r.json()
            out = data.get("response", "") or ""
        else:
            if client_portkey is None:
                raise RuntimeError("PORTKEY_API_KEY not set and USE_LOCAL_OLLAMA=false")

            # Portkey passthrough to OpenAI
            completion = client_portkey.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                stream=True,
                timeout=timeout_s,
            )
            for chunk in completion:
                delta = getattr(chunk.choices[0].delta, "content", None)
                if delta:
                    out += delta
    except Exception as e:
        # Fallback to ollama if possible
        try:
            payload = {"model": "llama3", "prompt": prompt}
            r = requests.post(OLLAMA_URL, json=payload, timeout=timeout_s)
            r.raise_for_status()
            data = r.json()
            out = data.get("response", "") or out
        except Exception:
            out = out or f"{{\"error\":\"LLM generation failed: {e}\"}}"

    return out


def run_agent(state: dict) -> dict:
    agent_name = "System Integration Intent Agent"
    print("\n🧠 Running System Integration Intent Agent (SoC integration manifest).")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    # Inputs (accept a few common keys to reduce friction)
    integration_description = (
        state.get("system_integration_description")
        or state.get("soc_integration_description")
        or state.get("integration_description")
        or ""
    ).strip()

    # Top module base name (we will produce *_sim and *_phys)
    top_base = (state.get("top_module") or state.get("soc_top_name") or "soc_top").strip()

    # Signatures (best-effort)
    # digital/analog signature agents may store differently; accept multiple keys.

    digital_sigs = state.get("digital_rtl_signatures") or state.get("rtl_signatures") or {}

    if not digital_sigs:
        for root, _, files in os.walk(workflow_dir):
            if "rtl_signatures.json" in files:
                p = os.path.join(root, "rtl_signatures.json")
                try:
                    with open(p, "r", encoding="utf-8") as f:
                       digital_sigs = json.load(f)
                except Exception:
                    pass
                    
    analog_sigs = state.get("analog_rtl_signatures") or state.get("analog_signatures") or {}

    # Optional hints from upstream analog agents
    # If your analog behavioral model and macro stub use different module names, provide them here.
    analog_behavioral_module = (state.get("analog_behavioral_module") or "").strip()  # e.g., "adc_model"
    analog_macro_module = (state.get("analog_macro_module") or "").strip()            # e.g., "adc_macro"

    if not integration_description:
        state["status"] = "❌ No system_integration_description provided."
        return state

    # -----------------------------------------------------------------
    # Strict JSON schema for downstream assembly.
    # Includes variant module overrides so we can generate sim/phys tops.
    # -----------------------------------------------------------------
    digital_module = (
        state.get("digital_top_module")
        or state.get("digital_module_name")
        or "digital_block"
    ).strip()

    analog_phys_module = (
        analog_macro_module
        or state.get("analog_top_module")
        or state.get("analog_module_name")
        or "analog_block"
    ).strip()

    analog_sim_module = (
       analog_behavioral_module
       or analog_phys_module
    ).strip()

    schema = {
        "top": {
            "base_name": top_base,
            "sim_module": f"{top_base}_sim",
            "phys_module": f"{top_base}_phys",
            "notes": "Generic system integration manifest for digital + analog assembly."
        },
        "instances": [
            {"name": "u_digital", "module": digital_module},
            {"name": "u_analog", "module": analog_phys_module}
        ],
        "connections": [],
        "tieoffs": [],
        "variants": {
            "sim": {
                "module_overrides": {
                    "u_analog": analog_sim_module
                }
            },
            "phys": {
                "module_overrides": {
                    "u_analog": analog_phys_module
                }
            }
        }
    }

    # Provide module name hints if user supplied them
    # (this improves correctness; still keeps LLM flexibility)
    variant_hint = ""
    if analog_behavioral_module or analog_macro_module:
        variant_hint = f"""
ANALOG MODULE NAME HINTS:
- analog_behavioral_module: {analog_behavioral_module or "(not provided)"}
- analog_macro_module: {analog_macro_module or "(not provided)"}
Use these in variants.module_overrides for instance 'u_adc' if applicable.
""".strip()

    prompt = f"""
SYSTEM / SoC INTEGRATION DESCRIPTION:
{integration_description}

TOP BASE NAME:
{top_base}

DIGITAL RTL SIGNATURES (modules + ports):
{json.dumps(digital_sigs, indent=2)}

ANALOG RTL SIGNATURES (modules + ports, if available):
{json.dumps(analog_sigs, indent=2)}

{variant_hint}

---
You are a professional SoC integration engineer.

CONTEXT / CONSTRAINTS:
- Generate a generic system integration manifest for the blocks described in the provided signatures and integration description.
- Prefer a minimal top with the fewest required instances and explicit point-to-point connections.
- Keep sim/phys top port intent consistent. If analog sim/phys module names differ, use variants.module_overrides.
- Reuse identical port names across blocks whenever appropriate.
- Do not assume ADC-specific names unless they are present in the input signatures/description.


🔒 IMPORTANT OUTPUT FORMAT RULES
- DO NOT use markdown code fences (no ```json, no ```).
- DO NOT include explanations or extra text.
- ONLY output raw JSON.
- JSON must be 100% valid (parseable by json.loads).
- Use the schema exactly: top, instances, connections, tieoffs, variants.
- 'connections' must connect valid ports that exist in the provided signatures whenever possible.

TARGET JSON SCHEMA EXAMPLE:
{json.dumps(schema, indent=2)}

Now output JSON only.
""".strip()

    raw = _llm_generate(prompt)
    cleaned = _clean_llm_output_to_json_text(raw)

    if not cleaned:
        state["status"] = "❌ LLM returned empty output for system integration intent."
        return state

    try:
        intent = json.loads(cleaned)
    except Exception as e:
        # Save raw output for debugging
        try:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="system/integrate",
                filename="system_integration_intent_raw.txt",
                content=raw,
            )
        except Exception:
            pass
        state["status"] = f"❌ Failed to parse system integration intent JSON: {e}"
        return state

    if not isinstance(intent, dict):
        intent = {}

    intent.setdefault("top", {})
    intent.setdefault("instances", [])
    intent.setdefault("connections", [])
    intent.setdefault("tieoffs", [])
    intent.setdefault("variants", {})

    if isinstance(intent["top"], dict):
        intent["top"].setdefault("base_name", top_base)
        intent["top"].setdefault("sim_module", f"{top_base}_sim")
        intent["top"].setdefault("phys_module", f"{top_base}_phys")

    # Generic fallback if LLM under-specifies the manifest
    if not intent["instances"]:
        intent["instances"] = schema["instances"]

    if "sim" not in intent["variants"]:
        intent["variants"]["sim"] = schema["variants"]["sim"]
    if "phys" not in intent["variants"]:
        intent["variants"]["phys"] = schema["variants"]["phys"]

    # Persist artifact
    try:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="system/integration",
            filename="system_integration_intent.json",
            content=json.dumps(intent, indent=2),
        )
    except Exception as e:
        print(f"⚠️ Failed to upload system integration intent artifact: {e}")

    state["system_integration_intent"] = intent
    state["status"] = "✅ System integration intent generated"
    return state
