# agents/spec_agent.py
import os
import subprocess
import requests
import json
import time
from portkey_ai import Portkey
from openai import OpenAI

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_openai = OpenAI()
client_portkey = Portkey(api_key=PORTKEY_API_KEY)

def spec_agent(state: dict) -> dict:
    """
    Spec Agent — Generates synthesizable Verilog code based on natural-language spec.
    Supports both local Ollama and Portkey/OpenAI for flexibility.
    """
    print("\n🚀 Running Spec Agent...")
    spec = state.get("spec", "").strip()
    if not spec:
        state["status"] = "❌ No spec provided"
        return state

    # -------- Build generation prompt --------
    prompt = f"""
    You are a Verilog-2005 code generator.

    Task:
    - Write synthesizable Verilog-2005 code (not SystemVerilog).
    - Do not use markdown or ``` fences.
    - Use lower-case signal names (clk, reset, enable).
    - Always declare every signal used.
    - Only output Verilog starting with 'module'.
    - End with 'endmodule'.

    Spec: {spec}
    """

    rtl_code = ""

    # -------- Generate Verilog using LLM --------
    try:
        if USE_LOCAL_OLLAMA:
            print("⚙️ Using Ollama locally...")
            payload = {"model": "llama3", "prompt": prompt}
            with requests.post(OLLAMA_URL, json=payload, stream=True, timeout=300) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        j = json.loads(line.decode())
                        if "response" in j:
                            rtl_code += j["response"]
                            print(j["response"], end="", flush=True)
                    except Exception:
                        continue
        else:
            print("🌐 Using Portkey/OpenAI path...")
            completion = client_portkey.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                stream=True,
            )
            for chunk in completion:
                if chunk and hasattr(chunk, "choices"):
                    delta = chunk.choices[0].delta.get("content", "")
                    if delta:
                        rtl_code += delta
                        print(delta, end="", flush=True)
    except Exception as e:
        state["status"] = f"❌ LLM generation failed: {e}"
        return state

    # -------- Clean & normalize RTL --------
    rtl_code = rtl_code.replace("```verilog", "").replace("```systemverilog", "").replace("```", "").strip()
    if "module" in rtl_code:
        rtl_code = rtl_code[rtl_code.index("module"):]

    # -------- Save Verilog to file --------
    rtl_path = "design.v"
    with open(rtl_path, "w", encoding="utf-8") as f:
        f.write(rtl_code)

    # -------- Compile with Icarus Verilog --------
    log_path = "spec_agent_compile.log"
    if os.path.exists(log_path):
        os.remove(log_path)

    try:
        print("\n🚀 Compiling...")
        result = subprocess.run(
            ["/usr/bin/iverilog", "-o", "design.out", rtl_path],
            check=True,
            capture_output=True,
            text=True
        )
        state["status"] = "✅ Compilation successful"
        state["compiler_output"] = result.stdout.strip()
        with open(log_path, "a") as logf:
            logf.write(result.stdout or "")
    except subprocess.CalledProcessError as e:
        state["status"] = "❌ Compilation failed"
        state["error_log"] = (e.stderr or e.stdout or "").strip()
        with open(log_path, "a") as logf:
            logf.write(state["error_log"])

    # -------- Return structured result --------
    state["rtl"] = rtl_code
    state["artifact"] = rtl_path
    state["artifact_log"] = log_path

    return state

# Standalone test
if __name__ == "__main__":
    init_state = {"spec": "4-bit counter with synchronous reset"}
    result = spec_agent(init_state)
    print(result["status"])
    if "error_log" in result:
        print(result["error_log"])







