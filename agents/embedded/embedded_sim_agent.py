import os
import json
import subprocess
from datetime import datetime
from openai import OpenAI
from portkey_ai import Portkey

USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()

def run_agent(state: dict) -> dict:
    print("\n⚙️ Running Embedded Sim Agent...")

    code_path = state.get("code_file")
    if not code_path or not os.path.exists(code_path):
        state["status"] = "❌ Missing code file for simulation"
        return state

    workflow_dir = os.path.dirname(code_path)
    log_path = os.path.join(workflow_dir, "embedded_sim_agent.log")
    sim_log_path = os.path.join(workflow_dir, "simulation.log")
    telemetry_path = os.path.join(workflow_dir, "telemetry.json")

    try:
        # For MVP, simulate logic via LLM (no real build)
        with open(code_path, "r") as f:
            code = f.read()

        prompt = f"""
Analyze this embedded firmware C code and simulate one minute of operation.
List key events (sensor reads, LED toggles, UART prints, delays).
Return JSON with:
- timeline (array of events with timestamps)
- summary (human-readable simulation overview)
Code:
{code}
"""

        if USE_LOCAL_OLLAMA:
            import requests
            payload = {"model": "llama3", "prompt": prompt}
            response = requests.post(OLLAMA_URL, json=payload, timeout=600)
            sim_result = response.json().get("response", "")
        else:
            completion = client_portkey.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
            )
            sim_result = completion.choices[0].message.content.strip()

        # Try to extract JSON if possible
        try:
            telemetry = json.loads(sim_result)
        except Exception:
            telemetry = {
                "timeline": [{"t": 0, "event": "Simulation started"}],
                "summary": "Could not parse structured output; using fallback summary."
            }

        with open(sim_log_path, "w") as f:
            f.write(sim_result)
        with open(telemetry_path, "w") as f:
            json.dump(telemetry, f, indent=2)

        with open(log_path, "a") as log:
            log.write(f"[{datetime.now()}] ✅ Simulation completed successfully.\n")

        state.update({
            "status": "✅ Simulation Completed",
            "telemetry_file": telemetry_path,
            "simulation_log": sim_log_path,
        })

    except Exception as e:
        with open(log_path, "a") as log:
            log.write(f"[{datetime.now()}] ⚠️ Simulation failed: {e}\n")
        state["status"] = f"⚠️ Simulation failed: {e}"

    return state
