import os
import re
import json
import datetime
import subprocess
import requests
from portkey_ai import Portkey
from openai import OpenAI

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()

def rtl_agent(state: dict) -> dict:
    print("\n🧠 Running RTL Agent (Spec-Aware Validation)...")

    # --- Added: Multi-user workflow isolation ---
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)
    os.chdir(workflow_dir)
    # --------------------------------------------
    rtl_file = state.get("artifact")
    if not rtl_file or not os.path.exists(rtl_file):
        v_files = [f for f in os.listdir(workflow_dir) if f.endswith(".v")]
        if not v_files:
          raise FileNotFoundError(f"No RTL (.v) file found in {workflow_dir}")
        rtl_file = os.path.join(workflow_dir, v_files[0])  # pick the first .v file found

    print(f"✅ Using RTL file: {rtl_file}")
    # Detect the JSON spec file dynamically

    if not spec_file or not os.path.exists(spec_file):
        json_candidates = [f for f in os.listdir(workflow_dir) if f.endswith("_spec.json")]
        if not json_candidates:
          raise FileNotFoundError(f"No spec JSON file found in {workflow_dir}")
        spec_file = os.path.join(workflow_dir, json_candidates[0])
        

    if not os.path.exists(rtl_file):
        state["status"] = f"❌ RTL file '{rtl_file}' not found."
        return state
    if not spec_file or not os.path.exists(spec_file):
        state["status"] = "⚠ Spec JSON not found — limited validation mode."
        spec = {}
    else:
        with open(spec_file, "r") as f:
            spec = json.load(f)

    # --- Step 1: Basic Verilog Syntax Lint via Icarus ---
    log_path = os.path.join(workflow_dir, "rtl_agent_compile.log")
    try:
        result = subprocess.run(
            ["/usr/bin/iverilog", "-o", "rtl_check.out", rtl_file],
            check=True, capture_output=True, text=True
        )
        compile_status = "✅ Verilog syntax check passed."
    except subprocess.CalledProcessError as e:
        compile_status = "⚠ Verilog syntax check failed."
        error_log = (e.stderr or e.stdout or "").strip()
        with open(log_path, "w") as logf:
            logf.write(error_log)
        state.update({
            "status": compile_status,
            "error_log": error_log,
            "artifact_log": log_path,
            "workflow_id": workflow_id,
            "workflow_dir": workflow_dir
        })
        return state

    # --- Step 2: Extract RTL ports from Verilog ---
    with open(rtl_file, "r") as f:
        verilog_text = f.read()

    ports = re.findall(r"(input|output|inout)\s+(?:wire|reg)?\s*(?:\[[^\]]+\]\s*)?(\w+)", verilog_text)
    port_names = [p[1] for p in ports]
    print(f"🔍 Extracted ports: {port_names}")

    clocks_detected = [p for p in port_names if re.search(r"clk|clock", p, re.IGNORECASE)]
    resets_detected = [p for p in port_names if re.search(r"rst|reset", p, re.IGNORECASE)]

    # --- Step 3: Validate with spec.json ---
    issues = []
    if "clock" in spec:
        for clk in spec["clock"]:
            name = clk["name"]
            if name not in port_names:
                issues.append(f"❌ Clock '{name}' missing in RTL ports.")
    if "reset" in spec:
        for rst in spec["reset"]:
            name = rst["name"]
            if name not in port_names:
                issues.append(f"❌ Reset '{name}' missing in RTL ports.")
    if "io" in spec:
        for pin in spec["io"].get("inputs", []):
            pin_name = re.split(r"\[", pin)[0]
            if pin_name not in port_names:
                issues.append(f"⚠ Input '{pin}' not found in RTL.")
        for pin in spec["io"].get("outputs", []):
            pin_name = re.split(r"\[", pin)[0]
            if pin_name not in port_names:
                issues.append(f"⚠ Output '{pin}' not found in RTL.")

    # --- Step 4: Optional LLM-based linting ---
    lint_feedback = ""
    try:
        lint_prompt = f"""
You are a senior RTL reviewer.
Analyze the following Verilog code for any logic or style issues (not syntax).
Summarize issues clearly.

{verilog_text[:3000]}
"""
        if USE_LOCAL_OLLAMA:
            payload = {"model": "llama3", "prompt": lint_prompt}
            r = requests.post(OLLAMA_URL, json=payload, timeout=60)
            lint_feedback = r.text.strip()
        else:
            response = client_portkey.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": lint_prompt}],
            )
            lint_feedback = response.choices[0].message["content"]
    except Exception as e:
        lint_feedback = f"(Skipped LLM lint due to {e})"

    # --- Step 5: Log Summary ---
    with open(log_path, "w") as logf:
        logf.write(f"RTL Validation Log — {datetime.datetime.now()}\n\n")
        logf.write(f"{compile_status}\n\n")
        if issues:
            logf.write("Spec mismatches:\n")
            logf.writelines(f"- {i}\n" for i in issues)
        else:
            logf.write("✅ All spec ports found in RTL.\n")
        logf.write("\n🔍 LLM Review Summary:\n")
        logf.write(lint_feedback)

    # --- Step 6: Update state ---
    overall_status = "⚠ RTL validation completed with mismatches." if issues else "✅ RTL validated successfully."

    state.update({
        "status": overall_status,
        "artifact_log": log_path,
        "lint_feedback": lint_feedback,
        "port_list": port_names,
        "clock_ports": clocks_detected,
        "reset_ports": resets_detected,
        "issues": issues,
        "workflow_id": workflow_id,
        "workflow_dir": workflow_dir
    })

    print(f"🧾 RTL Agent completed — {overall_status}")
    return state


# --- Local Test Example ---
if __name__ == "__main__":
    state = {
        "artifact": "uart_tx.v",
        "spec_json": "uart_tx_spec.json",
        "workflow_id": "test_run_1"
    }
    result = rtl_agent(state)
    print(json.dumps(result, indent=2))
