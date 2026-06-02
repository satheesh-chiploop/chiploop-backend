import os
import json
import subprocess
import datetime
import requests

from model_gateway import complete_text
from tooling.runner import run_command

# --- Configuration ---
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")

def run_agent(state: dict) -> dict:
    print("\n⚡ Running Optimizer Agent...")

    # --- Multi-user directory isolation ---
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)
    # --------------------------------------

    # --- Load RTL ---
    rtl_code = state.get("rtl", "")
    if not rtl_code:
        v_files = [f for f in os.listdir(workflow_dir) if f.endswith(".v")]
        if v_files:
            rtl_path = os.path.join(workflow_dir, v_files[0])
            with open(rtl_path, "r", encoding="utf-8") as f:
                rtl_code = f.read()
        else:
            state["status"] = "❌ No RTL available to optimize"
            return state

    # --- Load Spec for multi-clock/reset awareness ---
    spec_json_path = state.get("spec_json") or os.path.join(workflow_dir, "auto_module_spec.json")
    spec_context = ""
    if os.path.exists(spec_json_path):
        with open(spec_json_path, "r", encoding="utf-8") as f:
            spec = json.load(f)
            clocks = spec.get("clock", [])
            resets = spec.get("reset", [])
            domain_info = "\n".join([
                f"- Clock {i+1}: {clk.get('name', 'clk')} @ {clk.get('frequency_mhz', 100)}MHz, "
                f"Duty={clk.get('duty_cycle', 0.5)} | Reset: {resets[min(i, len(resets)-1)].get('name', 'reset')}"
                for i, clk in enumerate(clocks)
            ])
            spec_context = f"\nClock/Reset Context:\n{domain_info}\n"
    else:
        spec_context = "(Spec JSON not found — limited optimization context)"

    # --- Build optimization prompt ---
    prompt = f"""
You are an RTL optimizer. You are a Verilog-2005 code optimizer.
Task:
- Input RTL code:
{rtl_code}

- Optimize it for readability and synthesis.
- Ensure it remains synthesizable.
- Write synthesizable Verilog-2005 code (not SystemVerilog).
- Do not use markdown or ``` fences.
- Use lower-case signal names (clk, reset, enable) to avoid binding issues.
- Always declare every signal used in the code (clk, reset, enable, etc.).
- Do not include natural language.
- Only output Verilog starting with 'module'.
- If reset is required, use a single always block with either synchronous or asynchronous reset.
- Module name must match the spec.
- End with 'endmodule'.

{spec_context}
"""

    optimized_rtl = ""
    log_path = os.path.join(workflow_dir, "optimizer_agent_compile.log")
    optimized_file = os.path.join(workflow_dir, "design_optimized.v")

    # --- 1️⃣ Choose LLM path (Ollama vs Portkey/OpenAI) ---
    try:
        if USE_LOCAL_OLLAMA:
            print("⚙️ Using local Ollama for optimization...")
            payload = {"model": "llama3", "prompt": prompt}
            with requests.post(OLLAMA_URL, json=payload, stream=True, timeout=600) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        j = json.loads(line.decode())
                        if "response" in j:
                            optimized_rtl += j["response"]
                            print(j["response"], end="", flush=True)
                    except Exception:
                        continue
        else:
            print("🌐 Using Portkey backend for optimization...")
            try:
                optimized_rtl += complete_text(
                    prompt,
                    capability="rtl_generation",
                    agent_name="Digital Optimizer Agent",
                    state=state,
                )
                print(optimized_rtl[:1200], flush=True)
            except Exception as e:
                print(f"⚠️ Portkey failed, falling back to Ollama: {e}")
                payload = {"model": "llama3", "prompt": prompt}
                with requests.post(OLLAMA_URL, json=payload, stream=True, timeout=600) as r:
                    for line in r.iter_lines():
                        if not line:
                            continue
                        try:
                            j = json.loads(line.decode())
                            if "response" in j:
                                optimized_rtl += j["response"]
                        except Exception:
                            continue
    except Exception as e:
        print(f"⚠️ LLM optimization failed: {e}")
        state["status"] = f"⚠️ Optimization LLM failed: {e}"
        return state

    # --- 2️⃣ Clean output ---
    optimized_rtl = (
        optimized_rtl.replace("```verilog", "")
        .replace("```systemverilog", "")
        .replace("```", "")
        .strip()
    )
    if "module" in optimized_rtl:
        optimized_rtl = optimized_rtl[optimized_rtl.index("module"):]

    # --- 3️⃣ Save optimized RTL ---
    with open(optimized_file, "w", encoding="utf-8") as f:
        f.write(optimized_rtl)

    # --- 4️⃣ Compile with Icarus ---
    try:
        result = run_command(
            state,
            "digital_optimizer_compile",
            ["iverilog", "-o", "design_optimized.out", optimized_file],
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode if result.returncode is not None else 1, result.command, output=result.stdout, stderr=result.stderr)
        compile_status = "✅ Optimized RTL compiled successfully"
        with open(log_path, "w", encoding="utf-8") as logf:
            logf.write(result.stdout + result.stderr)
    except subprocess.CalledProcessError as e:
        compile_status = "⚠ Optimized RTL failed compilation"
        with open(log_path, "w", encoding="utf-8") as logf:
            logf.write((e.stdout or "") + (e.stderr or ""))

    # --- 5️⃣ Update state ---
    state.update({
        "optimized_rtl": optimized_rtl,
        "artifact": optimized_file,
        "artifact_log": log_path,
        "status": compile_status,
        "workflow_id": workflow_id,
        "workflow_dir": workflow_dir,
    })

    print(f"\n✅ Optimizer Agent completed — {compile_status}")
    return state


# --- Local test ---
if __name__ == "__main__":
    test_state = {
        "rtl": "module counter(input clk, input reset, output reg [3:0] count); always @(posedge clk) if(reset) count<=0; else count<=count+1; endmodule",
        "spec_json": "auto_module_spec.json",
        "workflow_id": "test_run_optimizer"
    }
    print(json.dumps(optimizer_agent(test_state), indent=2))
