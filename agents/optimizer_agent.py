import os
import json
import subprocess
import datetime
import requests
from portkey_ai import Portkey
from openai import OpenAI

# --- Configuration ---
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()

def optimizer_agent(state: dict) -> dict:
    print("\n⚡ Running Optimizer Agent...")

    # --- Multi-user directory isolation ---
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)
    os.chdir(workflow_dir)
    # --------------------------------------

    rtl_code = state.get("rtl") or ""
    spec_json = state.get("spec_json", os.path.join(workflow_dir, "auto_module_spec.json"))

    if not rtl_code:
        # if not present, read from previous stage file
        rtl_path = os.path.join(workflow_dir, "auto_module.v")
        if os.path.exists(rtl_path):
            with open(rtl_path, "r") as f:
                rtl_code = f.read()
        else:
            state["status"] = "❌ No RTL available to optimize"
            return state

    # --- Load spec for multi-clock/reset awareness ---
    spec_context = ""
    if os.path.exists(spec_json):
        with open(spec_json, "r") as f:
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
            completion = client_portkey.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                stream=True,
            )
            for chunk in completion:
                if chunk and hasattr(chunk, "choices"):
                    delta = chunk.choices[0].delta.get("content", "")
                    if delta:
                        optimized_rtl += delta
                        print(delta, end="", flush=True)
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
        result = subprocess.run(
            ["iverilog", "-o", "design_optimized.out", optimized_file],
            capture_output=True,
            text=True,
            check=True,
        )
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
