import os
import json
from datetime import datetime
from openai import OpenAI
from portkey_ai import Portkey

USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


def _load_digital_spec(digital_spec_path: str | None) -> dict:
    """Best-effort load of digital spec JSON (from Digital Spec Agent)."""
    if not digital_spec_path:
        return {}
    try:
        with open(digital_spec_path, "r", encoding="utf-8") as f:
            spec = json.load(f)
        return spec
    except Exception:
        return {}


def _infer_digital_io(digital_spec: dict) -> tuple[list[dict], list[dict]]:
    """
    Extract simple inputs/outputs from the flattened digital spec.

    We expect something like:
      {
        "name": "...",
        "ports": [
           {"name": "clk", "direction": "input", "width": 1, ...},
           ...
        ],
        ...
      }
    """
    ports = digital_spec.get("ports", [])
    inputs = []
    outputs = []

    for p in ports:
        name = p.get("name")
        direction = p.get("direction")
        width = p.get("width", 1)
        if not name or direction not in ("input", "output"):
            continue

        p_type = "bool" if str(width) == "1" else f"uint{width}_bus"
        entry = {
            "name": name,
            "type": p_type,
            "description": f"{direction} signal from digital design",
        }

        if direction == "input":
            # From embedded POV: these are things firmware might drive TO digital.
            outputs.append(entry)
        elif direction == "output":
            # From embedded POV: these are things firmware might read FROM digital.
            inputs.append(entry)

    return inputs, outputs


def run_agent(state: dict) -> dict:
    print("\n🧠 Running Embedded Spec Agent (system-aware)…")

    # --- Paths & workflow setup ---
    workflow_id = state.get("workflow_id", "embedded_default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    log_path = os.path.join(workflow_dir, "embedded_spec_agent.log")

    # --- Inputs ---
    user_prompt = state.get("user_prompt", "").strip()
    if not user_prompt:
        # Simple default if caller forgets to pass prompt
        user_prompt = (
            "Design firmware that monitors an overheat_flag coming from digital logic "
            "and prints an alert message when it is asserted."
        )

    digital_spec_path = state.get("digital_spec_json")
    digital_spec = _load_digital_spec(digital_spec_path)
    inferred_inputs, inferred_outputs = _infer_digital_io(digital_spec)

    # We put a compact hint of digital IO into the LLM prompt.
    digital_hint = {
        "inputs_from_digital": inferred_inputs,
        "outputs_to_digital": inferred_outputs,
    }

    # --- LLM prompt enforcing JSON-only output ---
    prompt = f"""
You are an embedded firmware architect.

Convert the following system description and digital interface hints into a structured
firmware specification JSON.

SYSTEM INTENT (from user):
\"\"\"{user_prompt}\"\"\"


DIGITAL INTERFACE HINT (from hardware spec):
{json.dumps(digital_hint, indent=2)}

REQUIRED OUTPUT (VERY IMPORTANT):

Return ONLY a valid JSON object (no markdown, no backticks, no comments, no extra text).
The JSON MUST strictly follow this schema:

{{
  "firmware_name": "short_name_snake_case",
  "description": "High-level description of what the firmware does.",
  "inputs_from_digital": [
    {{
      "name": "string",
      "type": "string",
      "description": "string"
    }}
  ],
  "outputs_to_digital": [
    {{
      "name": "string",
      "type": "string",
      "description": "string"
    }}
  ],
  "control_logic": "Plain language summary of the control flow and decisions.",
  "polling_interval_ms": 100,
  "logging": {{
    "enabled": true,
    "interface": "uart_printf"
  }}
}}

Guidelines:
- Use the digital interface hint when choosing inputs_from_digital and outputs_to_digital.
- If unsure, keep the list small and focused.
- If there are no obvious outputs_to_digital, you may return an empty list [] for that field.
- polling_interval_ms should be a reasonable integer (e.g., 50–1000).
- Do NOT wrap the JSON in ```json or any markdown fences.
- Do NOT add any explanation outside the JSON.
"""

    spec_json = None

    try:
        if USE_LOCAL_OLLAMA:
            import requests

            payload = {"model": "llama3", "prompt": prompt}
            response = requests.post(OLLAMA_URL, json=payload, timeout=600)
            raw = response.json().get("response", "").strip()
        else:
            completion = client_portkey.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
            )
            raw = completion.choices[0].message.content.strip()

        # Strip accidental code fences
        text = raw.strip()
        if text.startswith("```"):
            # remove first and last fence block
            parts = text.split("```")
            # pick the middle part if present
            text = parts[1].strip() if len(parts) >= 2 else text

        spec_json = json.loads(text)

        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"[{datetime.now()}] ✅ Embedded Spec Agent executed successfully.\n")
            log.write(f"User prompt: {user_prompt}\n")
            log.write(f"Digital spec path: {digital_spec_path}\n")
            log.write("Generated firmware spec JSON:\n")
            log.write(json.dumps(spec_json, indent=2))
            log.write("\n")

    except Exception as e:
        # Robust fallback JSON
        fallback = {
            "firmware_name": "fallback_firmware",
            "description": "Fallback firmware spec: monitor a boolean flag and log alerts.",
            "inputs_from_digital": inferred_inputs or [
                {
                    "name": "overheat_flag",
                    "type": "bool",
                    "description": "Set to 1 when system is overheated.",
                }
            ],
            "outputs_to_digital": inferred_outputs or [],
            "control_logic": "Periodically read overheat_flag; when 1, print an alert message.",
            "polling_interval_ms": 100,
            "logging": {
                "enabled": True,
                "interface": "uart_printf",
            },
        }
        spec_json = fallback
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"[{datetime.now()}] ⚠️ Embedded Spec Agent failed, using fallback: {e}\n")

    # --- Save spec JSON to disk ---


    firmware_name = spec_json.get("firmware_name") or "firmware"
    spec_path = os.path.join(workflow_dir, f"{firmware_name}_embedded_spec.json")
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump(spec_json, f, indent=2)

    # 🔴 IMPORTANT: these keys are what the Embedded Code Agent / system will look for
    state.update(
        {
            "status": "✅ Embedded Spec Generated",
            "spec_file": spec_path,           # used by Embedded Code Agent
            "embedded_spec_path": spec_path,  # explicit extra name (optional)
            "workflow_dir": workflow_dir,
            "workflow_id": workflow_id,
        }
    )

    print(f"✅ Embedded spec saved to {spec_path}")
    return state
    