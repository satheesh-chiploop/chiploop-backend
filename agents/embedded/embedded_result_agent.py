import os
import json
from datetime import datetime
from openai import OpenAI
from portkey_ai import Portkey
from agents.runtime import RUNTIME_ACTIVE_STATE_KEY, AgentContext, execute_agent
from utils.artifact_utils import save_text_artifact_and_record

AGENT_NAME = "Embedded Result Agent"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


def _run(context: AgentContext) -> dict:
    state = context.state
    print("\n📈 Running Embedded Result Agent...")

    telemetry_path = state.get("telemetry_file")
    if not telemetry_path or not os.path.exists(telemetry_path):
        state["status"] = "❌ Missing telemetry for result analysis"
        return state

    workflow_id = state.get("workflow_id", "embedded_default")
    agent_name = context.agent_name

    with open(telemetry_path, "r", encoding="utf-8") as f:
        telemetry = json.load(f)

    prompt = f"""
You are an expert firmware engineer explaining simulation results.

Given the following telemetry JSON from an embedded firmware simulation, write a clear,
human-friendly report suitable for a technical demo.

Include:
- Overall behavior (1 short paragraph)
- Bullet list of key events / transitions
- Any anomalies, missed events, or surprising behavior
- Suggested next steps or improvements (2–3 bullets)

Telemetry JSON:
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
                model="@chiploop/gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
            )
            summary = completion.choices[0].message.content.strip()

        # Save summary
        with open(result_path, "w", encoding="utf-8") as f:
            f.write(summary)

        # Log success
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"[{datetime.now()}] ✅ Result Agent completed successfully.\n")

        # Upload artifacts
        try:
            with open(result_path, "r", encoding="utf-8") as f:
                result_content = f.read()
            save_text_artifact_and_record(
                workflow_id=workflow_id,
                agent_name=agent_name,
                subdir="embedded",
                filename=os.path.basename(result_path),
                content=result_content,
            )

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

            print("🧩 Embedded Result Agent artifacts uploaded successfully.")
        except Exception as e:
            print(f"⚠️ Embedded Result Agent artifact upload failed: {e}")

        state.update(
            {
                "status": "✅ Embedded Result Generated",
                "result_file": result_path,
            }
        )
        print(f"✅ Result summary saved to {result_path}")
        return state

    except Exception as e:
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"[{datetime.now()}] ⚠️ Result generation failed: {e}\n")
        state["status"] = f"⚠️ Result generation failed: {e}"
        return state


def run_agent(state: dict) -> dict:
    context = AgentContext.from_state(state, AGENT_NAME)
    if state.get(RUNTIME_ACTIVE_STATE_KEY):
        return _run(context)
    result = execute_agent(context, _run)
    state.update(result.to_state_update())
    return state
