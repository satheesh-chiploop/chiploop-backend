import os
import json
import datetime
import requests
from portkey_ai import Portkey
from openai import OpenAI

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()

def integration_doc_agent(state: dict) -> dict:
    print("\n🔗 Running Integration Doc Agent...")

    # --- Multi-user directory isolation ---
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)
    os.chdir(workflow_dir)
    # --------------------------------------

    arch_doc_file = os.path.join(workflow_dir, "architecture_doc.md")
    rtl_file = state.get("artifact", os.path.join(workflow_dir, "auto_module.v"))
    spec_json = state.get("spec_json", os.path.join(workflow_dir, "auto_module_spec.json"))

    arch_doc = ""
    rtl_code = ""
    spec_data = {}

    if os.path.exists(arch_doc_file):
        with open(arch_doc_file, "r") as f:
            arch_doc = f.read()
    if os.path.exists(rtl_file):
        with open(rtl_file, "r") as f:
            rtl_code = f.read()
    if os.path.exists(spec_json):
        with open(spec_json, "r") as f:
            spec_data = json.load(f)

    prompt = f"""
You are a hardware integration documentation specialist.
Generate an integration document that explains how this module integrates into a larger SoC or subsystem.

Inputs:
- Architecture Document:
{arch_doc[:2500]}

- Verilog RTL:
{rtl_code[:1500]}

- Spec JSON:
{json.dumps(spec_data, indent=2)}

Guidelines:
- Start with "## Integration Overview"
- Include subsections: System Context, Interface Signals, Integration Considerations, Power/Clock Domains, and Verification Notes.
- Keep content technical, not marketing.
- Use Markdown format only.
- Do NOT include ``` fences.
"""

    integration_doc = ""
    log_path = os.path.join(workflow_dir, "integration_doc_agent_compile.log")
    output_file = os.path.join(workflow_dir, "integration_doc.md")

    try:
        if USE_LOCAL_OLLAMA:
            print("⚙️ Using local Ollama for integration doc generation...")
            payload = {"model": "llama3", "prompt": prompt}
            with requests.post(OLLAMA_URL, json=payload, stream=True, timeout=600) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        j = json.loads(line.decode())
                        if "response" in j:
                            integration_doc += j["response"]
                            print(j["response"], end="", flush=True)
                    except Exception:
                        continue
        else:
            print("🌐 Using Portkey backend for integration doc generation...")
            completion = client_portkey.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                stream=True,
            )
            for chunk in completion:
                if chunk and hasattr(chunk, "choices"):
                    delta = chunk.choices[0].delta.get("content", "")
                    if delta:
                        integration_doc += delta
                        print(delta, end="", flush=True)
    except Exception as e:
        print(f"⚠️ LLM generation failed: {e}")
        state["status"] = f"⚠️ Integration doc generation failed: {e}"
        return state

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(integration_doc)

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"Integration documentation generated at {datetime.datetime.now()}\n")

    state.update({
        "status": "✅ Integration document generated",
        "artifact": output_file,
        "artifact_log": log_path,
        "integration_doc": integration_doc,
        "workflow_id": workflow_id,
        "workflow_dir": workflow_dir,
    })

    print(f"\n✅ Integration Doc Agent completed — {output_file}")
    return state
