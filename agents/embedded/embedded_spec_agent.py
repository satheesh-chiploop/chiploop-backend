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

def run_agent(state: dict) -> dict:
    print("\n🧠 Running Embedded Spec Agent...")

    workflow_id = state.get("workflow_id", "embedded_default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    user_prompt = state.get("user_prompt", "").strip()
    if not user_prompt:
        user_prompt = "Design firmware to read a temperature sensor and blink an LED above 30°C."

    prompt = f"""
Convert this embedded system design prompt into a structured JSON specification
for firmware generation. Include:

- MCU
- Inputs
- Outputs
- Peripherals
- Communication Protocols
- Control Logic Summary
- Timing Requirements
- Power Constraints (if any)

Prompt: {user_prompt}
"""

    spec_json = None
    log_path = os.path.join(workflow_dir, "embedded_spec_agent.log")

    try:
        if USE_LOCAL_OLLAMA:
            import requests
            payload = {"model": "llama3", "prompt": prompt}
            response = requests.post(OLLAMA_URL, json=payload, timeout=600)
            spec_json = response.json().get("response", "").strip()
        else:
            completion = client_portkey.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
            )
            spec_json = completion.choices[0].message.content.strip()

        with open(log_path, "a") as log:
            log.write(f"[{datetime.now()}] ✅ Embedded Spec Agent executed successfully.\n")
            log.write(f"Prompt: {user_prompt}\n")
            log.write(f"Spec JSON:\n{spec_json}\n")

    except Exception as e:
        fallback = {
            "MCU": "Arduino Uno",
            "Inputs": ["temperature_sensor"],
            "Outputs": ["LED"],
            "Communication Protocols": ["UART"],
            "Control Logic Summary": "If temperature > 30°C, turn on LED",
            "Timing Requirements": "1Hz sampling rate"
        }
        spec_json = json.dumps(fallback, indent=2)
        with open(log_path, "a") as log:
            log.write(f"[{datetime.now()}] ⚠️ LLM failed. Fallback used: {e}\n")

    spec_path = os.path.join(workflow_dir, "embedded_spec.json")
    with open(spec_path, "w") as f:
        f.write(spec_json)

    state.update({
        "status": "✅ Embedded Spec Generated",
        "spec_file": spec_path,
        "workflow_dir": workflow_dir,
    })
    return state
