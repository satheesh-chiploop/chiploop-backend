import os
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from portkey_ai import Portkey
from openai import OpenAI

from utils.artifact_utils import save_text_artifact_and_record

USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")

PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


def _load_json_if_exists(path: Optional[str]) -> Dict[str, Any]:
    if not path or not isinstance(path, str) or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _flatten_ports_from_digital_spec(digital_spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Your Digital Spec Agent often flattens hierarchy already.
    Expect:
      {"ports":[{"name":"clk","direction":"input","width":1}, ...]}
    If it's hierarchical, try to find top_module. Keep it best-effort.
    """
    if not isinstance(digital_spec, dict):
        return []

    # Flat
    if isinstance(digital_spec.get("ports"), list):
        return digital_spec.get("ports", [])

    # Hierarchy variants
    h = digital_spec.get("hierarchy")
    if isinstance(h, dict):
        top = h.get("top_module")
        if isinstance(top, dict) and isinstance(top.get("ports"), list):
            return top.get("ports", [])

    return []


def _infer_embedded_view_of_signals(digital_ports: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Convert digital RTL ports to embedded spec signals.
    - From embedded POV:
        * "inputs_from_digital": signals firmware reads (digital outputs)
        * "outputs_to_digital": signals firmware drives (digital inputs)
    """
    inputs_from_digital: List[Dict[str, Any]] = []
    outputs_to_digital: List[Dict[str, Any]] = []

    for p in digital_ports:
        name = p.get("name")
        direction = p.get("direction")
        width = p.get("width", 1)

        if not name or direction not in ("input", "output"):
            continue

        # Skip typical clocks/resets unless user explicitly wants them
        if name in ("clk", "clock", "reset", "reset_n", "rst", "rst_n"):
            continue

        # Normalize type
        try:
            w = int(width)
        except Exception:
            w = 1

        if w <= 1:
            stype = "bool"
        elif w <= 8:
            stype = "uint8"
        elif w <= 16:
            stype = "uint16"
        elif w <= 32:
            stype = "uint32"
        else:
            stype = f"bitvector_{w}"

        sig = {
            "name": str(name),
            "direction": direction,  # direction in RTL
            "width": w,
            "type": stype,
            "description": p.get("description") or f"Derived from digital port '{name}'",
        }

        # RTL output => embedded reads it
        if direction == "output":
            inputs_from_digital.append(sig)
        # RTL input => embedded drives it
        elif direction == "input":
            outputs_to_digital.append(sig)

    return inputs_from_digital, outputs_to_digital


def _pick_user_intent(state: Dict[str, Any]) -> str:
    """
    Prefer consistent keys across your workflow:
    - embedded_user_prompt
    - user_prompt
    - spec (sometimes you store a high-level request here)
    """
    for k in ("embedded_user_prompt", "user_prompt", "spec", "embedded_spec_prompt"):
        v = state.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "Generate a generic firmware specification that reads status signals from digital logic and drives control signals back, using a simple periodic control loop and logging."


def run_agent(state: dict) -> dict:
    print("\n🧠 Running Embedded Spec Agent (generic + system-aware)…")

    workflow_id = state.get("workflow_id", "embedded_default")
    workflow_dir = state.get("workflow_dir") or f"backend/workflows/{workflow_id}"
    os.makedirs(workflow_dir, exist_ok=True)

    embedded_dir = os.path.join(workflow_dir, "embedded")
    os.makedirs(embedded_dir, exist_ok=True)

    log_path = os.path.join(embedded_dir, "embedded_spec_agent.log")

    # Load digital spec (best-effort)
    digital_spec_path = state.get("spec_json") or state.get("digital_spec_json")
    digital_spec = _load_json_if_exists(digital_spec_path)
    digital_ports = _flatten_ports_from_digital_spec(digital_spec)
    inferred_inputs, inferred_outputs = _infer_embedded_view_of_signals(digital_ports)

    user_intent = _pick_user_intent(state)

    digital_hint = {
        "digital_spec_path": digital_spec_path,
        "inputs_from_digital": inferred_inputs,
        "outputs_to_digital": inferred_outputs,
    }

    prompt = f"""
You are an embedded firmware architect.

Create a GENERIC firmware specification JSON based on:
1) System intent from the user
2) Digital interface hint (ports inferred from RTL spec)

IMPORTANT:
- Keep it generic and reusable. Do NOT invent domain-specific signal names (e.g., "overheat_flag") unless they appear in the interface hint or user intent.
- Use the inferred signal lists when available.
- If you need example semantics, describe them in descriptions, not by renaming signals.

SYSTEM INTENT:
\"\"\"{user_intent}\"\"\"

DIGITAL INTERFACE HINT (inferred):
{json.dumps(digital_hint, indent=2)}

OUTPUT RULES (MANDATORY):
- Return ONLY valid JSON (no markdown, no backticks, no comments, no extra text).

Schema (strict fields; you may leave arrays empty if unknown):
{{
  "firmware_name": "string_snake_case",
  "description": "string",
  "target": {{
    "language": "cpp",
    "standard": "c++17",
    "runtime": "bare_metal",
    "os": "none"
  }},
  "timing": {{
    "control_period_ms": 100,
    "deadline_ms": 100
  }},
  "logging": {{
    "enabled": true,
    "interface": "log_printf",
    "level": "info"
  }},
  "signals": {{
    "inputs_from_digital": [
      {{
        "name": "string",
        "type": "bool|uint8|uint16|uint32|bitvector_N",
        "width": 1,
        "description": "string"
      }}
    ],
    "outputs_to_digital": [
      {{
        "name": "string",
        "type": "bool|uint8|uint16|uint32|bitvector_N",
        "width": 1,
        "description": "string",
        "default_value": 0
      }}
    ]
  }},
  "behavior": {{
    "mode": "polling",
    "rules": [
      {{
        "name": "rule_name",
        "when": "plain language condition based on inputs",
        "then": "plain language action affecting outputs and/or logging"
      }}
    ],
    "notes": "plain language"
  }}
}}

Guidelines:
- firmware_name should be short and generic (e.g., "embedded_control_loop").
- control_period_ms should be 50–1000 unless user intent suggests otherwise.
- If no signals are available, leave arrays empty but keep structure.
- Provide 2–5 generic rules grounded in the intent and available signals.
""".strip()

    raw = ""
    spec_json: Dict[str, Any] = {}
    error: Optional[str] = None

    try:
        if USE_LOCAL_OLLAMA:
            import requests
            resp = requests.post(OLLAMA_URL, json={"model": "llama3", "prompt": prompt}, timeout=600)
            resp.raise_for_status()
            raw = (resp.json().get("response") or "").strip()
        else:
            completion = client_portkey.chat.completions.create(
                model="@chiploop/gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                stream=False,
            )
            raw = (completion.choices[0].message.content or "").strip()

        # Strip accidental fences
        text = raw.strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1].strip() if len(parts) >= 2 else text

        spec_json = json.loads(text)

    except Exception as e:
        error = str(e)

        # Generic fallback (NO domain-specific signal names)
        spec_json = {
            "firmware_name": "embedded_control_loop",
            "description": "Generic firmware spec: periodic control loop reading digital status signals and driving control outputs with optional logging.",
            "target": {"language": "cpp", "standard": "c++17", "runtime": "bare_metal", "os": "none"},
            "timing": {"control_period_ms": 100, "deadline_ms": 100},
            "logging": {"enabled": True, "interface": "log_printf", "level": "info"},
            "signals": {
                "inputs_from_digital": inferred_inputs[:8],   # keep small
                "outputs_to_digital": [
                    dict(s, **{"default_value": 0}) for s in inferred_outputs[:8]
                ],
            },
            "behavior": {
                "mode": "polling",
                "rules": [
                    {
                        "name": "log_on_status_change",
                        "when": "any boolean input signal changes value (if boolean inputs exist)",
                        "then": "log an informational message with the signal name and new value",
                    },
                    {
                        "name": "safe_defaults",
                        "when": "on startup",
                        "then": "drive outputs_to_digital to their default_value",
                    },
                ],
                "notes": "Fallback used because LLM output was unavailable or invalid JSON.",
            },
        }

    # Persist spec
    firmware_name = spec_json.get("firmware_name") or "embedded_control_loop"
    spec_path = os.path.join(embedded_dir, f"{firmware_name}_embedded_spec.json")

    try:
        with open(spec_path, "w", encoding="utf-8") as f:
            json.dump(spec_json, f, indent=2)
    except Exception:
        # last resort
        with open(spec_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(spec_json))

    # Log
    try:
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"[{datetime.now()}] Embedded Spec Agent executed.\n")
            log.write(f"Digital spec path: {digital_spec_path}\n")
            log.write(f"User intent: {user_intent}\n")
            if error:
                log.write(f"WARNING: Used fallback due to error: {error}\n")
            log.write("Spec JSON:\n")
            log.write(json.dumps(spec_json, indent=2))
            log.write("\n\n")
    except Exception:
        pass

    # Upload artifacts + record
    try:
        agent_name = "Embedded Spec Agent"

        with open(spec_path, "r", encoding="utf-8") as f:
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="embedded",
                filename=os.path.basename(spec_path),
                content=f.read(),
            )

        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                save_text_artifact_and_record(
                    workflow_id=workflow_id,
                    agent_name=agent_name,
                    subdir="embedded",
                    filename="embedded_spec_agent.log",
                    content=f.read(),
                )

        print("🧩 Embedded Spec Agent artifacts uploaded successfully.")
    except Exception as e:
        print(f"⚠️ Embedded Spec Agent artifact upload failed: {e}")

    # Keys downstream agents expect
    state.update(
        {
            "status": "✅ Embedded Spec Generated",
            "spec_file": spec_path,               # used by Embedded Code Agent (compat)
            "embedded_spec_json": spec_path,      # additional common key
            "embedded_spec_path": spec_path,      # additional common key
            "workflow_dir": workflow_dir,
            "workflow_id": workflow_id,
        }
    )

    print(f"✅ Embedded spec saved to {spec_path}")
    return state
