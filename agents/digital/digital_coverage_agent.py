# agents/coverage_agent.py
import os
import re
import json
import datetime
import subprocess
import requests
from portkey_ai import Portkey

# --- Config consistent with other agents ---
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)


def run_agent(state: dict) -> dict:
    """
    Analyze functional & code coverage results after simulation.

    Inputs:
      - simulation artifacts (log, vcd, ucdb, or coverage reports)
      - optional functional coverage definitions (covergroups.sv)
    Outputs:
      - backend/workflows/<id>/coverage_summary.json
      - backend/workflows/<id>/coverage_summary.txt
    """

    print("\n📊 Running Coverage Agent...")
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    # --- Detect simulation log / coverage database ---
    sim_logs = [f for f in os.listdir(workflow_dir) if re.search(r"(sim|run).*\.log$", f)]
    cov_files = [f for f in os.listdir(workflow_dir) if re.search(r"(vcd|ucdb|cov|coverage)", f)]

    sim_log_path = os.path.join(workflow_dir, sim_logs[0]) if sim_logs else None
    cov_file_path = os.path.join(workflow_dir, cov_files[0]) if cov_files else None

    log_data = ""
    if sim_log_path and os.path.exists(sim_log_path):
        log_data = open(sim_log_path, "r", encoding="utf-8", errors="ignore").read()[-5000:]

    # --- Try parsing line coverage summary if simulator provided it ---
    line_cov = re.findall(r"Line coverage\s*[:=]\s*(\d+)%", log_data)
    toggle_cov = re.findall(r"Toggle coverage\s*[:=]\s*(\d+)%", log_data)
    branch_cov = re.findall(r"Branch coverage\s*[:=]\s*(\d+)%", log_data)
    func_cov = re.findall(r"Functional coverage\s*[:=]\s*(\d+)%", log_data)

    base_summary = {
        "line": int(line_cov[-1]) if line_cov else None,
        "toggle": int(toggle_cov[-1]) if toggle_cov else None,
        "branch": int(branch_cov[-1]) if branch_cov else None,
        "functional": int(func_cov[-1]) if func_cov else None,
    }

    prompt = f"""
You are a verification coverage analyst.
Summarize and explain the simulation coverage below.
Include:
- Overall coverage percentages
- Missed bins or signals (if visible)
- Suggestions for improving coverage
Keep it concise and technical (engineer-friendly).

Log snippet:
{log_data[:4000]}

Coverage numbers:
{json.dumps(base_summary, indent=2)}
"""

    coverage_summary = ""
    if USE_LOCAL_OLLAMA:
        print("⚙️ Using local Ollama to summarize coverage...")
        payload = {"model": "llama3", "prompt": prompt}
        with requests.post(OLLAMA_URL, json=payload, stream=True) as r:
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    j = json.loads(line.decode())
                    if "response" in j:
                        coverage_summary += j["response"]
                        print(j["response"], end="", flush=True)
                except Exception:
                    continue
    else:
        print("🌐 Using Portkey to summarize coverage...")
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
                        coverage_summary += delta
                        print(delta, end="", flush=True)
        except Exception as e:
            print(f"⚠️ Portkey failed, fallback to Ollama: {e}")
            payload = {"model": "llama3", "prompt": prompt}
            with requests.post(OLLAMA_URL, json=payload, stream=True) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        j = json.loads(line.decode())
                        if "response" in j:
                            coverage_summary += j["response"]
                    except Exception:
                        continue

    # --- Save results ---
    json_path = os.path.join(workflow_dir, "coverage_summary.json")
    txt_path = os.path.join(workflow_dir, "coverage_summary.txt")

    with open(json_path, "w", encoding="utf-8") as jf:
        json.dump(base_summary, jf, indent=2)
    with open(txt_path, "w", encoding="utf-8") as tf:
        tf.write(coverage_summary)

    # --- Update state for workflow persistence ---
    state.update({
        "status": "✅ Coverage analysis completed",
        "artifact": txt_path,
        "artifact_log": json_path,
        "coverage": base_summary,
        "workflow_id": workflow_id,
        "workflow_dir": workflow_dir,
    })

    print(f"\n✅ Coverage Agent completed — results saved in {txt_path}")
    return state


# --- Local test ---
if __name__ == "__main__":
    test_state = {
        "workflow_id": "test_coverage_agent",
        "workflow_dir": "backend/workflows/test_coverage_agent"
    }
    os.makedirs(test_state["workflow_dir"], exist_ok=True)
    print(json.dumps(coverage_agent(test_state), indent=2))
