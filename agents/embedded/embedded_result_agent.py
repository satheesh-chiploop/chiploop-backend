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
    print("\n📈 Running Embedded Result Agent...")

    telemetry_path = state.get("telemetry_file")
    if not telemetry_path or not os.path.exists(telemetry_path):
        state["status"] = "❌ Missing telemetry for result analysis"
        return state

    with open(telemetry_path, "r") as f:
        telemetry = json.load(f)

    prompt = f"""
Summarize this firmware simulation telemetry into a clear, human-friendly report.
Highlight:
- total duration
- key behaviors observed
- I/O activity (LEDs, sensors, serial)
- any anomalies or missed events

Telemetry:
{json.dumps(telemetry, indent=2)}
"""

    workflow_dir = os.path.dirname(telemetry_path)
    log_path = os.path.join(workflow_dir, "embedded_result_agent.log")
    result_path = os.path.join(workflow_dir, "result_summary.txt")

    try:
        if USE_LOCAL_OLLAMA:
            import requests
            payload = {"model": "llama3", "prompt": prompt}
            response = requests.post(OLLAMA_URL, json=payload, timeout=600)
            summary = response.json().get("response", "").strip()
        else:
            completion = client_portkey.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
            )
            summary = completion.choices[0].message.content.strip()

        with open(result_path, "w") as f:
            f.write(summary)

        with open(log_path, "a") as log:
            log.write(f"[{datetime.now()}] ✅ Result Agent completed successfully.\n")

        state.update({
            "status": "✅ Embedded Result Generated",
            "result_file": result_path,
        })
        print(f"✅ Result summary saved to {result_path}")

    except Exception as e:
        with open(log_path, "a") as log:
            log.write(f"[{datetime.now()}] ⚠️ Result generation failed: {e}\n")
        state["status"] = f"⚠️ Result generation failed: {e}"

    return state
