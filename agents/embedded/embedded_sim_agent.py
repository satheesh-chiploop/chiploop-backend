import os
import json
from datetime import datetime
from openai import OpenAI
from portkey_ai import Portkey
from utils.artifact_utils import save_text_artifact_and_record

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

    workflow_id = state.get("workflow_id", "embedded_default")
    workflow_dir = os.path.dirname(code_path)
    log_path = os.path.join(workflow_dir, "embedded_sim_agent.log")
    sim_log_path = os.path.join(workflow_dir, "simulation.log")
    telemetry_path = os.path.join(workflow_dir, "telemetry.json")
    agent_name = "Embedded Sim Agent"

    try:
        # 1) Read code
        with open(code_path, "r", encoding="utf-8") as f:
            code = f.read()

        # 2) Build prompt (firmware + optional RTL summary if you add it to state later)
        prompt = f"""
You are a firmware simulation assistant.

Task:
Simulate approximately one minute of operation of the embedded firmware code below.
Focus on observable behavior: sensor reads, threshold checks, flags, LED/relay toggles,
UART/printf messages, timers/delays, and error conditions.

Return ONLY valid JSON with the following structure:

{{
  "timeline": [
    {{
      "t_ms": <integer timestamp in milliseconds>,
      "event": "<short event title>",
      "details": "<one-line explanation of what happened>"
    }}
  ],
  "summary": "<2–4 sentence natural-language summary of overall behavior>",
  "key_signals": [
    "<important signals or flags mentioned in the code, e.g., overheat_flag>"
  ]
}}

Do not include any commentary outside the JSON.

C code:
{code}
"""

        # 3) Call LLM
        if USE_LOCAL_OLLAMA:
            import requests
            payload = {"model": "llama3", "prompt": prompt}
            response = requests.post(OLLAMA_URL, json=payload, timeout=600)
            sim_result = response.json().get("response", "")
        else:
            completion = client_portkey.chat.completions.create(
                model="@chiploop/gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
            )
            sim_result = completion.choices[0].message.content.strip()

        # 4) Strip fences if present
        if sim_result.startswith("```"):
            sim_result = sim_result.strip("`")
            # crude but good enough: remove leading language tag if present
            if "\n" in sim_result:
                first_line, rest = sim_result.split("\n", 1)
                if first_line.strip().startswith("{"):
                    sim_result = first_line + "\n" + rest
                else:
                    sim_result = rest

        # 5) Try to parse JSON, fallback to minimal structure
        try:
            telemetry = json.loads(sim_result)
        except Exception:
            telemetry = {
                "timeline": [{"t_ms": 0, "event": "Simulation started", "details": "Fallback telemetry"}],
                "summary": "Could not parse structured JSON from the model; using fallback timeline.",
                "key_signals": [],
            }

        # 6) Write raw sim output + telemetry to disk
        with open(sim_log_path, "w", encoding="utf-8") as f:
            f.write(sim_result)

        with open(telemetry_path, "w", encoding="utf-8") as f:
            json.dump(telemetry, f, indent=2)

        # 7) Log success
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"[{datetime.now()}] ✅ Simulation completed successfully.\n")

        # 8) Upload artifacts to Supabase
        try:
            # Telemetry JSON
            with open(telemetry_path, "r", encoding="utf-8") as f:
                telemetry_content = f.read()
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="embedded",
                filename=os.path.basename(telemetry_path),
                content=telemetry_content,
            )

            # Simulation raw log
            with open(sim_log_path, "r", encoding="utf-8") as f:
                sim_content = f.read()
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="embedded",
                filename=os.path.basename(sim_log_path),
                content=sim_content,
            )

            # Agent log
            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8") as f:
                    log_content = f.read()
                save_text_artifact_and_record(
                    workflow_id=workflow_id,
                    agent_name=agent_name,
                    subdir="embedded",
                    filename=os.path.basename(log_path),
                    content=log_content,
                )

            print("🧩 Embedded Sim Agent artifacts uploaded successfully.")
        except Exception as e:
            print(f"⚠️ Embedded Sim Agent artifact upload failed: {e}")

        # 9) Update state for downstream Result Agent + UI
        state.update(
            {
                "status": "✅ Simulation Completed",
                "telemetry_file": telemetry_path,
                "simulation_log": sim_log_path,
            }
        )
        return state

    except Exception as e:
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"[{datetime.now()}] ⚠️ Simulation failed: {e}\n")
        state["status"] = f"⚠️ Simulation failed: {e}"
        return state
