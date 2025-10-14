import os
import json
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

def run_agent(state: dict) -> dict:
    print("\n🏗️ Running Architecture Doc Agent...")

    # --- Multi-user safe workflow directory ---
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)
    # ------------------------------------------

    # --- Dynamic file discovery ---
    spec_json = state.get("spec_json")
    if not spec_json or not os.path.exists(spec_json):
        json_files = [f for f in os.listdir(workflow_dir) if f.endswith("_spec.json")]
        spec_json = os.path.join(workflow_dir, json_files[0]) if json_files else os.path.join(workflow_dir, "auto_module_spec.json")

    rtl_file = state.get("artifact")
    if not rtl_file or not os.path.exists(rtl_file):
        v_files = [f for f in os.listdir(workflow_dir) if f.endswith(".v")]
        rtl_file = os.path.join(workflow_dir, v_files[0]) if v_files else os.path.join(workflow_dir, "auto_module.v")

    spec_data = {}
    rtl_code = ""

    if os.path.exists(spec_json):
        with open(spec_json, "r", encoding="utf-8") as f:
            spec_data = json.load(f)
    if os.path.exists(rtl_file):
        with open(rtl_file, "r", encoding="utf-8") as f:
            rtl_code = f.read()

    clocks = spec_data.get("clock", [])
    resets = spec_data.get("reset", [])
    module_name = spec_data.get("module", "auto_module")

    domain_info = "\n".join([
        f"- Clock {i+1}: {clk.get('name', 'clk')} @ {clk.get('frequency_mhz', 100)}MHz, "
        f"Duty={clk.get('duty_cycle', 0.5)} | Reset: {resets[min(i, len(resets)-1)].get('name', 'reset')}"
        for i, clk in enumerate(clocks)
    ])

    # --- Prompt construction ---
    prompt = f"""
You are a hardware architecture documentation assistant.
Generate a detailed architecture design document for this Verilog module.

Inputs:
Specification JSON:
{json.dumps(spec_data, indent=2)}

Verilog RTL:
{rtl_code[:3000]}

Clock/Reset Context:
{domain_info}

Guidelines:
- Start with "## Module Overview"
- Include subsections: Clock Domains, Reset Strategy, Data Flow, Control Logic, Interfaces, and Key Features.
- Keep technical tone (no fluff).
- Output Markdown only.
- Do NOT use ``` fences.
"""

    arch_doc = ""
    log_path = os.path.join(workflow_dir, "arch_doc_agent_compile.log")
    output_file = os.path.join(workflow_dir, "architecture_doc.md")

    # --- LLM logic with fallback ---
    try:
        if USE_LOCAL_OLLAMA:
            print("⚙️ Using local Ollama for architecture doc generation...")
            payload = {"model": "llama3", "prompt": prompt}
            with requests.post(OLLAMA_URL, json=payload, stream=True, timeout=600) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        j = json.loads(line.decode())
                        if "response" in j:
                            arch_doc += j["response"]
                            print(j["response"], end="", flush=True)
                    except Exception:
                        continue
        else:
            print("🌐 Using Portkey for architecture doc generation...")
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
                            arch_doc += delta
                            print(delta, end="", flush=True)
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
                                arch_doc += j["response"]
                        except Exception:
                            continue
    except Exception as e:
        print(f"❌ Architecture Doc generation failed: {e}")
        state["status"] = f"❌ Architecture Doc generation failed: {e}"
        return state

    # --- Save outputs ---
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(arch_doc or "⚠️ No architecture content generated.")

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"Architecture documentation generated at {datetime.datetime.now()}\n")

    # --- Update state ---
    state.update({
        "status": "✅ Architecture document generated",
        "artifact": output_file,
        "artifact_log": log_path,
        "architecture_doc": arch_doc,
        "workflow_id": workflow_id,
        "workflow_dir": workflow_dir,
    })

    print(f"\n✅ Architecture Doc Agent completed — {output_file}")
    return state


# --- Local test ---
if __name__ == "__main__":
    test_state = {"workflow_id": "test_arch_doc"}
    print(json.dumps(arch_doc_agent(test_state), indent=2))




