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
    print("\n💻 Running Embedded Code Agent...")

    spec_path = state.get("spec_file")
    if not spec_path or not os.path.exists(spec_path):
        state["status"] = "❌ Missing spec file for code generation"
        return state

    with open(spec_path, "r") as f:
        spec_text = f.read()

    prompt = f"""
Generate clean embedded C code based on this firmware specification.
Use standard syntax compatible with Arduino or STM32 HAL.
Include initialization, main loop, and logic matching the control summary.

Specification:
{spec_text}

Output: Embedded C source code only (no Markdown or explanations).
"""

    code = None
    workflow_dir = os.path.dirname(spec_path)
    log_path = os.path.join(workflow_dir, "embedded_code_agent.log")

    try:
        if USE_LOCAL_OLLAMA:
            import requests
            payload = {"model": "llama3", "prompt": prompt}
            response = requests.post(OLLAMA_URL, json=payload, timeout=600)
            code = response.json().get("response", "").strip()
        else:
            completion = client_portkey.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
            )
            code = completion.choices[0].message.content.strip()

        with open(log_path, "a") as log:
            log.write(f"[{datetime.now()}] ✅ Embedded Code Agent executed successfully.\n")
            log.write(f"Spec used: {spec_path}\n")

    except Exception as e:
        code = """#include <Arduino.h>
int sensorPin = A0;
int ledPin = 13;
void setup(){ pinMode(ledPin, OUTPUT); }
void loop(){
  int temp = analogRead(sensorPin);
  if(temp > 512) digitalWrite(ledPin, HIGH);
  else digitalWrite(ledPin, LOW);
  delay(1000);
}"""
        with open(log_path, "a") as log:
            log.write(f"[{datetime.now()}] ⚠️ LLM failed. Fallback code used: {e}\n")

    code_path = os.path.join(workflow_dir, "main.c")
    with open(code_path, "w") as f:
        f.write(code)

    state.update({
        "status": "✅ Embedded Code Generated",
        "code_file": code_path,
    })
    print(f"✅ Code saved to {code_path}")
    return state
