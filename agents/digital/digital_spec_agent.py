import subprocess
import os
import datetime
import json
import requests
from utils.artifact_utils import upload_artifact_generic, append_artifact_record
from portkey_ai import Portkey
from openai import OpenAI

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
USE_LOCAL_OLLAMA = os.getenv("USE_LOCAL_OLLAMA", "false").lower() == "true"
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY")
client_portkey = Portkey(api_key=PORTKEY_API_KEY)
client_openai = OpenAI()


# --- ✅ Added cleanup function (minimal addition) ---
def cleanup_verilog(verilog_code: str) -> str:
    lines = verilog_code.splitlines()
    seen = set()
    cleaned = []
    for line in lines:
        stripped = line.strip()
        # skip duplicate signal declarations (clk, reset, etc.)
        if any(keyword in stripped for keyword in ["input", "output", "inout"]):
            tokens = stripped.replace(";", "").split()
            sigs = [t for t in tokens if t not in ["input", "output", "inout", "wire", "reg", "logic"]]
            if any(sig in seen for sig in sigs):
                continue
            for sig in sigs:
                seen.add(sig)
        cleaned.append(line)
    return "\n".join(cleaned)


def run_agent(state: dict) -> dict:
    print("\n🚀 Running Spec Agent v4 (Spec JSON + RTL Generator)...")

    workflow_id = state.get("workflow_id", "default")
    workflow_dir = state.get("workflow_dir", f"backend/workflows/{workflow_id}")
    os.makedirs(workflow_dir, exist_ok=True)

    spec_data = state.get("spec", "")
    if not spec_data:
        state["status"] = "❌ No spec provided"
        return state

    try:
        spec = json.loads(spec_data) if isinstance(spec_data, str) else spec_data
    except Exception:
        spec = {"description": spec_data}

    module_name = spec.get("module", "auto_module")
    description = spec.get("description", spec_data if isinstance(spec_data, str) else "Generic digital design")

    # --- Normalize clocks & resets (same as before) ---
    def normalize_clock(c):
        return {"name": c.get("name", "clk"), "frequency_mhz": c.get("frequency_mhz", 100), "duty_cycle": c.get("duty_cycle", 0.5)}

    def normalize_reset(r):
        return {"name": r.get("name", "reset"), "active_low": r.get("active_low", False), "duration_cycles": r.get("duration_cycles", 5)}

    clocks = spec.get("clock", [])
    resets = spec.get("reset", [])
    if isinstance(clocks, dict): clocks = [clocks]
    if isinstance(resets, dict): resets = [resets]
    if not clocks: clocks = [{"name": "clk", "frequency_mhz": 100, "duty_cycle": 0.5}]
    if not resets: resets = [{"name": "reset", "active_low": False, "duration_cycles": 5}]
    clocks = [normalize_clock(c) for c in clocks]
    resets = [normalize_reset(r) for r in resets]

    io = spec.get("io", {})
    inputs = io.get("inputs", [])
    outputs = io.get("outputs", [])

    canonical_spec = {
        "module": module_name,
        "description": description,
        "clock": clocks,
        "reset": resets,
        "io": {"inputs": inputs, "outputs": outputs},
        "llm_source": "Ollama" if USE_LOCAL_OLLAMA else "Portkey/OpenAI",
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    os.makedirs(workflow_dir, exist_ok=True)
    spec_json_path = os.path.join(workflow_dir, f"{module_name}_spec.json")
    with open(spec_json_path, "w", encoding="utf-8") as f:
        json.dump(canonical_spec, f, indent=2)

    print(f"🧾 Spec JSON written → {spec_json_path}")

    # --- Slightly enhanced prompt (only one extra sentence) ---
    domain_info = "\n".join([
        f"- Clock {i+1}: {clk['name']} @ {clk['frequency_mhz']}MHz, duty={clk['duty_cycle']} | "
        f"Reset: {resets[min(i, len(resets)-1)]['name']} (active_low={resets[min(i, len(resets)-1)]['active_low']})"
        for i, clk in enumerate(clocks)
    ])

    prompt = f"""
You are a professional digital design engineer.
Generate synthesizable Verilog-2005 code for this specification.
Output must start with 'module' and end with 'endmodule'.
Do NOT include markdown code fences or explanations.
Ensure all ports are declared inside parentheses in the module declaration. 
Avoid duplicate declarations of signals like clk, reset, or common ports.
Each signal is declared only once across all modules.
Do not repeat `clk`, `reset`, or any input/output in submodules if already declared in the top module.
Avoid declaring loop indices (like i) globally.
Generate clean synthesizable Verilog with consistent indentation
Do NOT include undefined macros like `sv`, `enable`, or custom defines.
End every statement with a semicolon and close with `endmodule` only once.
Provide only compilable Verilog/SystemVerilog code — no explanations or comments outside the code.
Include all input/output declarations explicitly
Do not include any prose or sentences outside JSON. 

Specification JSON:
{json.dumps(canonical_spec, indent=2)}

Clock & Reset Context:
{domain_info}

Design Guidelines:
- Follow Verilog-2005 (not SystemVerilog).
- Use lowercase signal names.
- Declare all ports explicitly.
- For multiple clocks, create independent always blocks.
- For control or arithmetic designs, infer appropriate logic cleanly.
"""

    rtl_code = ""
    try:
        if USE_LOCAL_OLLAMA:
            print("⚙️ Using Ollama locally for Verilog generation...")
            payload = {"model": "llama3", "prompt": prompt}
            with requests.post(OLLAMA_URL, json=payload, stream=True, timeout=600) as r:
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
            print("🌐 Using Portkey/OpenAI backend...")
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

    rtl_code = rtl_code.replace("```verilog", "").replace("```", "").strip()
    if "module" in rtl_code:
        rtl_code = rtl_code[rtl_code.index("module"):]

    # --- ✅ Apply cleanup before writing to file ---
    rtl_code = cleanup_verilog(rtl_code)

    os.makedirs(workflow_dir, exist_ok=True)
    verilog_file = os.path.join(workflow_dir, f"{module_name}.v")
    with open(verilog_file, "w", encoding="utf-8") as f:
        f.write(rtl_code)

    log_path = os.path.join(workflow_dir, "spec_agent_compile.log")
    with open(log_path, "w") as logf:
        logf.write(f"Spec processed at {datetime.datetime.now()}\n")
        logf.write(f"Module: {module_name}\n")
        logf.write(f"Detected {len(clocks)} clock(s), {len(resets)} reset(s)\n")

    try:
        result = subprocess.run(
            ["/usr/bin/iverilog", "-o", "design.out", verilog_file],
            check=True, capture_output=True, text=True
        )
        if result.returncode == 0:
            compile_status = "✅ Verilog syntax check passed."
        else:
            compile_status = "⚠ Verilog syntax check failed."
        state["status"] = "✅ RTL and spec.json generated successfully"
        with open(log_path, "a") as logf:
            logf.write(result.stdout or "")
    except subprocess.CalledProcessError as e:
        state["status"] = "⚠ RTL generated but failed compilation"
        state["error_log"] = (e.stderr or e.stdout or "").strip()
        with open(log_path, "a") as logf:
            logf.write(state["error_log"])

    state.update({
        "artifact": verilog_file,
        "artifact_log": log_path,
        "spec_json": spec_json_path,
        "rtl": rtl_code,
        "workflow_dir": workflow_dir,
        "workflow_id": workflow_id,
    })
    # --- 📦 Upload artifacts to Supabase Storage ---
    try:
        user_id = state.get("user_id", "anonymous")
        workflow_id = state.get("workflow_id", "default")

    # Upload Spec JSON
        spec_storage = upload_artifact_generic(
           local_path=spec_json_path,
           user_id=user_id,
           workflow_id=workflow_id,
           agent_label="spec"
        )
        if spec_storage:
          append_artifact_record(workflow_id, "spec_json", spec_storage)

       # Upload Verilog RTL
        rtl_storage = upload_artifact_generic(
          local_path=verilog_file,
          user_id=user_id,
          workflow_id=workflow_id,
          agent_label="rtl"
       )
        if rtl_storage:
          append_artifact_record(workflow_id, "rtl_file", rtl_storage)

       # Upload compile log
        log_storage = upload_artifact_generic(
          local_path=log_path,
          user_id=user_id,
          workflow_id=workflow_id,
          agent_label="logs"
        )
        if log_storage:
          append_artifact_record(workflow_id, "compile_log", log_storage)

        print("🧩 All artifacts uploaded to Supabase Storage.")
    except Exception as e:
        print(f"⚠️ Artifact upload failed: {e}")

    print(f"\n✅ Generated {verilog_file} and {spec_json_path}")
    return state


