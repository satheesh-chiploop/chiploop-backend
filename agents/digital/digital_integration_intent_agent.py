import os
import json
import datetime
import requests
from typing import Any, Dict

from portkey_ai import Portkey
from openai import OpenAI
from utils.artifact_utils import save_text_artifact_and_record

# ---------------------------------------------------------------------
# Setup (match existing agent style)
# ---------------------------------------------------------------------
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


def _now():
    return datetime.datetime.now().isoformat()


def _clean_llm_output_to_json_text(raw: str) -> str:
    if not raw:
        return ""

    # Remove markdown fences if any (agents do similar cleanup elsewhere)
    raw = (
        raw.replace("```json", "")
           .replace("```JSON", "")
           .replace("```", "")
           .strip()
    )

    # If model accidentally outputs extra text, try to keep only the first JSON object/array.
    # We do a simple bracket extraction: find first '{' and last '}'.
    first_obj = raw.find("{")
    last_obj = raw.rfind("}")
    if first_obj != -1 and last_obj != -1 and last_obj > first_obj:
        return raw[first_obj:last_obj + 1].strip()

    # fallback: maybe it returned an array
    first_arr = raw.find("[")
    last_arr = raw.rfind("]")
    if first_arr != -1 and last_arr != -1 and last_arr > first_arr:
        return raw[first_arr:last_arr + 1].strip()

    return raw.strip()


def _llm_generate(prompt: str, timeout_s: int = 600) -> str:
    """
    Match your existing pattern:
    - If USE_LOCAL_OLLAMA: call ollama
    - Else: Portkey streaming, fallback to ollama
    """
    out = ""

    try:
        if USE_LOCAL_OLLAMA:
            print("⚙️ Using local Ollama for integration intent.")
            payload = {"model": "llama3", "prompt": prompt}
            with requests.post(OLLAMA_URL, json=payload, stream=True, timeout=timeout_s) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        j = json.loads(line.decode())
                        if "response" in j:
                            out += j["response"]
                            print(j["response"], end="", flush=True)
                    except Exception:
                        continue
            return out

        print("🌐 Using Portkey backend for integration intent.")
        try:
            completion = client_portkey.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                stream=True,
            )
            for chunk in completion:
                if chunk and hasattr(chunk, "choices"):
                    delta = chunk.choices[0].delta.get("content", "")
                    if delta:
                        out += delta
                        print(delta, end="", flush=True)
            return out

        except Exception as e:
            print(f"⚠️ Portkey failed, falling back to Ollama: {e}")
            payload = {"model": "llama3", "prompt": prompt}
            with requests.post(OLLAMA_URL, json=payload, stream=True, timeout=timeout_s) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        j = json.loads(line.decode())
                        if "response" in j:
                            out += j["response"]
                    except Exception:
                        continue
            return out

    except Exception as e:
        print(f"⚠️ LLM generation failed: {e}")
        return ""


def run_agent(state: dict) -> dict:
    agent_name = "Digital Integration Intent Agent"
    print("\n🧠 Running Digital Integration Intent Agent (Model 2: text → intent JSON).")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    # Inputs (Model 2)
    integration_description = (state.get("integration_description") or "").strip()
    top_module = (state.get("top_module") or state.get("top_name") or "").strip()
    rtl_signatures = state.get("rtl_signatures")  # expected dict from RTL Signature Agent

    if not integration_description:
        state["status"] = "❌ No integration_description provided."
        return state

    if not rtl_signatures:
        # Allow fallback: if user didn't run signature agent, we can still proceed but with limited context.
        rtl_signatures = {}
        print("⚠️ rtl_signatures not found in state. Proceeding in limited context mode.")

    # -----------------------------------------------------------------
    # Prompt: strict JSON-only (same rule style as digital_spec_agent)
    # -----------------------------------------------------------------
    schema = {
        "instances": [
            {"name": "u_ipA", "module": "ipA"}
        ],
        "connections": [
            {"from": "u_ipA.out_valid", "to": "u_ipB.in_valid"}
        ],
        "tieoffs": [
            {"signal": "u_ipB.debug_en", "value": "1'b0"}
        ],
        "top": {
            "name": "top_soc",
            "notes": "Optional: clocks/resets exposure policy"
        }
    }

    prompt = f"""
USER INTEGRATION DESCRIPTION:
{integration_description}

TOP MODULE (if provided):
{top_module or "(not specified)"}

AVAILABLE RTL SIGNATURES (modules + ports):
{json.dumps(rtl_signatures, indent=2)}

---
You are a professional SoC integration engineer.

🔒 IMPORTANT OUTPUT FORMAT RULES
- DO NOT use markdown code fences (no ```json, no ```).
- DO NOT include explanations, headers, or extra text.
- ONLY output raw JSON.
- JSON must be 100% valid (parseable by json.loads in Python).
- Use the schema exactly: instances, connections, tieoffs, top.
- 'instances' must include every module to instantiate.
- 'connections' must only connect valid ports that exist in rtl_signatures whenever possible.
- When uncertain, make best-effort matches using common patterns (valid/ready/data, req/gnt, addr/wdata/rdata).
- Add tieoffs for likely-unused ports (debug_en, test_mode, scan_enable etc.) only if present in signatures.

TARGET JSON SCHEMA EXAMPLE:
{json.dumps(schema, indent=2)}

Now generate the JSON only.
""".strip()

    raw = _llm_generate(prompt)
    cleaned = _clean_llm_output_to_json_text(raw)

    if not cleaned:
        state["status"] = "❌ LLM returned empty output for integration intent."
        return state

    # Parse JSON
    try:
        intent = json.loads(cleaned)
    except Exception as e:
        # Save the raw output for debugging
        try:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="digital/integrate",
                filename="integration_intent_raw.txt",
                content=raw,
            )
        except Exception:
            pass

        state["status"] = f"❌ Failed to parse integration intent JSON: {e}"
        return state

    # Ensure top name exists
    if isinstance(intent, dict):
        intent.setdefault("top", {})
        if top_module and isinstance(intent["top"], dict):
            intent["top"].setdefault("name", top_module)

    # Persist artifact
    try:
        save_text_artifact_and_record(
            workflow_id=workflow_id,
            agent_name=agent_name,
            subdir="digital/integrate",
            filename="integration_intent.json",
            content=json.dumps(intent, indent=2),
        )
    except Exception as e:
        print(f"⚠️ Failed to upload integration intent artifact: {e}")

    # Update state
    state["integration_intent"] = intent
    state["status"] = "✅ Integration intent generated"
    return state