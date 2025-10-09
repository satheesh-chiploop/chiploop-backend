import os, json, datetime, requests
from portkey_ai import Portkey
from openai import OpenAI

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()

def testcase_agent(state: dict) -> dict:
    print("\n🧩 Running Testcase Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    num_random_tests = int(state.get("num_random_tests", os.getenv("NUM_RANDOM_TESTS", 5)))

    rtl_files = [f for f in os.listdir(workflow_dir) if f.endswith(".v")]
    tb_dirs = [d for d in os.listdir(workflow_dir) if d.startswith("uvm_tb_")]
    rtl_file = os.path.join(workflow_dir, rtl_files[0]) if rtl_files else None
    tb_dir = os.path.join(workflow_dir, tb_dirs[0]) if tb_dirs else None

    if not rtl_file or not tb_dir:
        raise FileNotFoundError("Missing RTL or Testbench for testcase generation.")

    rtl_text = open(rtl_file, "r", encoding="utf-8").read()

    prompt = f"""
You are a SystemVerilog UVM verification engineer.
Generate UVM testcases for this module.

Types:
1. Directed tests (basic functionality + corner case)
2. {num_random_tests} randomized tests using UVM constrained-random sequences.
   - Use `rand` variables and `constraint` blocks
   - Generate transactions with `uvm_do_with` or `start_item()/finish_item()`
   - Include seed usage ($urandom or parameterized seed)
   - Prefix random tests as <module>_randN_test

RTL (snippet):
{rtl_text[:2000]}

Guidelines:
- Each testcase must be a UVM class derived from `uvm_test`
- Use `uvm_component_utils`
- Output pure SystemVerilog code, no markdown fences
"""

    tc_code = ""
    log_path = os.path.join(workflow_dir, "testcase_agent_compile.log")

    try:
        if USE_LOCAL_OLLAMA:
            print("⚙️ Using local Ollama for testcase generation...")
            payload = {"model": "llama3", "prompt": prompt}
            with requests.post(OLLAMA_URL, json=payload, stream=True, timeout=600) as r:
                for line in r.iter_lines():
                    if not line: continue
                    try:
                        j = json.loads(line.decode())
                        if "response" in j: tc_code += j["response"]
                    except Exception: continue
        else:
            print("🌐 Using Portkey for testcase generation...")
            try:
                completion = client_portkey.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    stream=True,
                )
                for chunk in completion:
                    if chunk and hasattr(chunk, "choices"):
                        delta = chunk.choices[0].delta.get("content", "")
                        if delta: tc_code += delta
            except Exception as e:
                print(f"⚠️ Portkey failed, fallback to Ollama: {e}")
                payload = {"model": "llama3", "prompt": prompt}
                with requests.post(OLLAMA_URL, json=payload, stream=True, timeout=600) as r:
                    for line in r.iter_lines():
                        if not line: continue
                        try:
                            j = json.loads(line.decode())
                            if "response" in j: tc_code += j["response"]
                        except Exception: continue
    except Exception as e:
        state["status"] = f"❌ Testcase generation failed: {e}"
        return state

    tc_file = os.path.join(tb_dir, "generated_tests.sv")
    open(tc_file, "w", encoding="utf-8").write(tc_code)

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"Directed + {num_random_tests} random testcases generated at {datetime.datetime.now()}\n")

    state.update({
        "status": f"✅ Directed + Random Testcases generated ({num_random_tests})",
        "artifact": tc_file,
        "artifact_log": log_path,
        "workflow_dir": workflow_dir,
        "workflow_id": workflow_id,
        "num_random_tests": num_random_tests,
    })

    print(f"✅ Testcase Agent completed — {tc_file}")
    return state
