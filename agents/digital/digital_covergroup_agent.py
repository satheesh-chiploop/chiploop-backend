import os, json, datetime, requests
from model_gateway import complete_text

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")

def run_agent(state: dict) -> dict:
    print("\n🎯 Running Covergroup Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    rtl_files = [f for f in os.listdir(workflow_dir) if f.endswith(".v")]
    rtl_file = os.path.join(workflow_dir, rtl_files[0]) if rtl_files else None
    if not rtl_file:
        raise FileNotFoundError("No RTL file found.")

    rtl_text = open(rtl_file, "r", encoding="utf-8").read()

    prompt = f"""
You are a SystemVerilog verification engineer.
Create functional covergroups for this RTL.
Include:
- coverpoints for key signals (clk, reset, enable, state, count, etc.)
- cross coverage for enable × reset, state × output, etc.
- add bins for min/mid/max values.
Output: A compilable covergroup definition (.sv file) for QuestaSim.

Rules:
- Start with a covergroup declaration:
  covergroup cg_<module_name> @(posedge <clock_port>);
- Add coverpoints for each input/output signal.
- No English text, comments, or markdown.
- End with 'endgroup'.
- Ensure all names match the DUT port list.
Output only SystemVerilog covergroup code (no markdown fences).
RTL snippet:
{rtl_text[:2000]}
"""

    cov_code = ""
    if USE_LOCAL_OLLAMA:
        payload = {"model": "llama3", "prompt": prompt}
        with requests.post(OLLAMA_URL, json=payload, stream=True) as r:
            for line in r.iter_lines():
                if not line: continue
                try:
                    j = json.loads(line.decode())
                    if "response" in j: cov_code += j["response"]
                except Exception: continue
    else:
        try:
            cov_code += complete_text(
                prompt,
                capability="verification_debug",
                agent_name="Digital Covergroup Agent",
                state=state,
            )
        except Exception as e:
            print(f"⚠️ Portkey failed, fallback to Ollama: {e}")
            payload = {"model": "llama3", "prompt": prompt}
            with requests.post(OLLAMA_URL, json=payload, stream=True) as r:
                for line in r.iter_lines():
                    if not line: continue
                    try:
                        j = json.loads(line.decode())
                        if "response" in j: cov_code += j["response"]
                    except Exception: continue

    cov_file = os.path.join(workflow_dir, "covergroups.sv")
    open(cov_file, "w", encoding="utf-8").write(cov_code)
    state.update({
        "status": "✅ Functional covergroups generated",
        "artifact": cov_file
    })
    print(f"✅ Covergroup Agent completed — {cov_file}")
    return state
