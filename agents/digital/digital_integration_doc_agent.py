import os
import json
import datetime
import requests
from portkey_ai import Portkey
from openai import OpenAI

# --- Config ---
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


def run_agent(state: dict) -> dict:
    print("\n🔗 Running Integration Doc Agent...")

    # --- Multi-user safe workflow dir ---
    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)
    # -------------------------------------

    # --- Dynamic input file detection ---
    arch_doc_file = os.path.join(workflow_dir, "architecture_doc.md")

    # find RTL (any .v file)
    rtl_file = state.get("artifact")
    if not rtl_file or not os.path.exists(rtl_file):
        v_files = [f for f in os.listdir(workflow_dir) if f.endswith(".v")]
        rtl_file = os.path.join(workflow_dir, v_files[0]) if v_files else None

    # find Spec JSON
    spec_json = state.get("spec_json")
    if not spec_json or not os.path.exists(spec_json):
        json_files = [f for f in os.listdir(workflow_dir) if f.endswith("_spec.json")]
        spec_json = os.path.join(workflow_dir, json_files[0]) if json_files else None

    arch_doc, rtl_code, spec_data = "", "", {}

    if os.path.exists(arch_doc_file):
        with open(arch_doc_file, "r", encoding="utf-8") as f:
            arch_doc = f.read()
    if rtl_file and os.path.exists(rtl_file):
        with open(rtl_file, "r", encoding="utf-8") as f:
            rtl_code = f.read()
    if spec_json and os.path.exists(spec_json):
        with open(spec_json, "r", encoding="utf-8") as f:
            spec_data = json.load(f)

    # --- Prompt construction ---
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

    # --- LLM handling ---
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
                            integration_doc += delta
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
                                integration_doc += j["response"]
                        except Exception:
                            continue

    except Exception as e:
        print(f"❌ Integration Doc generation failed: {e}")
        state["status"] = f"❌ Integration Doc generation failed: {e}"
        return state

    # --- Save outputs ---
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(integration_doc or "⚠️ No integration content generated.")

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"Integration documentation generated at {datetime.datetime.now()}\n")

    # --- Update state ---
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


# --- Local test run ---
if __name__ == "__main__":
    test_state = {"workflow_id": "test_integration_doc"}
    print(json.dumps(integration_doc_agent(test_state), indent=2))
