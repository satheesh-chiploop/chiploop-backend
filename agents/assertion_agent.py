import os, json, datetime, requests
from portkey_ai import Portkey

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)

def assertion_agent(state: dict) -> dict:
    print("\n🧠 Running Assertion Agent...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    rtl_files = [f for f in os.listdir(workflow_dir) if f.endswith(".v")]
    rtl_file = os.path.join(workflow_dir, rtl_files[0]) if rtl_files else None
    if not rtl_file:
        raise FileNotFoundError("RTL file not found for assertion generation.")

    rtl_text = open(rtl_file, "r", encoding="utf-8").read()

    prompt = f"""
You are an expert SystemVerilog verification engineer.
Given the RTL module below, generate SVA assertions to verify:

- Reset behavior
- Enable/disable operation
- Counter or FSM transition validity
- Clock stability
- Overflow/underflow protection (if applicable)
Output: A single .sv file containing only valid SVA code.

Rules:
- Do not include English text, comments, or markdown.
- Start with: default clocking @(posedge <clock_port>); endclocking
- Do NOT use backticks (`) or define any macros.
- Include only property, assert, and cover statements.
- No English text, markdown, or explanations.
- End file cleanly with no trailing commentary.
- Ensure QuestaSim compatibility (no undefined macros, no syntax outside SVA grammar)
- Use port names from the spec JSON exactly.
- Ensure syntax correctness for QuestaSim.
- Use `assert property` syntax, include a `default clocking @(posedge clk)` if needed.
- Each property should have a label and UVM-style message.

RTL snippet:
{rtl_text[:2000]}
Output only SystemVerilog code (no markdown fences).
"""

    assertions_code = ""
    log_path = os.path.join(workflow_dir, "assertion_agent.log")
    try:
        if USE_LOCAL_OLLAMA:
            payload = {"model": "llama3", "prompt": prompt}
            with requests.post(OLLAMA_URL, json=payload, stream=True) as r:
                for line in r.iter_lines():
                    if not line: continue
                    try:
                        j = json.loads(line.decode())
                        if "response" in j: assertions_code += j["response"]
                    except Exception: continue
        else:
            try:
                completion = client_portkey.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    stream=True,
                )
                for chunk in completion:
                    if chunk and hasattr(chunk, "choices"):
                        delta = chunk.choices[0].delta.get("content", "")
                        if delta: assertions_code += delta
            except Exception as e:
                print(f"⚠️ Portkey failed, fallback to Ollama: {e}")
                payload = {"model": "llama3", "prompt": prompt}
                with requests.post(OLLAMA_URL, json=payload, stream=True) as r:
                    for line in r.iter_lines():
                        if not line: continue
                        try:
                            j = json.loads(line.decode())
                            if "response" in j: assertions_code += j["response"]
                        except Exception: continue
    except Exception as e:
        state["status"] = f"❌ Assertion generation failed: {e}"
        return state

    sv_file = os.path.join(workflow_dir, "assertions.sv")
    open(sv_file, "w", encoding="utf-8").write(assertions_code)

    with open(log_path, "w") as f:
        f.write(f"Assertions generated at {datetime.datetime.now()}\n")

    state.update({
        "status": "✅ Assertions generated",
        "artifact": sv_file,
        "artifact_log": log_path,
    })
    print(f"✅ Assertion Agent completed — {sv_file}")
    return state
